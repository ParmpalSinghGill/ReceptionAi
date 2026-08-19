"use client";

import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * DESIGN.md §3: buttons are FULLY ROUNDED PILLS. This is distinctive to the
 * design and must not drift to 6px rounded rectangles — hence `rounded-pill`
 * in the base, not per-variant.
 *
 * The focus ring is inherited from globals.css's `:focus-visible` rule rather
 * than restated here, so a variant cannot accidentally opt out of it.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-pill font-medium transition-colors duration-micro ease-brand disabled:pointer-events-none disabled:opacity-50 whitespace-nowrap [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-fg hover:bg-accent-hover active:bg-accent-press",
        secondary: "border border-line bg-bg-raised text-fg hover:border-strong hover:bg-bg-inset",
        ghost: "text-fg-muted hover:bg-bg-raised hover:text-fg",
        danger: "border border-danger/40 bg-danger-dim text-danger-fg hover:border-danger",
        link: "text-accent underline underline-offset-4 hover:text-accent-hover",
      },
      size: {
        // 2.5.8 Target Size: every size clears 24×24 CSS px. `icon` is 36px.
        sm: "h-8 px-3 text-[13px] [&_svg]:size-3.5",
        md: "h-10 px-4 text-sm [&_svg]:size-4",
        lg: "h-12 px-6 text-base [&_svg]:size-4",
        icon: "h-9 w-9 [&_svg]:size-4",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  /** DESIGN.md §3's signature filled dot before the label (`● Request a call`). */
  dot?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, asChild = false, dot = false, children, ...props },
  ref,
) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props}>
      {dot ? (
        <>
          <span
            aria-hidden="true"
            className="inline-block size-1.5 rounded-pill bg-current opacity-90"
          />
          {children}
        </>
      ) : (
        children
      )}
    </Comp>
  );
});

export { buttonVariants };
