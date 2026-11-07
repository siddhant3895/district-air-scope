# District Air Scope

Create a new project named DistrictWatch and build its initial FRONTEND DESIGN PROTOTYPE only. This is an air-quality monitoring platform focused initially on district-level AQI in India. Do not provision a database or invent a backend/API integration; use clearly marked illustrative mock data only and keep data access modular so we can connect a separate backend later.

VISUAL DIRECTION: warm editorial data platform. Cream-white, refined, modern, polished and readable. Take inspiration from customer-health dashboard information hierarchy, AssetWise warm styling, and editorial portfolio typography/spacing. This should have a distinct DistrictWatch identity, not copy a template.

TOKENS: page background #F7F5EF; surfaces #FFFEFA; warm charcoal #25251F; muted sage #87917B; terracotta #B96B50; thin warm-gray borders. Use Manrope (display headings) paired with Inter (UI/body). Generous whitespace, intentional grids, restrained subtle shadows. Avoid generic blue SaaS, glassmorphism, excessive gradients, over-rounded cards, and decorative animation. AQI severity colors must remain distinct, understandable, and accessible.

BUILD THESE ROUTES/SCREENS:
1) Overview dashboard: top navigation/brand, district selector, latest AQI summary panel, historical trend chart, district comparison, freshness/source attribution and data quality indicators.
2) District detail view: district name and location, current AQI summary, trend chart, pollutant breakdown, data coverage/source attribution, and caveat/quality information.
3) Methodology & data quality page: data source notes, coverage, freshness, missing-data and limitations sections. Don't fabricate claims; use placeholders explicitly marked as sample content.

COMPONENT STRUCTURE: reusable app shell/nav, AQI summary card, trend chart, district comparison card/table, freshness/source badge, quality indicator, district selector. Use clean component boundaries and a modular mock-data service/interface for later API integration. Chart axes/labels should be legible; ensure responsive desktop and mobile layouts. Use an empty/unavailable state where suitable. Add visible 'Illustrative data — not live measurements' labeling anywhere mock values appear.

Deliver a visually coherent, functional clickable frontend prototype with navigation between screens and basic district selection. Prioritize UI quality and responsiveness over backend features.

This project was built with [Lovable](https://lovable.dev).

**Live app**: https://district-air-scope.lovable.app

## Build with Lovable

Continue developing this project in the [Lovable editor](https://lovable.dev/projects/28ad557b-2a66-4707-8578-1bd55faa2851).

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: every change made in Lovable is committed straight to this repository.
- **Full ownership**: this code is yours. Push to `main` on GitHub and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
npm i
npm run dev
```

## Frontend & Backend API Configuration

DistrictWatch connects to the Python FastAPI backend (`backend/`) in live mode by default (`VITE_DATA_MODE=live`).

### Frontend Environment Variables (`.env`)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `VITE_DATA_MODE` | `live` | Set to `live` to query the FastAPI backend, or `mock` for offline illustrative preview. |
| `VITE_API_BASE_URL` | `/api` | Base URL used by the client/browser adapter (`src/data/api-service.ts`). Use `/api` when reverse-proxied from the same origin (or during `vite dev`), or set to an absolute URL (e.g., `https://api.districtwatch.example.com/api`) when the backend is hosted on a separate origin. |
| `VITE_BACKEND_ORIGIN` | `http://localhost:8000` | **SSR & Proxy Backend Origin**: Required whenever `VITE_API_BASE_URL` is a relative path (`/api`). During server-side rendering (SSR) in TanStack Start, Node.js `fetch()` cannot resolve relative URLs without an origin, so `src/data/api-service.ts` prepends `VITE_BACKEND_ORIGIN` (e.g., `http://localhost:8000` locally or `http://backend-api:8000` in Docker). `vite.config.ts` also uses `VITE_BACKEND_ORIGIN` for the `/api` dev proxy. |
| `VITE_ALLOW_MOCK_FALLBACK` | `false` | Must remain `false` in staging and production; production builds (`import.meta.env.PROD`) strictly forbid mock fallback. |

### Backend Data Provider Environment Variables (`backend/.env`)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `XKDR_API_KEY` | *(empty)* | Bearer token for the [India Air Quality Database by XKDR Forum](https://airquality.xkdr.org/) (CPCB CAAQM + US Diplomatic Post hourly readings, CC BY 4.0). Register for a free personal key at `https://airquality.xkdr.org/signup` (or use the public demo key `aqi_demo_wbf92Qx21zX-Wa_Tg8Dx1nXe` for 2024 archive data). |
| `XKDR_API_BASE_URL` | `https://airquality.xkdr.org` | Base URL for XKDR API v1 (`/v1/meta`, `/v1/stations`, `/v1/parameters`, `/v1/measurements`). |
| `OPENAQ_API_KEY` | *(empty)* | API key for [OpenAQ v3](https://explore.openaq.org/register) (`X-API-Key` header). |
| `DATA_GOV_IN_API_KEY` | *(empty)* | Optional API key for CPCB OGD (`data.gov.in`). |
| `DATABASE_URL` | `sqlite:///./districtwatch.db` | SQLite database path used for station registry, raw observations, station AQI evaluations, and 30-day district history. |
