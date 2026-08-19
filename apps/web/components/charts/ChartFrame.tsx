"use client";

import { useId, useState } from "react";

import type { AnalyticsSeries } from "@/lib/api/types";
import { formatAxisTick, formatValue, type ChartUnit } from "@/lib/charts/format";
import { cn } from "@/lib/utils";

/**
 * The shared chart frame: figure, caption, the `role="img"` label, and the
 * table view toggle.
 *
 * The table is not decoration. An <svg> of <path> elements is NOTHING to a
 * screen reader, so the table is how a non-sighted user reads the data at
 * all. `<details>` keeps it out of the way for sighted users without hiding
 * it from assistive tech.
 */
export function ChartFrame({
  title,
  description,
  ariaLabel,
  children,
  table,
  height = 280,
  className,
}: {
  title: string;
  description?: string;
  ariaLabel: string;
  children: React.ReactNode;
  table: React.ReactNode;
  height?: number;
  className?: string;
}) {
  const titleId = useId();
  return (
    <figure className={cn("space-y-3", className)}>
      <figcaption>
        <h3 id={titleId} className="text-sm font-medium text-fg">
          {title}
        </h3>
        {description && <p className="text-xs text-fg-muted">{description}</p>}
      </figcaption>

      <div style={{ height }} role="img" aria-label={ariaLabel}>
        {children}
      </div>

      <details className="text-sm">
        <summary className="cursor-pointer text-xs text-fg-muted hover:text-fg">
          View as table
        </summary>
        <div className="scroll-x mt-2">{table}</div>
      </details>
    </figure>
  );
}

/** The tabular relief for a time series. */
export function SeriesTable({
  series,
  granularity,
  timezone,
  unit,
}: {
  series: AnalyticsSeries[];
  granularity: "hour" | "day" | "week" | "month";
  timezone: string;
  unit: ChartUnit;
}) {
  const times = [...new Set(series.flatMap((s) => s.points.map((p) => p.t)))].sort();
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="border-b border-line">
          <th scope="col" className="py-1.5 pr-3 text-left font-medium text-fg-subtle">
            Date
          </th>
          {series.map((s) => (
            <th key={s.key} scope="col" className="py-1.5 pr-3 text-right font-medium text-fg-subtle">
              {s.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {times.map((t) => (
          <tr key={t} className="border-b border-line/50">
            <th scope="row" className="py-1.5 pr-3 text-left font-normal text-fg-muted">
              {formatAxisTick(t, granularity, timezone)}
            </th>
            {series.map((s) => (
              <td key={s.key} className="tabular py-1.5 pr-3 text-right text-fg">
                {formatValue(s.points.find((p) => p.t === t)?.v ?? 0, unit)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** A one-line summary a screen reader hears from `aria-label`. */
export function describeSeries(
  title: string,
  series: AnalyticsSeries[],
  unit: ChartUnit,
): string {
  const parts = series.map(
    (s) => `${s.label}: ${formatValue(s.points.at(-1)?.v ?? 0, unit)} most recent`,
  );
  return `${title}. ${series.length} series. ${parts.join("; ")}. Full data in the table below.`;
}

/** Shared tooltip. Values are tabular so they do not jitter between points. */
export function ChartTooltip({
  active,
  payload,
  label,
  unit,
  granularity,
  timezone,
}: {
  active?: boolean;
  payload?: Array<{ name?: string; value?: number; color?: string; dataKey?: string }>;
  label?: string;
  unit: ChartUnit;
  granularity: "hour" | "day" | "week" | "month";
  timezone: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-input border border-line bg-bg-raised px-3 py-2 shadow-overlay">
      <p className="mb-1 text-xs font-medium text-fg">
        {label ? formatAxisTick(String(label), granularity, timezone) : ""}
      </p>
      <ul className="space-y-0.5">
        {payload.map((p) => (
          <li key={p.dataKey ?? p.name} className="flex items-center gap-2 text-xs">
            {/* The swatch carries identity; the LABEL is text in an ink token,
                never the series colour (which would fail contrast). */}
            <span
              aria-hidden="true"
              className="size-2 shrink-0 rounded-[2px]"
              style={{ background: p.color }}
            />
            <span className="text-fg-muted">{p.name}</span>
            <span className="tabular ml-auto font-medium text-fg">
              {formatValue(p.value ?? 0, unit)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** A toggle used by charts that also offer a table-only mode. */
export function useTableView() {
  return useState(false);
}
