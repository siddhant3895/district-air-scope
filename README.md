# District Air Scope

Create a new project named DistrictWatch and build its initial FRONTEND DESIGN PROTOTYPE only. This is an air-quality monitoring platform focused initially on district-level AQI in India. Do not provision a database or invent a backend/API integration; use clearly marked illustrative mock data only and keep data access modular so we can connect a separate backend later.

VISUAL DIRECTION: warm editorial data platform. Cream-white, refined, modern, polished and readable. Take inspiration from customer-health dashboard information hierarchy, AssetWise warm styling, and editorial portfolio typography/spacing. This should have a distinct DistrictWatch identity, not copy a template.

TOKENS: page background #F7F5EF; surfaces #FFFEFA; warm charcoal #25251F; muted sage #87917B; terracotta #B96B50; thin warm-gray borders. Use Instrument Serif (display headings) paired with DM Sans (UI/body). Generous whitespace, intentional grids, restrained subtle shadows. Avoid generic blue SaaS, glassmorphism, excessive gradients, over-rounded cards, and decorative animation. AQI severity colors must remain distinct, understandable, and accessible.

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
