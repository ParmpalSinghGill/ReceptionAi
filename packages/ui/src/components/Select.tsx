"use client";

import * as React from "react";
import { cn } from "../lib/cn";
import { focusRing } from "../lib/focus";

export interface SelectProps
  extends React.SelectHTMLAttributes<HTMLSelectElement> {
  invalid?: boolean;
}

/**
 * Select — a native <select>.
 *
 * Deliberately native rather than a listbox reimplementation: it is keyboard
 * operable, screen-reader correct, and uses the platform picker on mobile for
 * free. The custom chevron is decorative and pointer-events-none so clicks
 * still reach the control beneath it.
 */
export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  function Select({ className, invalid, children, ...props }, ref) {
    return (
      <div className="relative">
        <select
          ref={ref}
          aria-invalid={invalid || undefined}
          className={cn(
            // pr-9 (36px) is chevron clearance, not layout rhythm — it stays
            // on Tailwind's numeric scale rather than the brand step scale.
            "h-10 w-full appearance-none rounded-input border bg-bg-inset pl-3 pr-9",
            "font-sans text-body-sm text-fg",
            "transition-colors duration-micro ease-brand",
            "disabled:cursor-not-allowed disabled:opacity-50",
            invalid ? "border-danger" : "border-line hover:border-line-strong",
            focusRing,
            className,
          )}
          {...props}
        >
          {children}
        </select>
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-fg-subtle"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M4 6l4 4 4-4" />
        </svg>
      </div>
    );
  },
);
