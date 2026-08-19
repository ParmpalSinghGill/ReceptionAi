import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * DESIGN.md §3: borders do the work, not shadows. One hairline `--border`,
 * 12px radius, `--bg-raised`. Shadows appear only on overlays.
 */
export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  function Card({ className, ...props }, ref) {
    return (
      <div
        ref={ref}
        // `min-w-0` in the base, because a Card is almost always a flex or
        // grid item and such an item's AUTOMATIC MINIMUM SIZE is its content.
        // Without it a card holding an unbreakable string — a phone number, a
        // hostname, a UUID — grows past its own track and scrolls the page
        // sideways (1.4.10 Reflow). Fixing it here rather than at each call
        // site means no future card can reintroduce the bug.
        className={cn("min-w-0 rounded-card border border-line bg-bg-raised", className)}
        {...props}
      />
    );
  },
);

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1 p-5", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-section font-semibold text-fg", className)} {...props} />;
}

export function CardDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm text-fg-muted", className)} {...props} />;
}

export function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-5 pt-0", className)} {...props} />;
}

export function CardFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("flex items-center gap-3 border-t border-line p-5", className)} {...props} />
  );
}
