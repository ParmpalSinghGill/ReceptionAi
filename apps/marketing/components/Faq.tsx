"use client";

import * as React from "react";
import { cn } from "@receptionai/ui";

const ITEMS = [
  {
    q: "Will callers know they're talking to software?",
    a: "It introduces itself as your virtual receptionist on the first turn — we don't let it pretend to be a person, and in several states that disclosure is the law rather than a preference. In practice callers care far more about being answered than about who answers; the complaints we see are about hold music, not about disclosure.",
  },
  {
    q: "What happens when it doesn't know the answer?",
    a: "It says so and hands off, rather than guessing. You define the escalation path per topic: ring a mobile, take a message, or transfer to a human queue during opening hours. Anything clinical, legal, or financial beyond your published policy escalates by default, and you can't switch that off for medical verticals.",
  },
  {
    q: "Does it actually write to our calendar, or just take a message?",
    a: "It writes. ReceptionAi holds the slot while it confirms with the caller, then commits the booking to your existing calendar and sends the confirmation. If the slot disappears mid-conversation because a colleague booked it at the front desk, it notices and offers the next two openings in the same turn instead of failing.",
  },
  {
    q: "How long does setup take?",
    a: "Most practices are answering live calls within a day. You forward your number, connect your calendar, and answer about twenty questions about hours, services, insurers, and what counts as an emergency. That becomes the knowledge base; you can edit any answer afterwards and the change is live on the next call.",
  },
  {
    q: "What about HIPAA and call recordings?",
    a: "We sign a BAA for every medical and dental account. Transcripts and recordings are encrypted at rest, scoped strictly to your tenant, and retained for a window you choose — thirty days by default. You can delete any conversation permanently from the dashboard, and the deletion propagates to backups within the retention window.",
  },
  {
    q: "What does it cost when call volume spikes?",
    a: "Plans are priced on conversations per month, not per minute, so a long call costs the same as a short one. Overages bill at a published per-conversation rate rather than a surprise tier jump, and you can set a hard cap — past it, calls forward to your existing line rather than silently accruing charges.",
  },
];

/**
 * FAQ accordion.
 *
 * Built on <details>/<summary>: it is keyboard operable, screen-reader
 * correct, and expandable without JavaScript for free. Reimplementing this
 * with divs and aria-expanded would be strictly more code and strictly worse.
 */
export function Faq() {
  return (
    <div className="flex flex-col gap-3">
      {ITEMS.map((item) => (
        <details
          key={item.q}
          className={cn(
            "group rounded-card border border-line bg-bg-raised",
            "open:border-line-strong",
          )}
        >
          <summary
            className={cn(
              "flex cursor-pointer list-none items-center justify-between gap-4 p-step5",
              "font-sans text-body font-medium text-fg",
              "rounded-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2",
              "[&::-webkit-details-marker]:hidden",
            )}
          >
            {item.q}
            <svg
              aria-hidden="true"
              viewBox="0 0 16 16"
              className="size-4 shrink-0 text-fg-subtle transition-transform duration-standard ease-brand group-open:rotate-45"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            >
              <path d="M8 3v10M3 8h10" />
            </svg>
          </summary>
          <p className="max-w-prose px-step5 pb-step5 text-body-sm text-fg-muted">
            {item.a}
          </p>
        </details>
      ))}
    </div>
  );
}
