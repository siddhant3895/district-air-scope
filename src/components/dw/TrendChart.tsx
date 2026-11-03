import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Reading } from "@/data";
import { aqiBand, formatTrendDate } from "@/lib/aqi";
import { EmptyState, IllustrativeLabel, SectionHeader } from "./primitives";

export function TrendChart({
  history,
  title = "30-day trend",
  isIllustrative,
}: {
  history: Reading[];
  title?: string | undefined;
  isIllustrative?: boolean | undefined;
}) {
  const valid = history.filter((h) => h.aqi !== null);
  const missing = history.length - valid.length;
  return (
    <section className="surface p-6">
      <SectionHeader
        eyebrow="Daily mean AQI"
        title={title}
        right={<IllustrativeLabel show={isIllustrative} />}
      />
      {valid.length === 0 ? (
        <EmptyState
          title="No trend available"
          body="There are no verified readings in this 30-day window to chart."
        />
      ) : (
        <>
          <div
            className="h-64 md:h-72"
            role="img"
            aria-label={`AQI trend chart, ${valid.length} days with data`}
          >
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={history} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                <defs>
                  <linearGradient id="dwFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--terracotta)" stopOpacity={0.18} />
                    <stop offset="100%" stopColor="var(--terracotta)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="var(--border)" vertical={false} />
                <XAxis
                  dataKey="date"
                  tickFormatter={formatTrendDate}
                  tick={{ fill: "var(--muted-foreground)", fontSize: 12 }}
                  axisLine={{ stroke: "var(--border)" }}
                  tickLine={false}
                  minTickGap={28}
                />
                <YAxis
                  tick={{ fill: "var(--muted-foreground)", fontSize: 12 }}
                  axisLine={false}
                  tickLine={false}
                  width={44}
                />
                {[100, 200, 300].map((y) => (
                  <ReferenceLine
                    key={y}
                    y={y}
                    stroke="var(--sage)"
                    strokeDasharray="3 4"
                    label={{
                      value: aqiBand(y + 1)?.label ?? "",
                      position: "insideTopRight",
                      fill: "var(--muted-foreground)",
                      fontSize: 11,
                    }}
                  />
                ))}
                <Tooltip
                  contentStyle={{
                    background: "var(--card)",
                    border: "1px solid var(--border)",
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                  labelFormatter={(l) => formatTrendDate(String(l))}
                  formatter={(v) =>
                    v === null || v === undefined
                      ? ["Missing", "AQI"]
                      : [`${v} · ${aqiBand(Number(v))?.label}`, "AQI"]
                  }
                />
                <Area
                  type="monotone"
                  dataKey="aqi"
                  stroke="var(--terracotta)"
                  strokeWidth={2}
                  fill="url(#dwFill)"
                  connectNulls={false}
                  dot={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-3 text-xs text-muted-foreground">
            {missing > 0
              ? `${missing} of ${history.length} days missing — shown as gaps, not interpolated.`
              : `All ${history.length} days reported.`}
          </p>
        </>
      )}
    </section>
  );
}
