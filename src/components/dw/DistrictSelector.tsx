import { ChevronDown } from "lucide-react";
import type { District } from "@/data";

export function DistrictSelector({ districts, value, onChange }: { districts: District[]; value: string; onChange: (id: string) => void }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1.5 block">District</span>
      <span className="relative block">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full appearance-none rounded-md border border-input bg-card py-2.5 pl-3 pr-10 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-ring sm:w-72"
        >
          {districts.map((d) => (
            <option key={d.id} value={d.id}>{d.name} — {d.state}</option>
          ))}
        </select>
        <ChevronDown className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
      </span>
    </label>
  );
}
