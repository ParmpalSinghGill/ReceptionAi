import { cn } from "@/lib/utils";

/**
 * DESIGN.md §1: the "Ai" in the wordmark carries `--accent`. The serif/sans
 * contrast is the whole personality, so the mark uses the display serif while
 * the nav around it is sans.
 */
export function Wordmark({ collapsed = false, className }: { collapsed?: boolean; className?: string }) {
  if (collapsed) {
    return (
      <span
        className={cn("font-display text-2xl leading-none text-fg rail:hidden", className)}
        aria-hidden="true"
      >
        R<span className="text-accent">a</span>
      </span>
    );
  }
  return (
    <span className={cn("font-display text-2xl leading-none text-fg", className)}>
      Reception<span className="text-accent">Ai</span>
      <span className="sr-only"> admin portal</span>
    </span>
  );
}
