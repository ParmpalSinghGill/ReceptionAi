import {
  ArrowRightLeft,
  Ban,
  CalendarCheck,
  CalendarClock,
  Check,
  CircleHelp,
  MessageSquareText,
  PhoneOff,
  TriangleAlert,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type {
  BookingStatus,
  ConversationOutcome,
  ConversationStatus,
} from "@/lib/api/types";
import { OUTCOME_LABELS } from "@/lib/format";

/**
 * DESIGN.md §7: status is never colour alone — a confirmed booking shows a
 * CHECK GLYPH AND THE WORD, not just green. Every mapping below carries an
 * icon, and the label is always rendered.
 */
const OUTCOME_STYLE: Record<
  ConversationOutcome,
  { tone: "ok" | "warn" | "danger" | "info" | "neutral" | "accent"; icon: React.ReactNode }
> = {
  resolved: { tone: "ok", icon: <Check /> },
  booked: { tone: "ok", icon: <CalendarCheck /> },
  rescheduled: { tone: "info", icon: <CalendarClock /> },
  answered: { tone: "neutral", icon: <Check /> },
  message_taken: { tone: "neutral", icon: <MessageSquareText /> },
  transferred: { tone: "warn", icon: <ArrowRightLeft /> },
  cancelled: { tone: "warn", icon: <Ban /> },
  abandoned: { tone: "danger", icon: <PhoneOff /> },
  failed: { tone: "danger", icon: <TriangleAlert /> },
};

/**
 * A null outcome means two completely different things and used to render as
 * one. A conversation that is STILL RUNNING has no outcome yet — "In progress"
 * is right and the pulse dot belongs there. A conversation that ENDED without
 * one is a row nothing can honestly classify: a pre-0004 record, or one the
 * agent left before declaring anything. Labelling that "In progress" claimed a
 * finished conversation was live, which is why every row on this screen read
 * "In progress" while nothing at all was happening.
 *
 * `status` is what separates them, so it is required rather than optional — an
 * optional prop here would silently default the wrong way at the one call site
 * that forgot it.
 */
export function OutcomeBadge({
  outcome,
  status,
}: {
  outcome: ConversationOutcome | null;
  status: ConversationStatus;
}) {
  if (!outcome) {
    if (status === "active") {
      return (
        <Badge tone="accent" icon={<span className="block size-1.5 rounded-pill bg-current" />}>
          In progress
        </Badge>
      );
    }
    // Ended, and nothing recorded how. Saying so is better than picking a
    // plausible word: DESIGN.md §7's rule is that a status never overstates
    // what is known.
    return (
      <Badge tone="neutral" icon={<CircleHelp />}>
        Unknown
      </Badge>
    );
  }
  const style = OUTCOME_STYLE[outcome];
  return (
    <Badge tone={style.tone} icon={style.icon}>
      {OUTCOME_LABELS[outcome] ?? outcome}
    </Badge>
  );
}

const BOOKING_STYLE: Record<
  BookingStatus,
  { tone: "ok" | "warn" | "danger" | "info" | "neutral"; icon: React.ReactNode; label: string }
> = {
  confirmed: { tone: "ok", icon: <Check />, label: "Confirmed" },
  pending: { tone: "warn", icon: <CalendarClock />, label: "Pending" },
  completed: { tone: "neutral", icon: <Check />, label: "Completed" },
  cancelled: { tone: "danger", icon: <Ban />, label: "Cancelled" },
  no_show: { tone: "danger", icon: <PhoneOff />, label: "No-show" },
};

export function BookingStatusBadge({ status }: { status: BookingStatus }) {
  const s = BOOKING_STYLE[status];
  return (
    <Badge tone={s.tone} icon={s.icon}>
      {s.label}
    </Badge>
  );
}

/** Containment: did the assistant handle it without a human? */
export function ContainedBadge({ contained }: { contained: boolean }) {
  return contained ? (
    <Badge tone="ok" icon={<Check />}>
      Handled
    </Badge>
  ) : (
    <Badge tone="warn" icon={<ArrowRightLeft />}>
      Needed a person
    </Badge>
  );
}
