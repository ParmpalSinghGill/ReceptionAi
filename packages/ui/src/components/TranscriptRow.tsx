import * as React from "react";
import { cn } from "../lib/cn";

export interface TranscriptRowProps
  extends React.HTMLAttributes<HTMLDivElement> {
  /** The speaker tag — "CALLER", "DESK". Rendered as an 11px label. */
  speaker: string;
  /** The message text. */
  children: React.ReactNode;
  /**
   * Marks the row as still streaming; renders a blinking caret after the text.
   * The caret is decorative — the aria-live region on the container announces
   * the text itself.
   */
  streaming?: boolean;
  /** Emphasises the desk's turn against the caller's. */
  tone?: "caller" | "desk";
}

/**
 * TranscriptRow — a LABEL (11px --fg-subtle) beside message text
 * (DESIGN.md §5).
 *
 * The label sits in its own fixed-width column at ≥640px so the message text
 * aligns down the card; below that it stacks, because a 64px gutter on a
 * 375px screen leaves too little for the sentence.
 */
export const TranscriptRow = React.forwardRef<
  HTMLDivElement,
  TranscriptRowProps
>(function TranscriptRow(
  { className, speaker, children, streaming = false, tone = "caller", ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        "flex flex-col gap-1 sm:flex-row sm:gap-4",
        className,
      )}
      {...props}
    >
      <span
        className={cn(
          "shrink-0 pt-0.5 font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)]",
          "sm:w-16",
          tone === "desk" ? "text-accent" : "text-fg-subtle",
        )}
      >
        {speaker}
      </span>
      <p
        className={cn(
          "min-w-0 text-body-sm",
          tone === "desk" ? "text-fg" : "text-fg-muted",
        )}
      >
        {children}
        {streaming ? (
          <span
            aria-hidden="true"
            className="ml-0.5 inline-block h-[1em] w-[2px] translate-y-[0.15em] bg-accent motion-safe:animate-caret-blink"
          />
        ) : null}
      </p>
    </div>
  );
});
