"use client";

import { formatValue } from "@/lib/charts/format";
import { cn } from "@/lib/utils";

/**
 * Outcome mix — a sorted HORIZONTAL bar, not a pie (06 §12.5).
 *
 * Seven outcome kinds, and people compare lengths far better than angles. It
 * is also nominal-categorical: swapping the order would not change the
 * meaning, so every bar takes the SAME slot-1 hue rather than spending the
 * identity channel re-encoding what bar length already shows.
 *
 * Direct-labelled on every row, which is why it needs no legend and no
 * tooltip: the number is already there.
 */
export function OutcomeBars({
  data,
  title,
  description,
}: {
  data: Array<{ key: string; label: string; value: number }>;
  title: string;
  description?: string;
}) {
  const sorted = [...data].sort((a, b) => b.value - a.value);
  const max = Math.max(...sorted.map((d) => d.value), 1);
  const total = sorted.reduce((sum, d) => sum + d.value, 0);

  return (
    <figure className="space-y-3">
      <figcaption>
        <h3 className="text-sm font-medium text-fg">{title}</h3>
        {description && <p className="text-xs text-fg-muted">{description}</p>}
      </figcaption>

      {/* A bar list that is already a labelled row per item IS a table — so
          it is marked up as a list and read directly, with no separate
          table view needed. */}
      <ul className="space-y-2">
        {sorted.map((d) => {
          const pct = d.value / total;
          return (
            <li
              key={d.key}
              className="grid grid-cols-[minmax(4rem,8rem)_minmax(0,1fr)_auto] items-center gap-3"
            >
              <span className="truncate text-xs text-fg-muted">{d.label}</span>
              <span className="relative h-5 rounded-[4px] bg-bg-inset">
                <span
                  className={cn("absolute inset-y-0 left-0 rounded-[4px] bg-chart-1")}
                  style={{ width: `${(d.value / max) * 100}%` }}
                />
              </span>
              <span className="tabular w-20 text-right text-xs text-fg">
                {formatValue(d.value)}
                <span className="ml-1.5 text-fg-subtle">{formatValue(pct, "percent")}</span>
              </span>
            </li>
          );
        })}
      </ul>
    </figure>
  );
}
