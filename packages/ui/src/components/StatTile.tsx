import * as React from "react";
import { cn } from "../lib/cn";

export interface StatTileProps extends React.HTMLAttributes<HTMLDivElement> {
  /** The big serif numeral, e.g. "24/7" or "~1.9s". */
  value: React.ReactNode;
  /** The uppercase micro-label, e.g. "ANSWERED". */
  label: string;
  /** A line of explanation in --fg-muted. */
  description?: React.ReactNode;
  /**
   * Set for values that update live. DESIGN.md §2 requires tabular-nums
   * anywhere numbers change, so the numeral doesn't jitter between frames.
   */
  live?: boolean;
}

/**
 * StatTile — big serif numeral in --accent, an uppercase micro-label, then a
 * line of --fg-muted explanation (DESIGN.md §5). Used on both marketing and
 * the dashboard.
 *
 * Marked up as a description list: the label is the term and the numeral its
 * definition, which is what a screen reader needs to pair them. Visually the
 * numeral leads, so the DOM order is reversed with flex-col-reverse — the
 * accessible order stays label-then-value.
 */
export const StatTile = React.forwardRef<HTMLDivElement, StatTileProps>(
  function StatTile(
    { className, value, label, description, live = false, ...props },
    ref,
  ) {
    return (
      <div ref={ref} className={cn("flex flex-col gap-2", className)} {...props}>
        <dl className="flex flex-col-reverse gap-1">
          <dt className="font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)] text-fg-subtle">
            {label}
          </dt>
          <dd
            className={cn(
              "font-display text-stat text-accent",
              live && "[font-variant-numeric:tabular-nums]",
            )}
          >
            {value}
          </dd>
        </dl>
        {description ? (
          <p className="max-w-prose text-body-sm text-fg-muted">{description}</p>
        ) : null}
      </div>
    );
  },
);
