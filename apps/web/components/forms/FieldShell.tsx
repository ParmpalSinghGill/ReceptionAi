"use client";

import { useId, type ReactNode } from "react";

import { Label } from "@/components/ui/label";
import type { FieldError, RenderField } from "@/lib/forms/types";
import { cn } from "@/lib/utils";

export function FieldShell({
  field,
  inputId,
  errors,
  registerRef,
  children,
}: {
  field: RenderField;
  inputId: string;
  errors: FieldError[];
  registerRef: (el: HTMLElement | null) => void;
  children: (ids: { describedBy: string; labelId: string }) => ReactNode;
}) {
  const helpId = useId();
  const errorId = useId();
  const labelId = `${inputId}-label`;

  const hasError = errors.some((e) => e.severity === "error");

  // Order matters: aria-describedby is announced in the order given, and the
  // error must come BEFORE the hint so a screen-reader user hears the problem
  // first rather than after a sentence of guidance.
  const describedBy = [errors.length ? errorId : null, field.help ? helpId : null]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      ref={registerRef as never}
      // tabIndex -1 so the error summary's jump links can focus this wrapper
      // without adding every field group to the tab order.
      tabIndex={-1}
      className="space-y-2 scroll-mt-24 focus:outline-none"
    >
      <div className="flex items-baseline justify-between gap-2">
        <Label id={labelId} htmlFor={inputId} className={cn(hasError && "text-danger-fg")}>
          {field.label}
          {field.required && (
            <>
              {/* The asterisk is decorative; the word is what gets announced. */}
              <span aria-hidden="true" className="ml-0.5 text-danger-fg">
                *
              </span>
              <span className="sr-only"> (required)</span>
            </>
          )}
        </Label>

        {field.sensitive && (
          <span
            className="rounded-pill bg-bg-inset px-2 py-0.5 text-xs text-fg-subtle"
            title="This value is masked on screen and excluded from analytics, error reports and autosaved drafts."
          >
            Sensitive
          </span>
        )}
      </div>

      {children({ describedBy, labelId })}

      {field.help && (
        <p id={helpId} className="text-sm text-fg-muted">
          {field.help}
        </p>
      )}

      {errors.length > 0 && (
        <ul id={errorId} className="space-y-1">
          {errors.map((error, i) => (
            <li
              key={i}
              className={cn(
                "text-sm",
                error.severity === "error" && "text-danger-fg",
                error.severity === "warning" && "text-warn-fg",
                error.severity === "info" && "text-fg-muted",
              )}
            >
              {error.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
