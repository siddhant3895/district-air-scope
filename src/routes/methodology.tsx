import { createFileRoute } from "@tanstack/react-router";
import { AppShell } from "@/components/dw/AppShell";
import { QualityIndicator } from "@/components/dw/primitives";
import { AQI_BANDS } from "@/lib/aqi";

export const Route = createFileRoute("/methodology")({
  head: () => ({
    meta: [
      { title: "Methodology & data quality — DistrictWatch" },
      { name: "description", content: "How DistrictWatch handles sources, coverage, freshness, missing data and limitations (sample content)." },
      { property: "og:title", content: "Methodology & data quality — DistrictWatch" },
      { property: "og:description", content: "Sources, coverage, freshness and limitations behind DistrictWatch AQI figures." },
      { property: "og:type", content: "article" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Methodology,
});

const SECTIONS = [
  { id: "sources", title: "Data sources", body: "[Sample content] Name each upstream provider here, the station networks they operate, licence terms, and how readings are aggregated to district level." },
  { id: "coverage", title: "Coverage", body: "[Sample content] Describe how many districts and stations are covered, how stations are mapped to district boundaries, and which boundary dataset/version is used." },
  { id: "freshness", title: "Freshness", body: "[Sample content] State the expected update cadence and the threshold at which a reading is marked stale. In this prototype, readings older than 6 hours are flagged." },
  { id: "missing", title: "Missing data", body: "[Sample content] Explain how gaps are handled. This prototype shows missing days as gaps in charts and never interpolates or estimates absent values." },
  { id: "limitations", title: "Limitations", body: "[Sample content] List known limitations — e.g. sparse stations in rural districts, sensor calibration differences, and that a district figure may not reflect conditions at any specific location." },
];

function Methodology() {
  return (
    <AppShell>
      <div className="grid gap-12 lg:grid-cols-[14rem_1fr]">
        <nav className="hidden lg:block" aria-label="Sections">
          <ul className="sticky top-8 space-y-2 text-sm">
            {[...SECTIONS, { id: "quality", title: "Quality levels" }, { id: "scale", title: "AQI scale" }].map((s) => (
              <li key={s.id}><a href={`#${s.id}`} className="text-muted-foreground hover:text-foreground">{s.title}</a></li>
            ))}
          </ul>
        </nav>
        <article className="max-w-2xl">
          <p className="eyebrow mb-2">Methodology</p>
          <h1 className="text-4xl leading-[1.05] md:text-6xl">How we read the numbers.</h1>
          <p className="mt-5 rounded-md border border-terracotta/40 bg-accent p-4 text-sm text-terracotta">
            Sample content. The text on this page is placeholder copy for the design prototype and makes no claims about real data sources or methods.
          </p>

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
              <li className="flex flex-wrap gap-3"><QualityIndicator level="high" /><span className="text-muted-foreground">[Sample] ≥ 85% coverage, 3+ stations</span></li>
              <li className="flex flex-wrap gap-3"><QualityIndicator level="medium" /><span className="text-muted-foreground">[Sample] 60–85% coverage</span></li>
              <li className="flex flex-wrap gap-3"><QualityIndicator level="low" /><span className="text-muted-foreground">[Sample] &lt; 60% coverage or ≤ 2 stations</span></li>
              <li className="flex flex-wrap gap-3"><QualityIndicator level="unavailable" /><span className="text-muted-foreground">No readings received</span></li>
            </ul>
          </section>

          <section id="scale" className="scroll-mt-8 py-8">
            <p className="eyebrow mb-1">07</p>
            <h2 className="text-3xl">AQI scale</h2>
            <p className="mt-3 text-sm text-muted-foreground">Categories follow India's National Air Quality Index bands.</p>
            <ul className="mt-4 overflow-hidden rounded-md border">
              {AQI_BANDS.map((b) => (
                <li key={b.key} className="grid grid-cols-[7.5rem_5rem_1fr] items-center gap-3 border-b bg-card px-3 py-2.5 text-sm last:border-b-0">
                  <span className={`rounded-sm px-2 py-0.5 text-xs font-medium ${b.bg} ${b.text}`}>{b.label}</span>
                  <span className="tabular-nums">{b.min}–{b.max}</span>
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
