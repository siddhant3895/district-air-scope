// CPCB National AQI category bands (India).
export interface AqiBand {
  key: "good" | "satisfactory" | "moderate" | "poor" | "verypoor" | "severe";
  label: string;
  min: number;
  max: number;
  bg: string;
  text: string;
  hint: string;
}

export const AQI_BANDS: AqiBand[] = [
  { key: "good", label: "Good", min: 0, max: 50, bg: "bg-aqi-good", text: "text-primary-foreground", hint: "Minimal impact" },
  { key: "satisfactory", label: "Satisfactory", min: 51, max: 100, bg: "bg-aqi-satisfactory", text: "text-foreground", hint: "Minor discomfort to sensitive people" },
  { key: "moderate", label: "Moderate", min: 101, max: 200, bg: "bg-aqi-moderate", text: "text-foreground", hint: "Discomfort for people with lung or heart conditions" },
  { key: "poor", label: "Poor", min: 201, max: 300, bg: "bg-aqi-poor", text: "text-foreground", hint: "Discomfort to most people on prolonged exposure" },
  { key: "verypoor", label: "Very Poor", min: 301, max: 400, bg: "bg-aqi-verypoor", text: "text-primary-foreground", hint: "Respiratory illness on prolonged exposure" },
  { key: "severe", label: "Severe", min: 401, max: 500, bg: "bg-aqi-severe", text: "text-primary-foreground", hint: "Affects healthy people; serious impact on others" },
];

export function aqiBand(aqi: number | null): AqiBand | null {
  if (aqi === null) return null;
  return AQI_BANDS.find((b) => aqi <= b.max) ?? AQI_BANDS[AQI_BANDS.length - 1]!;
}

export const AQI_CSS_VAR: Record<AqiBand["key"], string> = {
  good: "var(--aqi-good)",
  satisfactory: "var(--aqi-satisfactory)",
  moderate: "var(--aqi-moderate)",
  poor: "var(--aqi-poor)",
  verypoor: "var(--aqi-verypoor)",
  severe: "var(--aqi-severe)",
};

export function relativeTime(iso: string | null, now = new Date("2026-11-01T00:00:00Z")) {
  if (!iso) return "No data";
  const h = Math.round((now.getTime() - new Date(iso).getTime()) / 3600000);
  return h <= 1 ? "Updated ~1h ago" : `Updated ${h}h ago`;
}
