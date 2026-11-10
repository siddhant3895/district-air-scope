# District Air Scope ------------

**District Air Scope** is an India-focused air-quality monitoring project built around a curated registry of 30 districts. It combines a React/TypeScript frontend with a Python FastAPI backend that ingests station observations, evaluates pollutant measurements using CPCB National Air Quality Index (NAQI) rules, and presents district snapshots with source attribution, coverage, freshness and data-quality caveats.

The project is under active development. A district being present in the registry does **not** mean current, valid measurements are available for it. Live data depends on provider access, station coverage, successful ingestion and the observation-completeness rules below.

> **Important:** DistrictWatch's district-level value is a project-defined composite derived from verified monitoring stations. It is not an official CPCB district-level AQI.

## What is implemented

- A 30-district registry with state filtering and district search.
- A responsive TanStack Start, React and TypeScript frontend.
- A FastAPI backend with read-only public data endpoints.
- Ingestion adapters for XKDR Forum, OpenAQ v3 and the CPCB Open Government Data (OGD) API.
- SQLite persistence for station metadata, raw observations, evaluations, sync history and daily district history.
- Station-first pollutant and AQI evaluation, with explicit validity and completeness checks.
- District snapshots containing AQI, pollutant sub-indices, observation freshness, coverage, quality and caveats.
- A provider-health endpoint that reports configuration, sync status and data freshness.
- Backend unit, provider-adapter, aggregation, API-contract and regression tests.
- GitHub Actions checks for backend tests, frontend linting, type checking, tests and production build.

## Architecture

```text
district-air-scope/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI routes
│   │   ├── ingestion/    # CPCB OGD, OpenAQ and XKDR adapters
│   │   ├── services/     # Normalisation, NAQI, quality and aggregation
│   │   ├── districts_registry.py
│   │   ├── database.py
│   │   └── cli.py        # Database and ingestion commands
│   ├── tests/
│   ├── METHODOLOGY.md
│   └── requirements.txt
├── src/
│   ├── data/             # API adapter, mock service and shared types
│   └── routes/           # Overview, district and methodology pages
├── .github/workflows/
├── .env.example
├── backend/.env.example
├── docker-compose.yml
└── package.json
```

### Request and data flow

1. The ingestion CLI requests data from configured providers.
2. Provider adapters parse upstream records and normalise timestamps, pollutant names and measurement units.
3. Raw observations are stored with source and validity information.
4. The backend evaluates pollutant windows and station AQI before creating daily district history.
5. The API exposes district snapshots and health information.
6. The frontend consumes the API through a shared data-service interface. Illustrative mock data is available only as an explicitly selected development mode.

## Data sources

