import { cn } from "@/lib/utils";

/**
 * Every page header is the same shape: a 30px serif title (DESIGN.md §2's
 * "Page title" role), an optional line of explanation, and an actions slot.
 */
export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between",
        className,
      )}
    >
      <div className="min-w-0">
        <h1 className="font-display text-page-title text-fg">{title}</h1>
        {description && <p className="mt-1 max-w-prose text-sm text-fg-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Standard page padding. `max-w-content` is DESIGN.md §6's 1200px. */
export function PageShell({
  children,
  className,
  wide = false,
}: {
  children: React.ReactNode;
  className?: string;
  wide?: boolean;
}) {
  return (
    <div
      className={cn(
        "mx-auto w-full px-4 py-6 sm:px-6 sm:py-8",
        wide ? "max-w-[1600px]" : "max-w-content",
        className,
      )}
    >
      <div className={cn("space-y-6", className)}>{children}</div>
    </div>
  );
}
