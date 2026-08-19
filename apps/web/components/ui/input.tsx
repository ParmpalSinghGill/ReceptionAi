import * as React from "react";

import { cn } from "@/lib/utils";

/** DESIGN.md §3: 8px radius on inputs (cards are 12, pills are 999). */
export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, type, ...props }, ref) {
    return (
      <input
        ref={ref}
        type={type}
        className={cn(
          "flex h-10 w-full rounded-input border border-line bg-bg-inset px-3 py-2 text-sm text-fg",
          "placeholder:text-fg-subtle",
          "transition-colors duration-micro ease-brand hover:border-strong",
          "disabled:cursor-not-allowed disabled:opacity-50",
          "aria-[invalid=true]:border-danger",
          "file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-fg",
          className,
        )}
        {...props}
      />
    );
  },
);

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(
        "flex min-h-24 w-full rounded-input border border-line bg-bg-inset px-3 py-2 text-sm text-fg",
        "placeholder:text-fg-subtle",
        "transition-colors duration-micro ease-brand hover:border-strong",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "aria-[invalid=true]:border-danger",
        className,
      )}
      {...props}
    />
  );
});