| Provider | Purpose | Configuration |
|---|---|---|
| [XKDR India Air Quality Database](https://airquality.xkdr.org/) | Harmonised hourly station measurements, including CPCB CAAQM and US diplomatic-post data | **XKDR_API_KEY**, **XKDR_API_BASE_URL** |
| [OpenAQ](https://explore.openaq.org/) | Station and sensor discovery, hourly measurements and historical observations | **OPENAQ_API_KEY** |
| [Open Government Data Platform India](https://data.gov.in/) | Optional CPCB OGD air-quality feed | **DATA_GOV_IN_API_KEY** |

Provider keys are configured independently. A key being present only means that the provider is configured; it does not mean that a successful sync has happened or that fresh, valid observations are available.

### Data integrity rules

- Source attribution is retained with ingested observations and derived records.
- Measurements with missing or unverified units are not silently interpreted as concentrations or AQI sub-indices.
- The CPCB OGD resource can return records with **pollutant_unit** missing or set to **NA**. Such records are retained for auditability but excluded from AQI calculations unless their scale can be verified.
- Missing measurements remain missing. The pipeline does not interpolate missing hourly readings or convert unavailable values into zero.
- Station-to-district membership is based on the reviewed station registry; nearby stations are not automatically treated as belonging to a district.
- XKDR data requires the attribution specified by its licence and source terms. Review provider terms before redistributing data.

## AQI methodology

The backend follows a station-first evaluation process based on CPCB NAQI pollutant windows and validity requirements.

### Pollutant averaging windows

| Pollutants | Evaluation window |
|---|---:|
| PM2.5, PM10, NO2, SO2 | 24 hours |
| CO, O3 | 8 hours |

### Completeness and station validity

- A 24-hour pollutant window requires at least 16 valid hourly observations.
- For CO and O3, the daily completeness requirement is combined with at least 6 valid observations in the active 8-hour window.
- Missing hours are not interpolated.
- A station AQI is produced only when at least 3 pollutants have valid sub-indices and at least one of them is PM2.5 or PM10.
- The station AQI is the maximum of that station's valid pollutant sub-indices.

### District composite

For each district, the project aggregates station-level evaluations from stations whose membership has been reviewed.

The displayed district metric is the mean of the qualifying station AQIs. It is labelled as a **DistrictWatch composite metric**, not an official CPCB administrative-district AQI. District snapshots also expose reporting-station count, source attribution, coverage, quality and caveats so the number can be interpreted in context.

### Coverage

Coverage is based on expected pollutant-hours across the six criteria pollutants:

- Four 24-hour pollutants: 4 × 24 = 96 expected pollutant-hours.
- Two 8-hour pollutants: 2 × 8 = 16 expected pollutant-hours.
- Total: **112 expected pollutant-hours per reviewed station**.

District coverage is the observed, valid pollutant-hours divided by the expected pollutant-hours across registered stations. It is a data-completeness measure, not a measure of geographic population coverage.

Daily evaluation dates use Indian Standard Time (IST, UTC+05:30). Implementation details, including unit handling, quality checks and caveats, are documented in [backend/METHODOLOGY.md](backend/METHODOLOGY.md).

## Getting started

### Requirements

- Git
- Python 3.11 (the backend CI workflow uses Python 3.11)
- [Bun](https://bun.sh/) for the frontend workflow and lockfile
- Provider API credentials for whichever live sources you intend to use

The repository currently contains both a Bun lockfile and an npm lockfile. CI uses Bun and installs with the frozen Bun lockfile; use Bun locally to match CI.

### 1. Clone the repository

```bash
git clone https://github.com/siddhant3895/district-air-scope.git
cd district-air-scope
```

### 2. Configure environment files

Create the frontend and backend environment files from their examples.

macOS / Linux:

```bash
cp .env.example .env
cp backend/.env.example backend/.env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
Copy-Item backend/.env.example backend/.env
```

Add provider credentials to **backend/.env**. Configure at least one provider you intend to use. Keep real credentials out of Git and do not commit either **.env** file.

The frontend **.env** defaults to live API mode:

```dotenv
VITE_DATA_MODE=live
VITE_API_BASE_URL=/api
VITE_BACKEND_ORIGIN=http://localhost:8000
VITE_ALLOW_MOCK_FALLBACK=false
```

**VITE_BACKEND_ORIGIN** is used by the Vite proxy and by server-side requests that need an absolute backend origin. The local frontend proxy forwards **/api** requests to the backend.

### 3. Set up and start the backend

From the repository root, create a virtual environment.

macOS / Linux:

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
cd backend
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
```

Then, with the virtual environment activated:

```bash
pip install -r requirements.txt
python -m app.cli init-db
python -m app.cli sync --days 30
python -m uvicorn app.main:app --reload --port 8000
```

Database initialisation creates the local SQLite schema and seeds the district and reviewed-station registry. The sync command attempts ingestion from configured providers and recomputes affected district history. It may report partial or failed provider status if credentials, upstream availability or data coverage are insufficient.

The API is available at **http://localhost:8000**. FastAPI's interactive API documentation is available at **http://localhost:8000/docs**.

### 4. Start the frontend

Open a second terminal at the repository root:

```bash
bun install --frozen-lockfile
bun run dev
```

Open the local URL printed by Vite (normally **http://localhost:5173**).

### Useful backend commands

Run these from the **backend/** directory with the virtual environment activated:

```bash
# Initialise the database and seed the registry
python -m app.cli init-db

# Fetch provider data and evaluate a 30-day window
python -m app.cli sync --days 30

# Recompute stored district history without fetching upstream data
python -m app.cli recompute --days 30

# Recompute history using a specific IST date as the anchor
python -m app.cli recompute --anchor-date 2026-11-08 --days 30
```

## API

All public endpoints are read-only.

| Method | Endpoint | Description |
|---|---|---|
| GET | **/api/health** | Provider configuration, sync status, freshness and overall live-data readiness |
| GET | **/api/districts** | List tracked districts |
| GET | **/api/districts?state=Maharashtra** | Filter districts by state or Union Territory |
| GET | **/api/districts?q=nagpur** | Search district, state or district ID |
| GET | **/api/snapshots** | List district snapshots |
| GET | **/api/snapshots?anchor_date=2026-11-08** | Evaluate snapshots for an IST anchor date |
| GET | **/api/snapshots/{district_id}** | Retrieve one district snapshot |

The districts and snapshots endpoints also support **state** and **q** filters. Snapshot endpoints accept **anchor_date** in **YYYY-MM-DD** format, interpreted as an IST evaluation date.

A district snapshot includes:

- District identity and location.
- AQI and dominant pollutant, or **null** when unavailable.
- Latest contributing observation timestamp.
- Source attribution and reporting-station count.
- Coverage and quality classification.
- Pollutant concentrations and sub-indices.
- Daily history and explicit caveats.
- An **isIllustrative** flag; backend API responses are not mock data.

**liveDataReady** is deliberately stricter than API availability. It becomes true only when at least one configured provider has a successful sync and fresh valid observations, and at least one district has a fresh district evaluation. A successful **/api/health** response by itself does not imply that live AQI values are available.

## Frontend data modes

The frontend uses the same service interface for live API data and local illustrative data.

| Setting | Behaviour |
|---|---|
| **VITE_DATA_MODE=live** | Uses the FastAPI backend; this is the default |
| **VITE_DATA_MODE=mock** | Uses illustrative mock data for offline UI development |
| **VITE_ALLOW_MOCK_FALLBACK=true** | Allows mock fallback only outside production; keep disabled for normal live-data verification |

Production builds block mock fallback. Do not use illustrative data to represent measured air quality.

## Tests and checks

### Backend

Run from **backend/**:

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

The backend suite covers API contracts, district registry behaviour, pollutant calculations, provider adapters, unit normalisation, station-first aggregation, historical evaluation and regression cases.

### Frontend

Run from the repository root:

```bash
bun run lint
bun run typecheck
bun run test
bun run build
```

GitHub Actions runs the backend test suite and these frontend checks on pushes and pull requests targeting **main**.

## Current limitations

- The registry contains 30 tracked districts; it is not a complete registry of every district in India.
- Monitoring-station availability and data completeness vary by district and provider.
- A district may correctly return an unavailable AQI when it has no verified stations or insufficient valid observations.
- Provider credentials, rate limits, source coverage and upstream outages can affect ingestion.
- The district metric is a project-defined composite and must not be described as an official CPCB district-level AQI.
- The Docker Compose configuration is present, but it currently does not forward **XKDR_API_KEY** and **XKDR_API_BASE_URL** to its containers. XKDR-backed ingestion through Compose therefore needs that configuration corrected first.
- No production deployment URL is documented in this repository.
- The repository does not currently include a root **LICENSE** file.

## Project status

District Air Scope is under active development. The priority is data correctness, traceable source attribution, explicit handling of unavailable measurements and reproducible evaluation—not filling every district card with a number.

Contributions, bug reports and methodology reviews are welcome. Please include the affected endpoint or data source, a reproducible example where possible, and the expected behaviour.

---

**Repository:** [github.com/siddhant3895/district-air-scope](https://github.com/siddhant3895/district-air-scope)  
**Methodology:** [backend/METHODOLOGY.md](backend/METHODOLOGY.md)
