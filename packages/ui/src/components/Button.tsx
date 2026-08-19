"use client";

import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../lib/cn";
import { focusRing } from "../lib/focus";

/**
 * Button — a fully rounded pill (DESIGN.md §3: "must not drift to 6px rounded
 * rectangles"), with the signature leading dot on the primary variant.
 */
const button = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap",
    "rounded-pill font-sans font-medium",
    "transition-colors duration-micro ease-brand",
    "disabled:pointer-events-none disabled:opacity-50",
    focusRing,
  ].join(" "),
  {
    variants: {
      variant: {
        // Dark text on the bright accent. DESIGN.md §7 bans white on --accent.
        primary:
          "bg-accent text-accent-fg hover:bg-accent-hover active:bg-accent-press",
        outline:
          "border border-line-strong bg-transparent text-fg hover:bg-bg-raised hover:border-line-strong",
        ghost: "bg-transparent text-fg-muted hover:bg-bg-raised hover:text-fg",
        // text-danger-ON, not -fg: `--danger-fg` is the hue as readable text on
        // a dark surface, which on a solid --danger fill is the same colour as
        // the fill (contrast 1.00 — an invisible label).
        danger: "bg-danger text-danger-on hover:opacity-90 active:opacity-80",
      },
      size: {
        sm: "h-8 px-4 text-body-sm",
        md: "h-10 px-step5 text-body-sm",
        lg: "h-12 px-step5 text-body",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof button> {
  /**
   * The signature leading dot (`● Request a call`). Defaults to on for the
   * primary variant, which is where the screenshot uses it, and off elsewhere.
   * Pass explicitly to override.
   */
  dot?: boolean;
  /** Render as a child element (an <a>, typically) instead of a <button>. */
  asChild?: boolean;
}

/**
 * The dot is decorative — the label already carries the meaning — so it is
 * aria-hidden and never the sole indicator of anything.
 */
function Dot() {
  return (
    <span
      aria-hidden="true"
      className="size-1.5 shrink-0 rounded-pill bg-current"
    />
  );
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    { className, variant = "primary", size, dot, children, ...props },
    ref,
  ) {
    const showDot = dot ?? variant === "primary";
    return (
      <button
        ref={ref}
        className={cn(button({ variant, size }), className)}
        {...props}
      >
        {showDot ? <Dot /> : null}
        {children}
      </button>
    );
  },
);

/**
 * Anchor styled as a button. A link that navigates must stay an <a> so
 * middle-click, copy-link and the browser's own affordances keep working.
 */
export interface ButtonLinkProps
  extends React.AnchorHTMLAttributes<HTMLAnchorElement>,
    VariantProps<typeof button> {
  dot?: boolean;
}

export const ButtonLink = React.forwardRef<HTMLAnchorElement, ButtonLinkProps>(
  function ButtonLink(
    { className, variant = "primary", size, dot, children, ...props },
    ref,
  ) {
    const showDot = dot ?? variant === "primary";
    return (
      <a
        ref={ref}
        className={cn(button({ variant, size }), className)}
        {...props}
      >
        {showDot ? <Dot /> : null}
        {children}
      </a>
    );
  },
);

export { button as buttonVariants };
