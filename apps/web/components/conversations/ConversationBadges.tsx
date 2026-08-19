import {
  ArrowRightLeft,
  Ban,
  CalendarCheck,
  CalendarClock,
  Check,
  MessageSquareText,
  PhoneOff,
  TriangleAlert,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { BookingStatus, ConversationOutcome } from "@/lib/api/types";
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
  booked: { tone: "ok", icon: <CalendarCheck /> },
  rescheduled: { tone: "info", icon: <CalendarClock /> },
  answered: { tone: "neutral", icon: <Check /> },
  message_taken: { tone: "neutral", icon: <MessageSquareText /> },
  transferred: { tone: "warn", icon: <ArrowRightLeft /> },
  cancelled: { tone: "warn", icon: <Ban /> },
  abandoned: { tone: "danger", icon: <PhoneOff /> },
  failed: { tone: "danger", icon: <TriangleAlert /> },
};

export function OutcomeBadge({ outcome }: { outcome: ConversationOutcome | null }) {
  if (!outcome) {
    return (
      <Badge tone="accent" icon={<span className="block size-1.5 rounded-pill bg-current" />}>
        In progress
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
