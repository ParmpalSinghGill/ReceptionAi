"use client";

import * as React from "react";
import { cn } from "../lib/cn";
import { focusRing } from "../lib/focus";

export interface InputProps
  extends React.InputHTMLAttributes<HTMLInputElement> {
  /**
   * Marks the field invalid. Sets aria-invalid so the state reaches assistive
   * tech, and pairs the red border with that programmatic signal rather than
   * relying on colour alone (DESIGN.md §7).
   */
  invalid?: boolean;
}

/** Input — 8px radius (DESIGN.md §3), inset well, hairline border. */
export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  function Input({ className, invalid, ...props }, ref) {
    return (
      <input
        ref={ref}
        aria-invalid={invalid || undefined}
        className={cn(
          "h-10 w-full rounded-input border bg-bg-inset px-3",
          "font-sans text-body-sm text-fg placeholder:text-fg-subtle",
          "transition-colors duration-micro ease-brand",
          "disabled:cursor-not-allowed disabled:opacity-50",
          invalid ? "border-danger" : "border-line hover:border-line-strong",
          focusRing,
          className,
        )}
        {...props}
      />
    );
  },
);
