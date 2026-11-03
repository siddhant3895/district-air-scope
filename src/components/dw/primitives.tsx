import { Clock, Database, FlaskConical, ShieldCheck, ShieldAlert, ShieldQuestion, ShieldOff } from "lucide-react";
import { DATA_IS_ILLUSTRATIVE, type QualityLevel } from "@/data";
import { aqiBand, referenceNow, relativeTime } from "@/lib/aqi";
import { cn } from "@/lib/utils";

export function IllustrativeLabel({ className, show = DATA_IS_ILLUSTRATIVE }: { className?: string; show?: boolean }) {
  if (!show) return null;
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-sm border border-terracotta/40 bg-accent px-2 py-0.5 text-[11px] font-medium text-terracotta", className)}>
      <FlaskConical className="size-3" aria-hidden /> Illustrative data — not live measurements
    </span>
  );
}

export function AqiPill({ aqi, size = "sm" }: { aqi: number | null; size?: "sm" | "lg" }) {
  const b = aqiBand(aqi);
  if (!b) return <span className="inline-flex rounded-sm border border-dashed px-2 py-0.5 text-xs text-muted-foreground">Unavailable</span>;
  return (
    <span className={cn("inline-flex items-center rounded-sm font-medium", b.bg, b.text, size === "lg" ? "px-2.5 py-1 text-sm" : "px-2 py-0.5 text-xs")}>
      {b.label}
    </span>
  );
}

export function FreshnessBadge({ updatedAt, source }: { updatedAt: string | null; source: string }) {
  const stale = updatedAt && (referenceNow().getTime() - new Date(updatedAt).getTime()) / 3600000 > 6;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
      <span className={cn("inline-flex items-center gap-1", stale && "text-terracotta")}>
        <Clock className="size-3.5" aria-hidden /> {relativeTime(updatedAt)}{stale ? " · stale" : ""}
      </span>
      <span className="inline-flex items-center gap-1">
        <Database className="size-3.5" aria-hidden /> {source}
      </span>
    </div>
  );
}

const Q: Record<QualityLevel, { label: string; Icon: typeof ShieldCheck; cls: string; bars: number }> = {
  high: { label: "High confidence", Icon: ShieldCheck, cls: "text-sage", bars: 3 },
  medium: { label: "Medium confidence", Icon: ShieldQuestion, cls: "text-muted-foreground", bars: 2 },
  low: { label: "Low confidence", Icon: ShieldAlert, cls: "text-terracotta", bars: 1 },
  unavailable: { label: "No data", Icon: ShieldOff, cls: "text-muted-foreground", bars: 0 },
};

export function QualityIndicator({ level, coverage, compact }: { level: QualityLevel; coverage?: number; compact?: boolean }) {
  const q = Q[level];
  return (
    <span className={cn("inline-flex items-center gap-2 text-xs", q.cls)} title={q.label}>
      <span className="flex items-end gap-0.5" aria-hidden>
        {[1, 2, 3].map((i) => (
          <span key={i} className={cn("w-1 rounded-[1px]", i <= q.bars ? "bg-current" : "bg-border")} style={{ height: 4 + i * 3 }} />
        ))}
      </span>
      {!compact && <span className="font-medium">{q.label}</span>}
      {coverage !== undefined && level !== "unavailable" && <span className="text-muted-foreground">{Math.round(coverage * 100)}% coverage</span>}
      <span className="sr-only">{q.label}</span>
    </span>
  );
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-md border border-dashed bg-muted/50 px-6 py-12 text-center">
      <ShieldOff className="mb-3 size-6 text-muted-foreground" aria-hidden />
      <p className="font-display text-xl">{title}</p>
      <p className="mt-1 max-w-sm text-sm text-muted-foreground">{body}</p>
    </div>
  );
}

export function SectionHeader({ eyebrow, title, right }: { eyebrow?: string; title: string; right?: React.ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        {eyebrow && <p className="eyebrow mb-1">{eyebrow}</p>}
        <h2 className="text-2xl md:text-3xl">{title}</h2>
      </div>
      {right}
    </div>
  );
}
