import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { airQuality } from "@/data";
import { AppShell } from "@/components/dw/AppShell";
import { AqiSummaryCard } from "@/components/dw/AqiSummaryCard";
import { TrendChart } from "@/components/dw/TrendChart";
import { DistrictComparison } from "@/components/dw/DistrictComparison";
import { DistrictSelector } from "@/components/dw/DistrictSelector";
import { QualityIndicator } from "@/components/dw/primitives";

export const Route = createFileRoute("/")({
  validateSearch: (s: Record<string, unknown>) => ({ d: typeof s.d === "string" ? s.d : "new-delhi" }),
  loaderDeps: ({ search }) => ({ d: search.d }),
  loader: async ({ deps }) => {
    const [snaps, active] = await Promise.all([airQuality.listSnapshots(), airQuality.getSnapshot(deps.d)]);
    return { snaps, active: active ?? snaps[0] };
  },
  head: () => ({
    meta: [
      { title: "DistrictWatch — District air quality overview" },
      { name: "description", content: "Prototype dashboard for district-level AQI in India: latest readings, trends, comparison and data quality." },
      { property: "og:title", content: "DistrictWatch — District air quality overview" },
      { property: "og:description", content: "District-level AQI in India with trends, comparison and data quality signals." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Overview,
});

function Overview() {
  const { snaps, active } = Route.useLoaderData();
  const navigate = useNavigate({ from: "/" });
  const reporting = snaps.filter((s) => s.aqi !== null).length;

  return (
    <AppShell>
      <div className="mb-10 grid gap-6 md:grid-cols-[1fr_auto] md:items-end">
        <div>
          <p className="eyebrow mb-2">Overview · India</p>
          <h1 className="max-w-2xl text-4xl leading-[1.05] md:text-6xl">The air, read district by district.</h1>
        </div>
        <DistrictSelector districts={snaps.map((s) => s.district)} value={active.district.id} onChange={(d) => navigate({ search: { d } })} />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2"><AqiSummaryCard snap={active} showLink /></div>
        <aside className="surface flex flex-col p-6">
          <p className="eyebrow mb-4">Network data quality</p>
          <dl className="grid grid-cols-2 gap-5">
            <Stat label="Districts tracked" value={snaps.length} />
            <Stat label="Reporting" value={`${reporting}/${snaps.length}`} />
            <Stat label="Stations" value={snaps.reduce((a, s) => a + s.stations, 0)} />
            <Stat label="Avg coverage" value={`${Math.round((snaps.reduce((a, s) => a + s.coverage, 0) / snaps.length) * 100)}%`} />
          </dl>
          <ul className="mt-6 space-y-2 border-t pt-4">
            {(["high", "medium", "low", "unavailable"] as const).map((q) => (
              <li key={q} className="flex items-center justify-between">
                <QualityIndicator level={q} />
                <span className="text-sm tabular-nums">{snaps.filter((s) => s.quality === q).length}</span>
              </li>
            ))}
          </ul>
          <p className="mt-auto pt-6 text-xs text-muted-foreground">Illustrative data — not live measurements.</p>
        </aside>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-5">
        <div className="lg:col-span-3"><TrendChart history={active.history} title={`${active.district.name}, 30 days`} /></div>
        <div className="lg:col-span-2"><DistrictComparison snaps={snaps} activeId={active.district.id} /></div>
      </div>
    </AppShell>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="font-display text-3xl tabular-nums">{value}</dd>
    </div>
  );
}
