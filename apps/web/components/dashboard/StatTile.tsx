"use client";

import { ArrowDown, ArrowUp, Minus } from "lucide-react";

import { Sparkline } from "@/components/charts/Sparkline";
import { Card } from "@/components/ui/card";
import { formatValue, type ChartUnit } from "@/lib/charts/format";
import { cn } from "@/lib/utils";

/**
 * DESIGN.md §5's stat tile: a big SERIF numeral in --accent, an uppercase
 * micro-label, then a line of --fg-muted explanation. The same component
 * appears on the marketing page, which is why the serif numeral matters —
 * it is the thing that makes the dashboard feel like the same product as the
 * landing page.
 *
 * A single number does not need a chart (06 §12.5). The sparkline is the one
 * concession, and it carries direction only.
 */
export function StatTile({
  label,
  value,
  unit = "count",
  delta,
  hint,
  sparkline,
  loading,
  deltaGoodDirection = "up",
}: {
  label: string;
  value: number | undefined;
  unit?: ChartUnit;
  delta?: number;
  hint?: string;
  sparkline?: number[];
  loading?: boolean;
  deltaGoodDirection?: "up" | "down";
}) {
  const good =
    delta === undefined || delta === 0
      ? null
      : deltaGoodDirection === "up"
        ? delta > 0
        : delta < 0;
  const Icon = delta === undefined || delta === 0 ? Minus : delta > 0 ? ArrowUp : ArrowDown;

  return (
    <Card className="p-5">
      <p className="text-label uppercase text-fg-subtle">{label}</p>

      <p className="mt-2 font-display text-stat tabular text-accent">
        {loading ? (
          <span className="inline-block h-9 w-24 animate-pulse rounded-input bg-bg-inset align-middle" />
        ) : (
          formatValue(value ?? 0, unit)
        )}
      </p>

      {delta !== undefined && !loading && (
        <p
          className={cn(
            "mt-1 flex items-center gap-1 text-xs tabular",
            good === null && "text-fg-muted",
            good === true && "text-ok-fg",
            good === false && "text-warn-fg",
          )}
        >
          {/* The arrow is the redundant encoding: direction is never carried
              by colour alone (WCAG 1.4.1). */}
          <Icon className="size-3" aria-hidden="true" />
          {Math.abs(delta * 100).toFixed(1)}%
          <span className="text-fg-subtle">vs. previous week</span>
        </p>
      )}

      {hint && <p className="mt-2 text-sm text-fg-muted">{hint}</p>}

      {sparkline && sparkline.length > 1 && !loading && (
        <Sparkline values={sparkline} className="mt-3 h-7" />
      )}
    </Card>
  );
}
