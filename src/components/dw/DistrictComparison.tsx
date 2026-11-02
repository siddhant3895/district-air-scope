import { Link } from "@tanstack/react-router";
import type { DistrictSnapshot } from "@/data";
import { AQI_CSS_VAR, aqiBand } from "@/lib/aqi";
import { cn } from "@/lib/utils";
import { AqiPill, IllustrativeLabel, QualityIndicator, SectionHeader } from "./primitives";

export function DistrictComparison({ snaps, activeId }: { snaps: DistrictSnapshot[]; activeId?: string }) {
  const sorted = [...snaps].sort((a, b) => (b.aqi ?? -1) - (a.aqi ?? -1));
  return (
    <section className="surface p-6">
      <SectionHeader eyebrow="Ranked by latest AQI" title="District comparison" right={<IllustrativeLabel />} />
      <ul className="divide-y">
        {sorted.map((s, i) => {
          const b = aqiBand(s.aqi);
          return (
            <li key={s.district.id}>
              <Link
                to="/districts/$districtId"
                params={{ districtId: s.district.id }}
                className={cn("grid grid-cols-[1.5rem_1fr_auto] items-center gap-3 py-3 transition-colors hover:bg-muted/60 sm:grid-cols-[1.5rem_minmax(8rem,1fr)_2fr_auto_auto] sm:gap-4", activeId === s.district.id && "bg-muted/60")}
              >
                <span className="pl-1 text-xs tabular-nums text-muted-foreground">{i + 1}</span>
                <span>
                  <span className="block text-sm font-medium">{s.district.name}</span>
                  <span className="block text-xs text-muted-foreground">{s.district.state}</span>
                </span>
                <span className="hidden h-1.5 rounded-sm bg-muted sm:block" aria-hidden>
                  {b && <span className="block h-full rounded-sm" style={{ width: `${(s.aqi! / 500) * 100}%`, background: AQI_CSS_VAR[b.key] }} />}
                </span>
                <span className="flex items-center gap-2 justify-self-end">
                  <span className="w-9 text-right font-display text-xl tabular-nums">{s.aqi ?? "—"}</span>
                  <AqiPill aqi={s.aqi} />
                </span>
                <span className="hidden pr-1 sm:block"><QualityIndicator level={s.quality} compact /></span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
