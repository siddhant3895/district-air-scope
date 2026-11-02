import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

const NAV = [
  { to: "/", label: "Overview" },
  { to: "/districts/$districtId", label: "Districts", params: { districtId: "new-delhi" } },
  { to: "/methodology", label: "Methodology" },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen">
      <div className="border-b border-terracotta/30 bg-accent px-4 py-1.5 text-center text-xs text-terracotta">
        Design prototype · All figures are illustrative data — not live measurements
      </div>
      <header className="border-b bg-background">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-5 py-4 md:px-8">
          <Link to="/" className="flex items-baseline gap-2">
            <span className="font-display text-2xl leading-none">DistrictWatch</span>
            <span className="hidden text-xs text-muted-foreground sm:inline">Air quality, district by district</span>
          </Link>
          <nav className="flex gap-1 text-sm">
            {NAV.map((n) => (
              <Link
                key={n.label}
                to={n.to}
                params={"params" in n ? n.params : undefined}
                activeOptions={{ exact: n.to === "/" , includeSearch: false }}
                className="rounded-md px-3 py-1.5 text-muted-foreground transition-colors hover:text-foreground data-[status=active]:bg-secondary data-[status=active]:text-foreground"
              >
                {n.label}
              </Link>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-5 py-10 md:px-8 md:py-14">{children}</main>
      <footer className="border-t">
        <div className="mx-auto flex max-w-6xl flex-wrap justify-between gap-2 px-5 py-6 text-xs text-muted-foreground md:px-8">
          <span>DistrictWatch prototype · AQI categories follow India's National AQI bands</span>
          <Link to="/methodology" className="hover:text-foreground">Methodology & data quality</Link>
        </div>
      </footer>
    </div>
  );
}
