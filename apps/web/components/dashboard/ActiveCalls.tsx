"use client";

import Link from "next/link";
import { MessageSquare, Phone } from "lucide-react";
import { useEffect, useState } from "react";

import { Card } from "@/components/ui/card";
import { useTenant } from "@/components/shell/Providers";
import type { Conversation } from "@/lib/api/types";
import { CHANNEL_LABELS, formatElapsed } from "@/lib/format";
import { MOCK_NOW } from "@/lib/mock/data";

/**
 * The active-call strip. DESIGN.md §5's live badge, one per call in progress.
 *
 * The announcement rule (§7): a live indicator announces ON STATE CHANGE, not
 * on every tick. So the elapsed timer is `aria-hidden` and updates every
 * second for sighted users, while a separate `role="status"` region announces
 * only when the NUMBER of active calls changes. Putting the timer in a live
 * region would make a screen reader read "one minute four seconds… one minute
 * five seconds…" forever.
 */
export function ActiveCalls({ conversations }: { conversations: Conversation[] }) {
  const { timezone } = useTenant();
  const [now, setNow] = useState(MOCK_NOW);

  useEffect(() => {
    // The mock world has a fixed clock; ticking from it keeps the elapsed
    // times consistent with the timestamps everywhere else on the page.
    const id = setInterval(() => setNow((n) => new Date(n.getTime() + 1000)), 1000);
    return () => clearInterval(id);
  }, []);

  const count = conversations.length;

  if (count === 0) {
    return (
      <Card className="flex items-center gap-3 p-4">
        <span className="size-2 shrink-0 rounded-pill bg-fg-subtle" aria-hidden="true" />
        <p className="text-sm text-fg-muted">
          No calls in progress. New calls appear here the moment they connect.
        </p>
      </Card>
    );
  }

  return (
    <section aria-labelledby="active-calls-heading" className="space-y-2">
      <div className="relative flex items-center justify-between">
        <h2 id="active-calls-heading" className="text-label uppercase text-fg-subtle">
          In progress
        </h2>
        {/* The only thing announced: how many, and only when it changes. */}
        <p role="status" aria-live="polite" className="sr-only">
          {count === 1 ? "1 conversation in progress" : `${count} conversations in progress`}
        </p>
      </div>

      {/* `min-w-0` is load-bearing: without it this flex/grid child adopts its
          content's intrinsic width, the `overflow-x: auto` never engages, and
          the whole PAGE scrolls sideways instead of just this strip
          (1.4.10 Reflow). Caught at 375px, where three live calls are ~520px. */}
      <div className="scroll-x min-w-0">
        <ul className="flex gap-3 pb-1">
          {conversations.map((c) => {
            const Icon = c.channel === "web_chat" ? MessageSquare : Phone;
            return (
              <li key={c.id} className="shrink-0">
                {/* `relative` is required, not decorative: `.sr-only` is
                    `position: absolute`, so without a positioned ancestor the
                    screen-reader-only text below resolves against the nearest
                    positioned block far up the tree and extends the PAGE's
                    scroll width — 375px viewport, 517px page. */}
                <Link
                  href={`/conversations/${c.id}`}
                  className="relative flex items-center gap-3 rounded-pill border border-line bg-bg-raised py-2 pl-3 pr-4 transition-colors duration-micro hover:border-strong"
                >
                  <span className="relative flex size-2 shrink-0" aria-hidden="true">
                    <span className="absolute inset-0 rounded-pill bg-live animate-live-pulse motion-reduce:hidden" />
                    <span className="relative size-2 rounded-pill bg-live" />
                  </span>
                  <Icon className="size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" />
                  <span className="min-w-0">
                    <span className="block truncate text-sm text-fg">
                      {c.caller_name ?? "Unknown caller"}
                    </span>
                    <span className="block text-xs text-fg-subtle">
                      {CHANNEL_LABELS[c.channel]} · {c.turn_count} turns
                    </span>
                  </span>
                  <span
                    className="tabular ml-2 shrink-0 text-sm text-fg-muted"
                    aria-hidden="true"
                  >
                    {formatElapsed(c.started_at, now)}
                  </span>
                  <span className="sr-only">
                    , started {formatElapsed(c.started_at, now)} ago
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      </div>
      <p className="text-xs text-fg-subtle" aria-hidden="true">
        Timezone: {timezone}
      </p>
    </section>
  );
}
