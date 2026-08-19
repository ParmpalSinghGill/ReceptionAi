import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../lib/cn";

/**
 * Badge — a small pill chip.
 *
 * DESIGN.md §7: status is never colour alone. The status variants therefore
 * render a glyph before the label, and the label itself must carry the word
 * ("Confirmed", not a bare green dot). The glyph is aria-hidden because the
 * text already says it — announcing "check mark Confirmed" is noise.
 */
const badge = cva(
  "inline-flex items-center gap-2 rounded-pill border px-3 py-1 font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)]",
  {
    variants: {
      variant: {
        neutral: "border-line bg-bg-inset text-fg-muted",
        accent: "border-transparent bg-accent-dim text-accent",
        ok: "border-transparent bg-ok-dim text-ok-fg",
        warn: "border-transparent bg-warn-dim text-warn-fg",
        danger: "border-transparent bg-danger-dim text-danger-fg",
        info: "border-transparent bg-info-dim text-info-fg",
        outline: "border-line-strong bg-transparent text-fg-muted",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

/** Default glyphs, so a status badge never depends on hue to be understood. */
const STATUS_GLYPH: Partial<Record<string, string>> = {
  ok: "✓",
  warn: "!",
  danger: "✕",
  info: "i",
};

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badge> {
  /** Override the leading glyph, or pass `null` to suppress it. */
  glyph?: React.ReactNode | null;
}

export const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(
  function Badge({ className, variant = "neutral", glyph, children, ...props }, ref) {
    const resolved =
      glyph === undefined ? STATUS_GLYPH[variant ?? "neutral"] : glyph;
    return (
      <span ref={ref} className={cn(badge({ variant }), className)} {...props}>
        {resolved ? (
          <span aria-hidden="true" className="leading-none">
            {resolved}
          </span>
        ) : null}
        {children}
      </span>
    );
  },
);

export { badge as badgeVariants };
