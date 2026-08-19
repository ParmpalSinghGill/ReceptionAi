"use client";

import * as React from "react";
import { cn } from "../lib/cn";
import { focusRing } from "../lib/focus";

export interface TextareaProps
  extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
}

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  function Textarea({ className, invalid, ...props }, ref) {
    return (
      <textarea
        ref={ref}
        aria-invalid={invalid || undefined}
        className={cn(
          "min-h-20 w-full rounded-input border bg-bg-inset px-3 py-2",
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
