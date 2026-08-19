export type ChartUnit = "count" | "percent" | "currency" | "duration" | "ms";

/** Axis and tooltip value formatting. `compact` is for axis ticks, where
 *  "1.2k" beats "1200" at 12px. */
export function formatValue(
  value: number,
  unit: ChartUnit = "count",
  opts: { compact?: boolean; currency?: string; locale?: string } = {},
): string {
  const { compact = false, currency = "GBP", locale = "en-GB" } = opts;

  switch (unit) {
    case "percent":
      return `${(value * 100).toFixed(compact ? 0 : 1)}%`;
    case "currency":
      return new Intl.NumberFormat(locale, {
        style: "currency",
        currency,
        notation: compact ? "compact" : "standard",
        maximumFractionDigits: compact ? 1 : 2,
      }).format(value);
    case "duration": {
      if (value < 60) return `${Math.round(value)}s`;
      const m = Math.floor(value / 60);
      return `${m}m ${Math.round(value % 60)}s`;
    }
    case "ms":
      return value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(2)}s`;
    default:
      return new Intl.NumberFormat(locale, {
        notation: compact ? "compact" : "standard",
        maximumFractionDigits: compact ? 1 : 0,
      }).format(value);
  }
}

export function formatAxisTick(
  t: string,
  granularity: "hour" | "day" | "week" | "month",
  timezone: string,
  locale = "en-GB",
): string {
  const d = new Date(t);
  switch (granularity) {
    case "hour":
      return new Intl.DateTimeFormat(locale, { hour: "2-digit", timeZone: timezone }).format(d);
    case "month":
      return new Intl.DateTimeFormat(locale, { month: "short", timeZone: timezone }).format(d);
    default:
      return new Intl.DateTimeFormat(locale, {
        day: "numeric",
        month: "short",
        timeZone: timezone,
      }).format(d);
  }
}
