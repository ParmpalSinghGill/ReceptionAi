"use client";

import { cn } from "@/lib/utils";

/**
 * A sparkline is a shape, not a chart: no axes, no grid, no legend, no
 * tooltip. It answers "which way has this been going" and nothing else, so
 * everything that would invite a closer reading is deliberately absent.
 *
 * Hand-rolled SVG rather than Recharts: a ResponsiveContainer per KPI tile
 * mounts a ResizeObserver each, and four of them on the dashboard is real
 * cost for a 32px-tall decoration.
 */
export function Sparkline({
  values,
  className,
  ariaHidden = true,
}: {
  values: number[];
  className?: string;
  ariaHidden?: boolean;
}) {
  if (values.length < 2) return null;

  const w = 100;
  const h = 28;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;

  const points = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    // 2px inset top and bottom so the stroke is never clipped by the viewBox.
    const y = h - 2 - ((v - min) / span) * (h - 4);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const last = points.at(-1)!.split(",");

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      className={cn("w-full", className)}
      aria-hidden={ariaHidden || undefined}
      focusable="false"
    >
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="var(--chart-1)"
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* The final point is marked, because "where it ended" is the one value
          a reader actually looks for in a sparkline. */}
      <circle
        cx={last[0]}
        cy={last[1]}
        r={2}
        fill="var(--chart-1)"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
