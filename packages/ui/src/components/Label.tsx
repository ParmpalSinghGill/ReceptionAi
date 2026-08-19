import * as React from "react";
import { cn } from "../lib/cn";

export interface LabelProps
  extends React.LabelHTMLAttributes<HTMLLabelElement> {
  /**
   * Appends a required marker. The asterisk is aria-hidden and paired with
   * visually-hidden text, so a screen reader hears "required" rather than
   * "asterisk" — and the field itself still needs its own `required`.
   */
  required?: boolean;
}

export const Label = React.forwardRef<HTMLLabelElement, LabelProps>(
  function Label({ className, required, children, ...props }, ref) {
    return (
      <label
        ref={ref}
        className={cn(
          "font-sans text-body-sm font-medium text-fg",
          className,
        )}
        {...props}
      >
        {children}
        {required ? (
          <>
            <span aria-hidden="true" className="ml-1 text-danger">
              *
            </span>
            <span className="sr-only"> (required)</span>
          </>
        ) : null}
      </label>
    );
  },
);
