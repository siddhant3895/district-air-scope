import { Link } from "@tanstack/react-router";
import { ArrowUpRight, MapPin } from "lucide-react";
import type { DistrictSnapshot } from "@/data";
import { AQI_BANDS, aqiBand } from "@/lib/aqi";
import { cn } from "@/lib/utils";
import {
  AqiPill,
  EmptyState,
  FreshnessBadge,
  IllustrativeLabel,
  QualityIndicator,
} from "./primitives";

export function AqiSummaryCard({
  snap,
  showLink,
}: {
  snap: DistrictSnapshot;
  showLink?: boolean | undefined;
}) {
  const b = aqiBand(snap.aqi);
  const unavailableBody =
    snap.caveats.length > 0
      ? `${snap.caveats[0]} We show nothing rather than estimate.`
      : "No readings were received for this district in the current evaluation window. We show nothing rather than estimate.";

  return (
    <section className="surface p-6 md:p-8" aria-label={`AQI summary for ${snap.district.name}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="eyebrow mb-1">Latest AQI</p>
          <h2 className="text-3xl md:text-4xl">{snap.district.name}</h2>
          <p className="mt-1 inline-flex items-center gap-1 text-sm text-muted-foreground">
            <MapPin className="size-3.5" aria-hidden /> {snap.district.state} ·{" "}
            {snap.district.lat.toFixed(2)}°N, {snap.district.lon.toFixed(2)}°E
          </p>
        </div>
        <IllustrativeLabel show={snap.isIllustrative} />
      </div>

      {snap.aqi === null ? (
        <div className="mt-6">
          <EmptyState title="AQI unavailable" body={unavailableBody} />
        </div>
      ) : (
        <div className="mt-8 grid gap-8 md:grid-cols-[auto_1fr] md:items-end">
          <div>
            <div className="flex items-baseline gap-3">
              <span className="font-display text-7xl leading-none md:text-8xl">{snap.aqi}</span>
              <span className="text-sm text-muted-foreground">AQI</span>
            </div>
            <div className="mt-3 flex items-center gap-2">
              <AqiPill aqi={snap.aqi} size="lg" />
              <span className="text-sm text-muted-foreground">
                Dominant: {snap.dominant ?? "—"}
              </span>
            </div>
          </div>
          <div>
            <p className="mb-2 text-sm text-muted-foreground">{b?.hint}</p>
            <div className="flex h-2 overflow-hidden rounded-sm" aria-hidden>
              {AQI_BANDS.map((x) => (
                <div key={x.key} className={cn("flex-1", x.bg, x.key !== b?.key && "opacity-30")} />
              ))}
            </div>
            <div className="mt-1 flex justify-between text-[11px] text-muted-foreground">
              <span>0</span>
              <span>100</span>
              <span>200</span>
              <span>300</span>
              <span>400</span>
              <span>500</span>
            </div>
          </div>
        </div>
      )}

      <div className="mt-8 flex flex-wrap items-center justify-between gap-4 border-t pt-4">
        <div className="flex flex-wrap items-center gap-4">
          <FreshnessBadge updatedAt={snap.updatedAt} source={snap.source} />
          <QualityIndicator level={snap.quality} coverage={snap.coverage} />
        </div>
        {showLink && (
          <Link
            to="/districts/$districtId"
            params={{ districtId: snap.district.id }}
            className="inline-flex items-center gap-1 text-sm font-medium text-terracotta hover:underline"
          >
            District detail <ArrowUpRight className="size-4" />
          </Link>
        )}
      </div>
    </section>
  );
}
