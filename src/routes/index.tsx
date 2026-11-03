import { createFileRoute, useNavigate, useRouter } from "@tanstack/react-router";
import {
  airQuality,
  DATA_IS_ILLUSTRATIVE,
  type DistrictSnapshot,
  type HealthResponse,
} from "@/data";
import { AppShell } from "@/components/dw/AppShell";
import { AqiSummaryCard } from "@/components/dw/AqiSummaryCard";
import { TrendChart } from "@/components/dw/TrendChart";
import { DistrictComparison } from "@/components/dw/DistrictComparison";
import { DistrictSelector } from "@/components/dw/DistrictSelector";
import { EmptyState, QualityIndicator } from "@/components/dw/primitives";

interface OverviewLoaderData {
  snaps: DistrictSnapshot[];
  active: DistrictSnapshot | null;
  health: HealthResponse | null;
  error: string | null;
}

export const Route = createFileRoute("/")({
  validateSearch: (s: Record<string, unknown>): { d?: string } =>
    typeof s["d"] === "string" ? { d: s["d"] } : {},
  loaderDeps: ({ search }) => ({ d: search.d ?? "new-delhi" }),
  loader: async ({ deps }): Promise<OverviewLoaderData> => {
    try {
      const [snaps, selected, health] = await Promise.all([
        airQuality.listSnapshots(),
        airQuality.getSnapshot(deps.d),
        airQuality.getHealth ? airQuality.getHealth() : Promise.resolve(null),
      ]);
      const active = selected ?? snaps.find((s) => s.district.id === deps.d) ?? snaps[0] ?? null;
      return { snaps, active, health, error: null };
    } catch (err) {
      return {
        snaps: [],
        active: null,
        health: null,
        error: err instanceof Error ? err.message : "Unable to reach the DistrictWatch API.",
      };
    }
  },
  head: () => ({
    meta: [
      { title: "DistrictWatch — District air quality overview" },
      {
        name: "description",
        content:
          "Nationwide district-level AQI in India: latest readings, 30-day trends, comparison and data quality.",
      },
      { property: "og:title", content: "DistrictWatch — District air quality overview" },
      {
        property: "og:description",
        content: "District-level AQI in India with trends, comparison and data quality signals.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Overview,
});

function Overview() {
  const { snaps, active, health, error } = Route.useLoaderData();
  const navigate = useNavigate({ from: "/" });
  const router = useRouter();

  if (error || !active || snaps.length === 0) {
    return (
      <AppShell>
        <div className="mb-10">
          <p className="eyebrow mb-2">Overview · India</p>
          <h1 className="max-w-2xl text-4xl leading-[1.05] md:text-6xl">
            The air, read district by district.
          </h1>
        </div>
        <div className="surface p-8">
          <EmptyState
            title="Live API data unavailable"
            body={
              error
                ? `Could not load district snapshots from the backend API (${error}). Ensure the FastAPI server is running on port 8000.`
                : "No district snapshots were returned by the backend."
            }
          />
          <div className="mt-4 flex justify-center">
            <button
              type="button"
              onClick={() => router.invalidate()}
              className="rounded-md bg-primary px-4 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              Retry connection
            </button>
          </div>
        </div>
      </AppShell>
    );
  }

  const reporting = snaps.filter((s) => s.aqi !== null).length;
  const avgCoveragePct =
    snaps.length > 0
      ? Math.round((snaps.reduce((a, s) => a + s.coverage, 0) / snaps.length) * 100)
      : 0;

  return (
    <AppShell>
      <div className="mb-10 grid gap-6 md:grid-cols-[1fr_auto] md:items-end">
        <div>
          <p className="eyebrow mb-2">Overview · India</p>
          <h1 className="max-w-2xl text-4xl leading-[1.05] md:text-6xl">
            The air, read district by district.
          </h1>
        </div>
        <DistrictSelector
          districts={snaps.map((s) => s.district)}
          value={active.district.id}
          onChange={(d) => navigate({ search: { d } })}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <AqiSummaryCard snap={active} showLink />
        </div>
        <aside className="surface flex flex-col p-6">
          <p className="eyebrow mb-4">Network data quality</p>
          <dl className="grid grid-cols-2 gap-5">
            <Stat label="Districts tracked" value={snaps.length} />
            <Stat label="Reporting" value={`${reporting}/${snaps.length}`} />
            <Stat label="Stations" value={snaps.reduce((a, s) => a + s.stations, 0)} />
            <Stat label="Avg coverage" value={`${avgCoveragePct}%`} />
          </dl>
          <ul className="mt-6 space-y-2 border-t pt-4">
            {(["high", "medium", "low", "unavailable"] as const).map((q) => (
              <li key={q} className="flex items-center justify-between">
                <QualityIndicator level={q} />
                <span className="text-sm tabular-nums">
                  {snaps.filter((s) => s.quality === q).length}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-auto pt-6 text-xs text-muted-foreground">
            {active.isIllustrative || DATA_IS_ILLUSTRATIVE
              ? "Illustrative data — not live measurements."
              : health?.liveDataReady
                ? "Live CPCB NAQI · 16h completeness window."
                : "Station-first CPCB NAQI · 16h completeness window."}
          </p>
        </aside>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-5">
        <div className="lg:col-span-3">
          <TrendChart
            history={active.history}
            title={`${active.district.name}, 30 days`}
            isIllustrative={active.isIllustrative}
          />
        </div>
        <div className="lg:col-span-2">
          <DistrictComparison snaps={snaps} activeId={active.district.id} />
        </div>
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
