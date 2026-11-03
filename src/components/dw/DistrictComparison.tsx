import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import type { DistrictSnapshot } from "@/data";
import { AQI_CSS_VAR, aqiBand } from "@/lib/aqi";
import { cn } from "@/lib/utils";
import { AqiPill, IllustrativeLabel, QualityIndicator, SectionHeader } from "./primitives";

export function DistrictComparison({ snaps, activeId }: { snaps: DistrictSnapshot[]; activeId?: string }) {
  const [stateFilter, setStateFilter] = useState<string>("");
  const [query, setQuery] = useState<string>("");

  const availableStates = useMemo(() => {
    const s = new Set<string>();
    for (const snap of snaps) s.add(snap.district.state);
    return Array.from(s).sort((a, b) => a.localeCompare(b));
  }, [snaps]);

  const filteredAndSorted = useMemo(() => {
    const qNorm = query.trim().toLowerCase();
    return snaps
      .filter((s) => {
        if (stateFilter && s.district.state !== stateFilter) return false;
        if (
          qNorm &&
          !s.district.name.toLowerCase().includes(qNorm) &&
          !s.district.state.toLowerCase().includes(qNorm)
        ) {
          return false;
        }
        return true;
      })
      .sort((a, b) => (b.aqi ?? -1) - (a.aqi ?? -1) || a.district.name.localeCompare(b.district.name));
  }, [snaps, stateFilter, query]);

  const anyIllustrative = snaps.some((s) => s.isIllustrative);

  return (
    <section className="surface p-6">
      <SectionHeader eyebrow="Ranked by latest AQI" title="District comparison" right={<IllustrativeLabel show={anyIllustrative} />} />
      {snaps.length > 7 && (
        <div className="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter district or state…"
            aria-label="Filter districts by name or state"
            className="rounded-md border border-input bg-background px-3 py-1.5 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <select
            value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}
            aria-label="Filter districts by state"
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All states & UTs ({snaps.length})</option>
            {availableStates.map((st) => (
              <option key={st} value={st}>
                {st}
              </option>
            ))}
          </select>
        </div>
      )}
      {filteredAndSorted.length === 0 ? (
        <p className="py-6 text-center text-xs text-muted-foreground">No districts match the current filter.</p>
      ) : (
        <ul className="max-h-[28rem] divide-y overflow-y-auto pr-1">
          {filteredAndSorted.map((s, i) => {
            const b = aqiBand(s.aqi);
            return (
              <li key={s.district.id}>
                <Link
                  to="/districts/$districtId"
                  params={{ districtId: s.district.id }}
                  className={cn(
                    "grid grid-cols-[1.5rem_1fr_auto] items-center gap-3 py-3 transition-colors hover:bg-muted/60 sm:grid-cols-[1.5rem_minmax(8rem,1fr)_2fr_auto_auto] sm:gap-4",
                    activeId === s.district.id && "bg-muted/60"
                  )}
                >
                  <span className="pl-1 text-xs tabular-nums text-muted-foreground">{i + 1}</span>
                  <span>
                    <span className="block text-sm font-medium">{s.district.name}</span>
                    <span className="block text-xs text-muted-foreground">{s.district.state}</span>
                  </span>
                  <span className="hidden h-1.5 rounded-sm bg-muted sm:block" aria-hidden>
                    {b && s.aqi !== null && (
                      <span
                        className="block h-full rounded-sm"
                        style={{ width: `${Math.min(100, (s.aqi / 500) * 100)}%`, background: AQI_CSS_VAR[b.key] }}
                      />
                    )}
                  </span>
                  <span className="flex items-center gap-2 justify-self-end">
                    <span className="w-9 text-right font-display text-xl tabular-nums">{s.aqi ?? "—"}</span>
                    <AqiPill aqi={s.aqi} />
                  </span>
                  <span className="hidden pr-1 sm:block">
                    <QualityIndicator level={s.quality} compact />
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
