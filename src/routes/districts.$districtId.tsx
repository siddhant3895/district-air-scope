import { createFileRoute, Link, notFound, useNavigate } from "@tanstack/react-router";
import { AlertTriangle, ArrowLeft } from "lucide-react";
import { airQuality } from "@/data";
import { AppShell } from "@/components/dw/AppShell";
import { AqiSummaryCard } from "@/components/dw/AqiSummaryCard";
import { TrendChart } from "@/components/dw/TrendChart";
import { DistrictSelector } from "@/components/dw/DistrictSelector";
import { AqiPill, EmptyState, FreshnessBadge, IllustrativeLabel, QualityIndicator, SectionHeader } from "@/components/dw/primitives";

export const Route = createFileRoute("/districts/$districtId")({
  loader: async ({ params }) => {
    const [snap, districts] = await Promise.all([airQuality.getSnapshot(params.districtId), airQuality.listDistricts()]);
    if (!snap) throw notFound();
    return { snap, districts };
  },
  head: ({ loaderData }) => {
    const n = loaderData?.snap.district.name ?? "District";
    return {
      meta: [
        { title: `${n} air quality — DistrictWatch` },
        { name: "description", content: `AQI, trend, pollutant breakdown and data coverage for ${n} (prototype).` },
        { property: "og:title", content: `${n} air quality — DistrictWatch` },
        { property: "og:description", content: `District AQI detail for ${n}, with source and quality caveats.` },
        { property: "og:type", content: "website" },
        { name: "twitter:card", content: "summary_large_image" },
      ],
    };
  },
  notFoundComponent: () => (
    <AppShell><EmptyState title="District not found" body="This district isn't in the sample set." /></AppShell>
  ),
  component: DistrictDetail,
});

function DistrictDetail() {
  const { snap, districts } = Route.useLoaderData();
  const navigate = useNavigate();
  const maxSub = Math.max(1, ...snap.pollutants.map((p) => p.subIndex ?? 0));

  return (
    <AppShell>
      <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <Link to="/" search={{ d: snap.district.id }} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-4" /> Overview
        </Link>
        <DistrictSelector districts={districts} value={snap.district.id} onChange={(id) => navigate({ to: "/districts/$districtId", params: { districtId: id } })} />
      </div>
      <h1 className="sr-only">{snap.district.name} district air quality</h1>

      <AqiSummaryCard snap={snap} />

      <div className="mt-6 grid gap-6 lg:grid-cols-5">
        <div className="lg:col-span-3"><TrendChart history={snap.history} /></div>
        <section className="surface p-6 lg:col-span-2">
          <SectionHeader eyebrow="Latest sub-indices" title="Pollutants" />
          {snap.aqi === null ? (
            <EmptyState title="No pollutant data" body="Stations reported no pollutant values." />
          ) : (
            <ul className="space-y-4">
              {snap.pollutants.map((p) => (
                <li key={p.key}>
                  <div className="mb-1 flex items-baseline justify-between text-sm">
                    <span className="font-medium">{p.key}{p.key === snap.dominant && <span className="ml-2 text-xs text-terracotta">dominant</span>}</span>
                    <span className="tabular-nums text-muted-foreground">{p.value === null ? "Not reported" : `${p.value} ${p.unit}`}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <div className="h-1.5 flex-1 rounded-sm bg-muted">
                      {p.subIndex !== null && <div className="h-full rounded-sm bg-foreground/70" style={{ width: `${(p.subIndex / maxSub) * 100}%` }} />}
                    </div>
                    <span className="w-24 text-right"><AqiPill aqi={p.subIndex} /></span>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <IllustrativeLabel className="mt-6" />
        </section>
      </div>

      <div className="mt-6 grid gap-6 md:grid-cols-2">
        <section className="surface p-6">
          <SectionHeader eyebrow="Provenance" title="Coverage & source" />
          <dl className="grid grid-cols-2 gap-5 text-sm">
            <div><dt className="text-xs text-muted-foreground">Stations</dt><dd className="font-display text-3xl">{snap.stations}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Hourly coverage</dt><dd className="font-display text-3xl">{Math.round(snap.coverage * 100)}%</dd></div>
            <div className="col-span-2"><dt className="mb-1 text-xs text-muted-foreground">Freshness & source</dt><dd><FreshnessBadge updatedAt={snap.updatedAt} source={snap.source} /></dd></div>
            <div className="col-span-2"><dt className="mb-1 text-xs text-muted-foreground">Quality</dt><dd><QualityIndicator level={snap.quality} /></dd></div>
          </dl>
        </section>
        <section className="surface p-6">
          <SectionHeader eyebrow="Read with care" title="Caveats" />
          {snap.caveats.length === 0 ? (
            <p className="text-sm text-muted-foreground">No caveats flagged for this sample window.</p>
          ) : (
            <ul className="space-y-3">
              {snap.caveats.map((c) => (
                <li key={c} className="flex gap-2 text-sm"><AlertTriangle className="mt-0.5 size-4 shrink-0 text-terracotta" aria-hidden />{c}</li>
              ))}
            </ul>
          )}
          <Link to="/methodology" className="mt-5 inline-block text-sm font-medium text-terracotta hover:underline">How we assess quality →</Link>
        </section>
      </div>
    </AppShell>
  );
}
