"""Pollutant unit validation, molecular-weight conversion, and source-semantics normalizer.

Enforces three strict data-integrity rules:
1. Validate pollutant units using the actual upstream unit fields (`raw_unit`).
2. Never assume a unitless value (`raw_unit` is None, empty, or 'NA') is a sub-index
   or a physical concentration.
3. If upstream unit or scale semantics cannot be established unambiguously from the
   record's fields, fail safely (`value_kind='unverified_scale'`, `is_valid=False`) so
   the adapter can never silently produce incorrect AQI values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional
from app.models import SourceProvider, ValueKind
from app.schemas import PollutantKey
from app.services.naqi import calculate_sub_index
from app.services.quality import sanitize_concentration

# Standard molar volume of ideal gas at 25°C (298.15 K) and 1 atm (101.325 kPa): 24.45 L/mol
MOLAR_VOLUME_25C = 24.45

# Molecular weights (g/mol) for gaseous pollutants
MOLECULAR_WEIGHTS: Dict[PollutantKey, float] = {
    "NO2": 46.0055,
    "SO2": 64.066,
    "O3": 48.00,
    "CO": 28.01,
}

CANONICAL_UNITS: Dict[PollutantKey, str] = {
    "PM2.5": "µg/m³",
    "PM10": "µg/m³",
    "NO2": "µg/m³",
    "SO2": "µg/m³",
    "O3": "µg/m³",
    "CO": "mg/m³",
}

UG_M3_ALIASES = {"µg/m³", "ug/m3", "ug/m³", "μg/m³", "microgram/m3"}
MG_M3_ALIASES = {"mg/m³", "mg/m3", "milligram/m3"}
EXPLICIT_SUB_INDEX_UNITS = {"aqi", "sub_index", "sub-index", "naqi"}


@dataclass(frozen=True)
class NormalizedObservationResult:
    value_kind: ValueKind
    normalized_concentration: Optional[float]
    normalized_unit: Optional[str]
    calculated_sub_index: Optional[int]
    is_valid: bool
    validation_note: str


def normalize_upstream_observation(
    *,
    source_provider: SourceProvider,
    pollutant_key: PollutantKey,
    raw_value_text: str,
    raw_unit: Optional[str],
    explicit_value_kind: Optional[str] = None,
) -> NormalizedObservationResult:
    """Validate and normalize a raw upstream reading based strictly on explicit unit and scale metadata.

    Why unitless values fail safely:
    In CPCB's `data.gov.in` resource `3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69`, the schema
    includes `pollutant_unit` (keyword) and `pollutant_avg` (double), but actual API
    records populate `pollutant_unit` as literal `'NA'` without documenting whether
    `pollutant_avg` is a raw concentration or a pre-calculated CPCB sub-index.
    Assuming a unitless number is a sub-index (or a concentration) can silently corrupt AQI
    calculations. Therefore, any record with a missing or `'NA'` unit is rejected safely
    (`value_kind='unverified_scale'`, `is_valid=False`) while still being stored in
    `raw_station_observations` for auditability.
    """
    text = str(raw_value_text).strip() if raw_value_text is not None else ""
    if not text or text.upper() in {"NA", "N/A", "NULL", "NONE", "-", ""}:
        return NormalizedObservationResult(
            value_kind="missing",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=None,
            is_valid=False,
            validation_note="Upstream value is missing or 'NA'.",
        )

    try:
        numeric_val = float(text)
    except ValueError:
        return NormalizedObservationResult(
            value_kind="invalid",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=None,
            is_valid=False,
            validation_note=f"Non-numeric upstream value '{text}'.",
        )

    if numeric_val < 0:
        return NormalizedObservationResult(
            value_kind="invalid",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=None,
            is_valid=False,
            validation_note=f"Negative upstream value ({numeric_val}) rejected.",
        )

    unit_clean = raw_unit.strip().lower() if raw_unit and raw_unit.strip() else None
    kind_clean = explicit_value_kind.strip().lower() if explicit_value_kind else None

    # Safe Failure Rule: Never assume a unitless or 'NA'-unit value is a sub-index or concentration.
    if unit_clean is None or unit_clean in {"na", "n/a", "none", "null", "-", "unknown"}:
        return NormalizedObservationResult(
            value_kind="invalid",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=None,
            is_valid=False,
            validation_note=(
                f"Rejected safely: upstream {source_provider} record has missing or 'NA' unit "
                f"(raw_unit={raw_unit!r}); cannot verify whether value '{text}' is a physical "
                f"concentration or a pre-calculated sub-index."
            ),
        )

    # Explicit Sub-Index Case: Only accepted when the upstream unit explicitly states an index scale
    # AND explicit_value_kind confirms sub_index semantics.
    if unit_clean in EXPLICIT_SUB_INDEX_UNITS:
        if kind_clean != "sub_index":
            return NormalizedObservationResult(
                value_kind="invalid",
                normalized_concentration=None,
                normalized_unit=None,
                calculated_sub_index=None,
                is_valid=False,
                validation_note=(
                    f"Rejected safely: unit '{raw_unit}' indicates an index scale but "
                    f"explicit_value_kind='{explicit_value_kind}' did not confirm sub_index semantics."
                ),
            )
        if numeric_val > 500.0:
            return NormalizedObservationResult(
                value_kind="invalid",
                normalized_concentration=None,
                normalized_unit=None,
                calculated_sub_index=None,
                is_valid=False,
                validation_note=f"Sub-index {numeric_val} exceeds maximum 500 scale ceiling.",
            )
        return NormalizedObservationResult(
            value_kind="sub_index",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=int(round(numeric_val)),
            is_valid=True,
            validation_note=f"Verified explicit sub-index (raw_unit='{raw_unit}').",
        )

    # Concentration Case: Requires a verified physical concentration unit
    target_unit = CANONICAL_UNITS[pollutant_key]
    converted_conc: Optional[float] = None
    conversion_note = ""

    if pollutant_key in ("PM2.5", "PM10"):
        if unit_clean in UG_M3_ALIASES:
            converted_conc = numeric_val
            conversion_note = "Verified mass concentration in µg/m³."
        elif unit_clean in MG_M3_ALIASES:
            converted_conc = numeric_val * 1000.0
            conversion_note = "Converted particulate concentration from mg/m³ to µg/m³ (×1000)."
        else:
            return NormalizedObservationResult(
                value_kind="invalid",
                normalized_concentration=None,
                normalized_unit=None,
                calculated_sub_index=None,
                is_valid=False,
                validation_note=f"Unsupported particulate unit '{raw_unit}' for {pollutant_key}.",
            )
    elif pollutant_key == "CO":
        mw = MOLECULAR_WEIGHTS["CO"]
        if unit_clean in MG_M3_ALIASES:
            converted_conc = numeric_val
            conversion_note = "Verified CO concentration in mg/m³."
        elif unit_clean in UG_M3_ALIASES:
            converted_conc = numeric_val / 1000.0
            conversion_note = "Converted CO concentration from µg/m³ to mg/m³ (÷1000)."
        elif unit_clean == "ppm":
            converted_conc = numeric_val * (mw / MOLAR_VOLUME_25C)
            conversion_note = "Converted CO from ppm to mg/m³ at 25°C, 1 atm."
        elif unit_clean == "ppb":
            converted_conc = (numeric_val / 1000.0) * (mw / MOLAR_VOLUME_25C)
            conversion_note = "Converted CO from ppb to mg/m³ at 25°C, 1 atm."
        else:
            return NormalizedObservationResult(
                value_kind="invalid",
                normalized_concentration=None,
                normalized_unit=None,
                calculated_sub_index=None,
                is_valid=False,
                validation_note=f"Unsupported CO unit '{raw_unit}'.",
            )
    else:
        mw = MOLECULAR_WEIGHTS[pollutant_key]
        if unit_clean in UG_M3_ALIASES:
            converted_conc = numeric_val
            conversion_note = f"Verified {pollutant_key} concentration in µg/m³."
        elif unit_clean in MG_M3_ALIASES:
            converted_conc = numeric_val * 1000.0
            conversion_note = f"Converted {pollutant_key} from mg/m³ to µg/m³ (×1000)."
        elif unit_clean == "ppb":
            converted_conc = numeric_val * (mw / MOLAR_VOLUME_25C)
            conversion_note = f"Converted {pollutant_key} from ppb to µg/m³ at 25°C, 1 atm."
        elif unit_clean == "ppm":
            converted_conc = numeric_val * 1000.0 * (mw / MOLAR_VOLUME_25C)
            conversion_note = f"Converted {pollutant_key} from ppm to µg/m³ at 25°C, 1 atm."
        else:
            return NormalizedObservationResult(
                value_kind="invalid",
                normalized_concentration=None,
                normalized_unit=None,
                calculated_sub_index=None,
                is_valid=False,
                validation_note=f"Unsupported gaseous unit '{raw_unit}' for {pollutant_key}.",
            )

    sanitized = sanitize_concentration(pollutant_key, converted_conc)
    if sanitized is None:
        return NormalizedObservationResult(
            value_kind="invalid",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=None,
            is_valid=False,
            validation_note=(
                f"Converted concentration ({converted_conc:.2f} {target_unit}) exceeds "
                f"physical plausibility ceiling for {pollutant_key}."
            ),
        )

    sub_idx = calculate_sub_index(pollutant_key, sanitized)
    return NormalizedObservationResult(
        value_kind="concentration",
        normalized_concentration=round(sanitized, 4),
        normalized_unit=target_unit,
        calculated_sub_index=sub_idx,
        is_valid=True,
        validation_note=conversion_note,
    )
