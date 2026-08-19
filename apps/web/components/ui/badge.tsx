import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * DESIGN.md §7: status is NEVER colour alone. A confirmed booking shows a
 * check glyph and the word, not just green — so `Badge` takes an `icon` and
 * every status call site passes one.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "border-line bg-bg-inset text-fg-muted",
        accent: "border-accent/30 bg-accent-dim text-accent",
        ok: "border-ok/30 bg-ok-dim text-ok-fg",
        warn: "border-warn/30 bg-warn-dim text-warn-fg",
        danger: "border-danger/30 bg-danger-dim text-danger-fg",
        info: "border-info/30 bg-info-dim text-info-fg",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {
  icon?: React.ReactNode;
}

export function Badge({ className, tone, icon, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ tone }), className)} {...props}>
      {icon ? (
        <span aria-hidden="true" className="[&_svg]:size-3">
          {icon}
        </span>
      ) : null}
      {children}
    </span>
  );
}
