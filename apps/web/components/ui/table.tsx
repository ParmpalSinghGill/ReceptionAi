import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * A wide table scrolls INSIDE its own container; the page body never scrolls
 * horizontally (1.4.10 Reflow). `DataTable` below owns that wrapper so no
 * call site has to remember it.
 */
export function TableWrap({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("scroll-x rounded-card border border-line bg-bg-raised", className)}
      {...props}
    />
  );
}

export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return <table className={cn("w-full caption-bottom text-sm", className)} {...props} />;
}

export function THead({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <thead className={cn("border-b border-line", className)} {...props} />;
}

export function TBody({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn("divide-y divide-[var(--border)]", className)} {...props} />;
}

export function TR({ className, ...props }: React.HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr
      className={cn("transition-colors duration-micro hover:bg-bg-inset/60", className)}
      {...props}
    />
  );
}

export function TH({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn(
        "px-4 py-3 text-left text-label uppercase text-fg-subtle whitespace-nowrap",
        className,
      )}
      {...props}
    />
  );
}

export function TD({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("px-4 py-3 align-middle text-fg-muted", className)} {...props} />;
}

/** Empty state. A table that renders zero rows and says nothing reads as broken. */
export function TableEmpty({
  colSpan,
  title,
  hint,
}: {
  colSpan: number;
  title: string;
  hint?: string;
}) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-4 py-12 text-center">
        <p className="text-sm font-medium text-fg">{title}</p>
        {hint && <p className="mt-1 text-sm text-fg-muted">{hint}</p>}
      </td>
    </tr>
  );
}
