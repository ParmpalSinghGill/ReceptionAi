import * as React from "react";
import { cn } from "../lib/cn";

/**
 * Table — hairline-separated rows on --bg-raised.
 *
 * The wrapper scrolls horizontally and is focusable with tabIndex={0}: a
 * region that scrolls must be reachable by keyboard, otherwise a keyboard-only
 * user cannot see the columns that overflow (WCAG 2.2 — and the reason the
 * wrapper also carries role="region" with a label when `caption` is given).
 */
export interface TableProps extends React.TableHTMLAttributes<HTMLTableElement> {
  /** Describes the table for assistive tech. Rendered visually hidden. */
  caption?: string;
}

export const Table = React.forwardRef<HTMLTableElement, TableProps>(
  function Table({ className, caption, children, ...props }, ref) {
    return (
      <div
        className="w-full overflow-x-auto rounded-card border border-line bg-bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        tabIndex={0}
        role="region"
        aria-label={caption}
      >
        <table
          ref={ref}
          className={cn("w-full border-collapse text-left", className)}
          {...props}
        >
          {caption ? <caption className="sr-only">{caption}</caption> : null}
          {children}
        </table>
      </div>
    );
  },
);

export const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(function TableHeader({ className, ...props }, ref) {
  return <thead ref={ref} className={cn("bg-bg-inset", className)} {...props} />;
});

export const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(function TableBody({ className, ...props }, ref) {
  return <tbody ref={ref} className={cn(className)} {...props} />;
});

export const TableRow = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement>
>(function TableRow({ className, ...props }, ref) {
  return (
    <tr
      ref={ref}
      className={cn(
        "border-b border-line last:border-0 transition-colors duration-micro ease-brand hover:bg-bg-inset",
        className,
      )}
      {...props}
    />
  );
});

export const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(function TableHead({ className, ...props }, ref) {
  return (
    <th
      ref={ref}
      scope="col"
      className={cn(
        "px-4 py-3 font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)] text-fg-subtle",
        className,
      )}
      {...props}
    />
  );
});

export const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(function TableCell({ className, ...props }, ref) {
  return (
    <td
      ref={ref}
      className={cn("px-4 py-3 text-body-sm text-fg-muted", className)}
      {...props}
    />
  );
});
