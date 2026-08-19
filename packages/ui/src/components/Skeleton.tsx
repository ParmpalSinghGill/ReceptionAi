import * as React from "react";
import { cn } from "../lib/cn";

/**
 * Skeleton — a loading placeholder.
 *
 * The sheen only runs under motion-safe; with reduced motion it settles to a
 * flat inset block, which still reads as "not yet loaded".
 *
 * aria-hidden with the loading state announced by the CONTAINER (a region with
 * aria-busy), because a screen reader gains nothing from N shimmering boxes.
 */
export const Skeleton = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(function Skeleton({ className, ...props }, ref) {
  return (
    <div
      ref={ref}
      aria-hidden="true"
      className={cn(
        "rounded-input bg-bg-inset",
        "motion-safe:animate-skeleton-sheen",
        "motion-safe:bg-[linear-gradient(90deg,var(--bg-inset)_25%,var(--bg-raised)_50%,var(--bg-inset)_75%)] motion-safe:bg-[length:200%_100%]",
        className,
      )}
      {...props}
    />
  );
});
