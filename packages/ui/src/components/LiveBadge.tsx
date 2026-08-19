"use client";

import * as React from "react";

import { cn } from "../lib/cn";

/**
 * DESIGN.md §5 — the signature live badge.
 *
 * Pill, --bg-raised, hairline border, pulsing --accent dot, label in
 * --fg-muted. Used for marketing's "Live · answering as Hospital / Clinic"
 * and for the admin's active-call indicator.
 *
 * The dot is decorative; the STATE is carried by text, because §7 forbids
 * colour as the only channel. `motion-reduce` turns the pulse into a static
 * dot rather than removing it — the dot still means "live".
 */
export function LiveBadge({
  children,
  live = true,
  className,
  /**
   * When announcing, the label is exposed to assistive tech as a status
   * region. DESIGN.md §7: "a live call indicator announces on state change,
   * not on every tick" — so callers pass a label that changes only on state
   * change, never a ticking timer.
   */
  announce = false,
}: {
  children: React.ReactNode;
  live?: boolean;
  className?: string;
  announce?: boolean;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-2 rounded-pill border border-line bg-bg-raised px-3 py-1.5 text-sm text-fg-muted",
        className,
      )}
      {...(announce ? { role: "status", "aria-live": "polite" } : {})}
    >
      {/* The expanding ring is a SEPARATE element behind a solid core: the
          `live-pulse` keyframe scales to 2.4x and fades to 0, which would make
          the dot itself disappear for most of every cycle if applied to the
          core. `motion-safe:` means the ring simply never animates under
          prefers-reduced-motion, leaving the static dot §4 asks for. */}
      <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
        {live && (
          <span className="absolute inset-0 rounded-pill bg-live motion-safe:animate-live-pulse motion-reduce:hidden" />
        )}
        <span
          className={cn(
            "relative h-2 w-2 rounded-pill",
            live ? "bg-live" : "bg-fg-subtle",
          )}
        />
      </span>
      {children}
    </span>
  );
}
