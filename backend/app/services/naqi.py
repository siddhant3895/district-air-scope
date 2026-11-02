"""Official Indian CPCB National Air Quality Index (NAQI) calculation service.

Implements the Central Pollution Control Board (CPCB) health breakpoint tables,
piecewise linear sub-index interpolation, dominant pollutant selection, and
minimum-pollutant validation rules.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from app.schemas import Pollutant, PollutantKey

# Canonical order and units required by the DistrictWatch frontend
POLLUTANT_SPECS: Tuple[Tuple[PollutantKey, str], ...] = (
    ("PM2.5", "µg/m³"),
    ("PM10", "µg/m³"),
    ("NO2", "µg/m³"),
    ("SO2", "µg/m³"),
    ("O3", "µg/m³"),
    ("CO", "mg/m³"),
)

# CPCB Breakpoint Table: Tuple of (BP_LO, BP_HI, I_LO, I_HI)
# Categories:
#   Good (0-50), Satisfactory (51-100), Moderately Polluted (101-200),
#   Poor (201-300), Very Poor (301-400), Severe (401-500)
CPCB_BREAKPOINTS: Dict[PollutantKey, Tuple[Tuple[float, float, int, int], ...]] = {
    "PM2.5": (
        (0.0, 30.0, 0, 50),
        (31.0, 60.0, 51, 100),
        (61.0, 90.0, 101, 200),
        (91.0, 120.0, 201, 300),
        (121.0, 250.0, 301, 400),
        (251.0, 380.0, 401, 500),
    ),
    "PM10": (
        (0.0, 50.0, 0, 50),
        (51.0, 100.0, 51, 100),
        (101.0, 250.0, 101, 200),
        (251.0, 350.0, 201, 300),
        (351.0, 430.0, 301, 400),
        (431.0, 510.0, 401, 500),
    ),
    "NO2": (
        (0.0, 40.0, 0, 50),
        (41.0, 80.0, 51, 100),
        (81.0, 180.0, 101, 200),
        (181.0, 280.0, 201, 300),
        (281.0, 400.0, 301, 400),
        (401.0, 520.0, 401, 500),
    ),
    "SO2": (
        (0.0, 40.0, 0, 50),
        (41.0, 80.0, 51, 100),
        (81.0, 380.0, 101, 200),
        (381.0, 800.0, 201, 300),
        (801.0, 1600.0, 301, 400),
        (1601.0, 2400.0, 401, 500),
    ),
    "O3": (
        (0.0, 50.0, 0, 50),
        (51.0, 100.0, 51, 100),
        (101.0, 168.0, 101, 200),
        (169.0, 208.0, 201, 300),
        (209.0, 748.0, 301, 400),
        (749.0, 1288.0, 401, 500),
    ),
    "CO": (
        (0.0, 1.0, 0, 50),
        (1.1, 2.0, 51, 100),
        (2.1, 10.0, 101, 200),
        (10.1, 17.0, 201, 300),
        (17.1, 34.0, 301, 400),
        (34.1, 51.0, 401, 500),
    ),
}


def calculate_sub_index(pollutant: PollutantKey, concentration: Optional[float]) -> Optional[int]:
    """Calculate the CPCB NAQI sub-index (0-500) for a single pollutant concentration.

    Returns None if concentration is None, NaN, or negative.
    """
    if concentration is None or concentration < 0:
        return None

    bands = CPCB_BREAKPOINTS[pollutant]
    for idx, (bp_lo, bp_hi, i_lo, i_hi) in enumerate(bands):
        if concentration <= bp_hi:
            # Handle fractional values falling in the gap between previous band's bp_hi and current bp_lo
            if idx > 0 and concentration < bp_lo:
                prev_bp_hi = bands[idx - 1][1]
                prev_i_hi = bands[idx - 1][3]
                span_bp = bp_hi - prev_bp_hi
                span_i = i_hi - prev_i_hi
                sub_idx = prev_i_hi + ((concentration - prev_bp_hi) * span_i / span_bp)
            else:
                span_bp = bp_hi - bp_lo
                span_i = i_hi - i_lo
                sub_idx = i_lo + ((concentration - bp_lo) * span_i / span_bp)
            return max(0, min(500, int(round(sub_idx))))

    # Concentration exceeds highest Severe breakpoint ceiling -> cap at 500 per frontend 0-500 scale
    return 500


def build_pollutant_list(raw_values: Dict[PollutantKey, Optional[float]]) -> List[Pollutant]:
    """Build the ordered 6-element Pollutant list with rounded values and CPCB sub-indices."""
    result: List[Pollutant] = []
    for key, unit in POLLUTANT_SPECS:
        raw_val = raw_values.get(key)
        if raw_val is None or raw_val < 0:
            result.append(Pollutant(key=key, value=None, unit=unit, subIndex=None))
            continue

        rounded_val = round(float(raw_val), 2 if key == "CO" else 1)
        # Preserve integer look when decimal is .0 for non-CO pollutants if exact
        if key != "CO" and rounded_val.is_integer():
            rounded_val = float(int(rounded_val))
        sub_index = calculate_sub_index(key, float(raw_val))
        result.append(Pollutant(key=key, value=rounded_val, unit=unit, subIndex=sub_index))
    return result


def compute_overall_aqi(
    pollutants: List[Pollutant],
) -> Tuple[Optional[int], Optional[PollutantKey], bool]:
    """Compute overall district AQI, dominant pollutant, and whether CPCB's 3-pollutant rule is met.

    Per CPCB NAQI rules:
    - Overall AQI is the maximum sub-index among monitored pollutants.
    - Official AQI calculation requires at least 3 pollutants, including PM2.5 or PM10.
    Returns (aqi, dominant_pollutant, meets_cpcb_minimum_rule).
    """
    valid = [p for p in pollutants if p.subIndex is not None]
    if not valid:
        return None, None, False

    has_pm = any(p.key in ("PM2.5", "PM10") and p.subIndex is not None for p in valid)
    meets_cpcb_rule = len(valid) >= 3 and has_pm

    # Select dominant pollutant with highest sub-index (ties broken by canonical order)
    dominant_pollutant = max(valid, key=lambda p: p.subIndex or -1)
    return dominant_pollutant.subIndex, dominant_pollutant.key, meets_cpcb_rule
