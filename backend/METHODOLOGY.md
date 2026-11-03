# DistrictWatch Backend Aggregation & Data Integrity Methodology

## 1. CPCB `data.gov.in` (`3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69`) Unit Semantics & Safe Failure
In the Open Government Data Platform India (`data.gov.in`) resource `3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69` (*"Real time Air Quality Index from various location"*), the catalog schema defines `pollutant_min` (`double`), `pollutant_max` (`double`), `pollutant_avg` (`double`), and `pollutant_unit` (`keyword`). However, actual API responses populate `pollutant_unit` as literal `"NA"` (or omit it) on every record without documenting whether `pollutant_avg` represents a raw physical concentration (`µg/m³` / `mg/m³`) or a pre-calculated dimensionless CPCB sub-index (`0–500`).

- **No Silent Assumptions**: Assuming a unitless number is a sub-index (or a concentration) risks silently producing erroneous AQI values.
- **Safe Failure**: Whenever `raw_unit` is missing or `"NA"`, `normalize_upstream_observation()` marks the record as `value_kind = "unverified_scale"` and `is_valid = False`. The raw record and payload are preserved in `raw_station_observations` for auditability, but never enter station or district AQI calculations unless an explicit physical unit (`µg/m³`, `mg/m³`, `ppm`, `ppb`) or verified scale metadata is provided.

## 2. Distinction Between CPCB Station AQI and DistrictWatch District Metrics
The Central Pollution Control Board (CPCB) publishes official National Air Quality Index (NAQI) readings for **individual monitoring stations** and **CPCB-designated cities**—it does not publish an official administrative district-level AQI.

Accordingly, any district-level AQI displayed in DistrictWatch is a **DistrictWatch Composite District Metric** derived from verified ground stations located within that administrative district's boundary. Every populated district snapshot includes the explicit caveat:
> *"DistrictWatch composite metric (mean of station-level CPCB AQIs across verified district stations) — not an official CPCB district-level index."*

## 3. Station-First Calculation Pipeline & IST Evaluation Boundaries
1. **Indian Standard Time (`IST`, `UTC+05:30`) Day Boundaries**:
   - Daily evaluation dates (`YYYY-MM-DD`) are defined in **IST (`UTC+05:30`)**, spanning `00:00:00+05:30` (`18:30:00 UTC` previous day) through `23:59:59+05:30` (`18:29:59 UTC` current day).
   - For any IST calendar day with valid observations, the station's rolling evaluation window is anchored to the latest valid observation timestamp within that IST day (`[eval_end - 24h, eval_end]` and `[eval_end - 8h, eval_end]`).
2. **Pollutant Averaging Windows (Station Level)**:
   - **24-hour rolling window ($W_p = 24\text{h}$)**: Applied to `PM2.5`, `PM10`, `NO2`, and `SO2`.
   - **8-hour rolling window ($W_p = 8\text{h}$)**: Applied to `CO` and `O3`.
3. **Minimum Completeness Rule (Station Level)**:
   - A station pollutant evaluation over a 24-hour period requires **at least 16 valid hourly observations** (`>= 16` of 24 hours). For 8-hour pollutants (`CO`, `O3`), at least 16 valid hourly observations across the 24-hour day and **at least 6 valid hours within the active 8-hour window** (`>= 6` of 8 hours) are required.
   - Missing hours are never interpolated.
4. **Minimum 3-Pollutant + PM Rule (Station Level)**:
   - A station's overall AQI ($AQI_s$) is computed as $\max_p(I_{s,p})$ **only if at least 3 pollutants have valid window-averaged sub-indices AND at least one of those valid pollutants is `PM2.5` or `PM10`**.
5. **Calculation Provenance**:
   - `StationAqiEvaluation.source_provider` (`"openaq_v3"`, `"cpcb_ogd"`, `"hybrid_cpcb_openaq"`, or `"none"`) and `DailyDistrictHistoryRecord.source_attribution` are derived strictly from the actual valid station observations that contributed to the evaluation.

## 4. Window-Aligned Station-Pollutant-Hour Coverage Definition
Because `PM2.5`, `PM10`, `NO2`, and `SO2` use 24-hour averaging windows ($W_p = 24$) while `CO` and `O3` use 8-hour averaging windows ($W_p = 8$), the coverage denominator sums the exact averaging window length $W_p$ for each expected pollutant at each verified station in the district (`REVIEWED_STATIONS`):
- **Per-Station Expected Pollutant-Hours**: $(4 \times 24\text{h}) + (2 \times 8\text{h}) = 96 + 16 = \mathbf{112\text{ expected pollutant-hours per station}}$
- **District Coverage (`0.0–1.0`)**:
  $$\text{Coverage} = \frac{\sum_{s=1}^{S_{\text{registered}}} \sum_{p \in P_s} \min(W_p, \text{ValidObservedHoursInWindow}_{s,p})}{112 \times S_{\text{registered}}}$$
