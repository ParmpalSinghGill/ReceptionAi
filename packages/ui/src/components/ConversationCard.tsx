import * as React from "react";
import { cn } from "../lib/cn";

export interface ConversationCardProps
  extends React.HTMLAttributes<HTMLDivElement> {
  /** Header label, e.g. "Incoming call". */
  status: React.ReactNode;
  /** Right-aligned elapsed time, e.g. "0:12". */
  elapsed?: string;
  /** Whether the header dot pulses. */
  live?: boolean;
  /** Optional waveform strip, rendered under the header. */
  waveform?: React.ReactNode;
  /** Transcript rows. */
  children: React.ReactNode;
  /** The result strip — pass a <ConversationResult> or custom node. */
  footer?: React.ReactNode;
}

/**
 * ConversationCard — the signature card (DESIGN.md §5): --bg-raised, 12px
 * radius, a header row (status dot + label, elapsed time right-aligned), an
 * optional waveform, transcript rows, then a footer result strip.
 */
export const ConversationCard = React.forwardRef<
  HTMLDivElement,
  ConversationCardProps
>(function ConversationCard(
  { className, status, elapsed, live = true, waveform, children, footer, ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        "flex flex-col overflow-hidden rounded-card border border-line bg-bg-raised",
        className,
      )}
      {...props}
    >
      <div className="flex items-center justify-between gap-4 border-b border-line px-step5 py-4">
        <span className="flex items-center gap-2 font-sans text-body-sm font-medium text-fg">
          <span
            aria-hidden="true"
            className="relative flex size-2 shrink-0"
          >
            {live ? (
              <span className="absolute inset-0 rounded-pill bg-live motion-safe:animate-live-pulse" />
            ) : null}
            <span
              className={cn(
                "relative size-2 rounded-pill",
                live ? "bg-live" : "bg-fg-subtle",
              )}
            />
          </span>
          {status}
        </span>
        {elapsed ? (
          // tabular-nums so the timer does not jitter as digits change
          // (DESIGN.md §2).
          <span className="font-mono text-body-sm text-fg-subtle [font-variant-numeric:tabular-nums]">
            {elapsed}
          </span>
        ) : null}
      </div>

      {waveform ? (
        <div className="border-b border-line px-step5 py-4">{waveform}</div>
      ) : null}

      <div className="flex flex-col gap-4 px-step5 py-step5">{children}</div>

      {footer}
    </div>
  );
});

export interface ConversationResultProps
  extends React.HTMLAttributes<HTMLDivElement> {
  children: React.ReactNode;
}

/**
 * The footer result strip — --accent-dim with a check glyph (DESIGN.md §5).
 * The glyph is aria-hidden and the WORD carries the meaning, so the state is
 * never colour-alone (§7).
 */
export const ConversationResult = React.forwardRef<
  HTMLDivElement,
  ConversationResultProps
>(function ConversationResult({ className, children, ...props }, ref) {
  return (
    <div
      ref={ref}
      className={cn(
        "flex items-center gap-2 border-t border-line bg-accent-dim px-step5 py-3",
        "font-sans text-body-sm font-medium text-accent",
        className,
      )}
      {...props}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        className="size-4 shrink-0"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M3 8.5l3.5 3.5L13 5" />
      </svg>
      {children}
    </div>
  );
});
