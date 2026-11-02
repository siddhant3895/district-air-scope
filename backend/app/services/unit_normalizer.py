"""Pollutant unit validation, molecular-weight conversion, and source-semantics normalizer.

Enforces two strict rules:
1. Validate pollutant units using the actual upstream unit fields (`raw_unit`).
2. Never assume `pollutant_avg` (or any upstream numeric field) is already a sub-index
   or a raw concentration without checking its unit and upstream source semantics.
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
SUB_INDEX_UNIT_ALIASES = {"aqi", "sub_index", "sub-index", "index", "naqi"}


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
    """Validate and normalize a raw upstream reading based on its unit and source semantics."""
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

    # Case 1: Upstream record explicitly declares or lacks concentration units on CPCB OGD
    # In data.gov.in resource 3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69, CPCB publishes
    # pre-calculated pollutant sub-indices (0-500) with no unit field (or unit='NA').
    # We NEVER treat a unitless CPCB OGD pollutant_avg as a raw concentration.
    if unit_clean in SUB_INDEX_UNIT_ALIASES or (
        source_provider == "cpcb_ogd"
        and (unit_clean is None or unit_clean == "na")
        and kind_clean in (None, "sub_index")
    ):
        if numeric_val > 500.0:
            return NormalizedObservationResult(
                value_kind="invalid",
                normalized_concentration=None,
                normalized_unit=None,
                calculated_sub_index=None,
                is_valid=False,
                validation_note=f"CPCB sub-index {numeric_val} exceeds maximum 500 scale ceiling.",
            )
        return NormalizedObservationResult(
            value_kind="sub_index",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=int(round(numeric_val)),
            is_valid=True,
            validation_note=(
                "Validated as dimensionless CPCB pollutant sub-index (0-500); "
                "raw concentration intentionally left null because upstream omits concentration units."
            ),
        )

    # Case 2: Concentration value — requires an explicit, recognized physical concentration unit
    if unit_clean is None:
        return NormalizedObservationResult(
            value_kind="invalid",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=None,
            is_valid=False,
            validation_note=f"Missing upstream unit field for {source_provider} observation.",
        )

    target_unit = CANONICAL_UNITS[pollutant_key]
    converted_conc: Optional[float] = None
    conversion_note = ""

    if pollutant_key in ("PM2.5", "PM10"):
        # Particulates must be reported in mass/volume (µg/m³ or mg/m³)
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
        # CO canonical unit is mg/m³
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
        # Gaseous pollutants (NO2, SO2, O3) -> canonical unit is µg/m³
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
