# DistrictWatch Backend Aggregation & Data Quality Methodology

## 1. Distinction Between CPCB Station AQI and DistrictWatch District Metrics
The Central Pollution Control Board (CPCB) publishes official National Air Quality Index (NAQI) readings for **individual monitoring stations** and **CPCB-designated cities**—it does not publish an official administrative district-level AQI.

Accordingly, any district-level AQI displayed in DistrictWatch is a **DistrictWatch Composite District Metric** derived from verified ground stations located within that administrative district's boundary. Every populated district snapshot includes the explicit caveat:
> *"DistrictWatch composite metric (mean of station-level CPCB AQIs across verified district stations) — not an official CPCB district-level index."*

## 2. Station-First Calculation Pipeline
DistrictWatch never averages raw concentrations across a district before computing sub-indices. Instead, calculation proceeds strictly from the station level upward:

1. **Pollutant Averaging Windows (Station Level)**:
   - **24-hour rolling window ($W_p = 24	ext{h}$)**: Applied to `PM2.5`, `PM10`, `NO2`, and `SO2`.
   - **8-hour rolling window ($W_p = 8	ext{h}$)**: Applied to `CO` and `O3`.
2. **Minimum Completeness Rule (Station Level)**:
   - A station pollutant evaluation over a 24-hour period requires **at least 16 valid hourly observations** (`>= 16` of 24 hours). For 8-hour pollutants (`CO`, `O3`), at least 16 valid hourly observations across the 24-hour day and **at least 6 valid hours within the active 8-hour window** (`>= 6` of 8 hours) are required.
   - Missing hours are never interpolated.
3. **Minimum 3-Pollutant + PM Rule (Station Level)**:
   - A station's overall AQI ($AQI_s$) is computed as $\max_p(I_{s,p})$ **only if at least 3 pollutants have valid window-averaged sub-indices AND at least one of those valid pollutants is `PM2.5` or `PM10`**.
   - If a station fails either the completeness threshold or the 3-pollutant + PM rule, its station AQI is recorded as `null` (`is_valid_station_aqi = false`).
4. **District Aggregation (Station-First)**:
   - **District Composite AQI**: Arithmetic mean (rounded to nearest integer) of valid station-level AQIs ($AQI_s$) across verified stations in the district that satisfied all CPCB station completeness rules.
   - **District Pollutant Sub-Indices**: Arithmetic mean of the valid station-level sub-indices $I_{s,p}$ for each pollutant.

## 3. Window-Aligned Station-Pollutant-Hour Coverage Definition
Because `PM2.5`, `PM10`, `NO2`, and `SO2` use 24-hour averaging windows ($W_p = 24$) while `CO` and `O3` use 8-hour averaging windows ($W_p = 8$), the coverage denominator sums the exact averaging window length $W_p$ for each expected pollutant at each verified station in the district (`REVIEWED_STATIONS`), rather than assuming a uniform 24-hour window for all 6 pollutants:

- **Per-Station Expected Pollutant-Hours**:
  $$(4 \times 24\text{ hours}) + (2 \times 8\text{ hours}) = 96 + 16 = \mathbf{112\text{ expected pollutant-hours per station}}$$
- **District Expected Station-Pollutant-Hours**:
  $$\text{Expected Observations} = \sum_{s=1}^{S_{\text{registered}}} \sum_{p \in P_s} W_p = 112 \times S_{\text{registered}}$$
- **District Coverage (`0.0–1.0`)**:
  $$\text{Coverage} = \frac{\sum_{s=1}^{S_{\text{registered}}} \sum_{p \in P_s} \min(W_p, \text{ValidObservedHoursInWindow}_{s,p})}{\sum_{s=1}^{S_{\text{registered}}} \sum_{p \in P_s} W_p}$$
