import { createFileRoute } from "@tanstack/react-router";
import { AppShell } from "@/components/dw/AppShell";
import { QualityIndicator } from "@/components/dw/primitives";
import { DATA_IS_ILLUSTRATIVE } from "@/data";
import { AQI_BANDS } from "@/lib/aqi";

export const Route = createFileRoute("/methodology")({
  head: () => ({
    meta: [
      { title: "Methodology & data quality — DistrictWatch" },
      {
        name: "description",
        content:
          "How DistrictWatch handles CPCB, XKDR Forum, and OpenAQ sources, coverage, freshness, missing data and station-first aggregation.",
      },
      { property: "og:title", content: "Methodology & data quality — DistrictWatch" },
      {
        property: "og:description",
        content: "Sources, coverage, freshness and limitations behind DistrictWatch AQI figures.",
      },
      { property: "og:type", content: "article" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Methodology,
});

const SECTIONS = [
  {
    id: "sources",
    title: "Data sources & station-first aggregation",
    body: "Readings originate from Continuous Ambient Air Quality Monitoring Stations (CAAQMS) operated by the Central Pollution Control Board (CPCB), State Pollution Control Boards (SPCBs), IITM, and IMD, accessed via the India Air Quality Database maintained by XKDR Forum (airquality.xkdr.org, CC BY 4.0), OpenAQ API v3, and the Open Government Data Platform India (data.gov.in, Resource 3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69 under GODL-India). Sub-indices (24-hour window for PM2.5, PM10, NO2, SO2; 8-hour window for CO and O3) and station AQIs are calculated per station first. District figures represent the DistrictWatch composite mean of valid station-level AQIs — not an official CPCB administrative district index.",
  },
  {
    id: "coverage",
    title: "Coverage & district boundaries",
    body: "Each station is mapped to its administrative district through an audited station registry, excluding stations in neighboring districts of the same metropolitan area. Hourly coverage measures valid observed station-pollutant-hours against expected window hours across all verified stations in the district: 4 × 24h (PM2.5, PM10, NO2, SO2) + 2 × 8h (O3, CO) = 112 expected pollutant-hours per station.",
  },
  {
    id: "freshness",
    title: "Freshness",
    body: "Upstream station feeds are synced on a 30-minute cadence. Any district whose latest valid station measurement is older than 6 hours is flagged as stale in the interface.",
  },
  {
    id: "missing",
    title: "Missing data & CPCB completeness rules",
    body: "Following CPCB National Air Quality Index rules, a station requires at least 16 valid hours in a 24-hour window (and at least 6 of 8 hours in the active 8-hour window for CO and O3) across at least 3 pollutants including PM2.5 or PM10. Missing days and incomplete windows are preserved as null gaps in charts and are never interpolated or estimated.",
  },
  {
    id: "limitations",
    title: "Limitations",
    body: "Roughly 40% of Indian districts (including Ladakh, Lakshadweep, and Goa) lack continuous CAAQMS monitors and are reported honestly as Unavailable. Even in monitored districts, stations are concentrated in urban centers, so a district composite figure may not reflect hyper-local rural or roadside exposure.",
  },
];

function Methodology() {
  return (
    <AppShell>
      <div className="grid gap-12 lg:grid-cols-[14rem_1fr]">
        <nav className="hidden lg:block" aria-label="Sections">
          <ul className="sticky top-8 space-y-2 text-sm">
            {[
              ...SECTIONS,
              { id: "quality", title: "Quality levels" },
              { id: "scale", title: "AQI scale" },
            ].map((s) => (
              <li key={s.id}>
                <a href={`#${s.id}`} className="text-muted-foreground hover:text-foreground">
                  {s.title}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <article className="max-w-2xl">
          <p className="eyebrow mb-2">Methodology</p>
          <h1 className="text-4xl leading-[1.05] md:text-6xl">How we read the numbers.</h1>
          {DATA_IS_ILLUSTRATIVE && (
            <p className="mt-5 rounded-md border border-terracotta/40 bg-accent p-4 text-sm text-terracotta">
              Illustrative mode active. Switch VITE_DATA_MODE=live to query the DistrictWatch
              FastAPI backend.
            </p>
          )}

          {SECTIONS.map((s, i) => (
            <section key={s.id} id={s.id} className="scroll-mt-8 border-b py-8">
              <p className="eyebrow mb-1">0{i + 1}</p>
              <h2 className="text-3xl">{s.title}</h2>
              <p className="mt-3 leading-relaxed text-muted-foreground">{s.body}</p>
            </section>
          ))}

          <section id="quality" className="scroll-mt-8 border-b py-8">
            <p className="eyebrow mb-1">06</p>
            <h2 className="text-3xl">Quality levels</h2>
            <ul className="mt-4 space-y-3 text-sm">
              <li className="flex flex-wrap gap-3">
                <QualityIndicator level="high" />
                <span className="text-muted-foreground">
                  ≥ 85% window coverage across 3+ reporting stations
                </span>
              </li>
              <li className="flex flex-wrap gap-3">
                <QualityIndicator level="medium" />
                <span className="text-muted-foreground">
                  60–85% window coverage across 3+ reporting stations
                </span>
              </li>
              <li className="flex flex-wrap gap-3">
                <QualityIndicator level="low" />
                <span className="text-muted-foreground">
                  &lt; 60% window coverage or ≤ 2 reporting stations
                </span>
              </li>
              <li className="flex flex-wrap gap-3">
                <QualityIndicator level="unavailable" />
                <span className="text-muted-foreground">
                  No valid station readings received in window
                </span>
              </li>
            </ul>
          </section>

          <section id="scale" className="scroll-mt-8 py-8">
            <p className="eyebrow mb-1">07</p>
            <h2 className="text-3xl">AQI scale</h2>
            <p className="mt-3 text-sm text-muted-foreground">
              Categories follow India's National Air Quality Index bands.
            </p>
            <ul className="mt-4 overflow-hidden rounded-md border">
              {AQI_BANDS.map((b) => (
                <li
                  key={b.key}
                  className="grid grid-cols-[7.5rem_5rem_1fr] items-center gap-3 border-b bg-card px-3 py-2.5 text-sm last:border-b-0"
                >
                  <span className={`rounded-sm px-2 py-0.5 text-xs font-medium ${b.bg} ${b.text}`}>
                    {b.label}
                  </span>
                  <span className="tabular-nums">
                    {b.min}–{b.max}
                  </span>
                  <span className="text-muted-foreground">{b.hint}</span>
                </li>
              ))}
            </ul>
          </section>
        </article>
      </div>
    </AppShell>
  );
}
