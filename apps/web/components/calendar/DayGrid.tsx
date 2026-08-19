"use client";

import { Skeleton } from "@receptionai/ui";

import { cn } from "@/lib/utils";
import type { CalendarBooking } from "@/lib/api/types";
import { chipLabel, daySlots, minutesToLabel, slotMinutesFor } from "@/lib/calendar";

/**
 * The day, as half-hour rows from 9:00am to 6:00pm.
 *
 * **Why a list of rows rather than a resource-per-column timetable.** A column
 * per resource is the right shape for a practice with four clinicians and the
 * wrong one for a salon with fourteen — the columns become unreadable slivers,
 * and the layout has to be rebuilt for the second tenant. Rows of stacked
 * chips carry any number of concurrent bookings at the same legibility, and
 * the clinician filter narrows the view when someone wants one person's day.
 *
 * Keyboard: each occupied row is a button, so the whole grid is reachable with
 * Tab and operable with Enter/Space for free — no key handlers of our own, and
 * no `role="grid"` reimplementation of behaviour the platform already has.
 */
export function DayGrid({
  bookings,
  timezone,
  selectedSlot,
  onSelectSlot,
  isLoading,
  isBusy = false,
}: {
  bookings: CalendarBooking[];
  timezone: string;
  selectedSlot: number | null;
  onSelectSlot: (minutes: number | null) => void;
  /** First load of this day: nothing real to show, so render skeletons. */
  isLoading: boolean;
  /** A refetch with the previous day still on screen: dim it, keep it. */
  isBusy?: boolean;
}) {
  const slots = daySlots();

  // One pass, not a `filter` per row: 18 rows × N bookings is quadratic for no
  // reason, and the grouping is needed anyway to know which rows are empty.
  const bySlot = new Map<number, CalendarBooking[]>();
  for (const booking of bookings) {
    const minutes = slotMinutesFor(booking.starts_at, timezone);
    const bucket = bySlot.get(minutes);
    if (bucket) bucket.push(booking);
    else bySlot.set(minutes, [booking]);
  }

  return (
    <ol
      className={cn(
        "divide-y divide-[var(--border)] overflow-hidden rounded-card border border-line bg-bg-raised",
        "transition-opacity duration-standard ease-brand",
        // Stale-but-visible. Dimming says "this is the previous day" without
        // throwing away a grid the reader is mid-sentence about, which is what
        // replacing it with skeletons on every arrow press would do.
        isBusy && !isLoading && "opacity-60",
      )}
      aria-busy={isLoading || isBusy}
      aria-label="Appointments by half hour"
    >
      {slots.map((slot) => {
        const inSlot = bySlot.get(slot.minutes) ?? [];
        const isSelected = selectedSlot === slot.minutes;
        const isHour = slot.minutes % 60 === 0;

        return (
          <li key={slot.label}>
            <SlotRow
              label={slot.label}
              isHour={isHour}
              isSelected={isSelected}
              bookings={inSlot}
              isLoading={isLoading}
              onSelect={() => onSelectSlot(isSelected ? null : slot.minutes)}
            />
          </li>
        );
      })}
    </ol>
  );
}

function SlotRow({
  label,
  isHour,
  isSelected,
  bookings,
  isLoading,
  onSelect,
}: {
  label: string;
  isHour: boolean;
  isSelected: boolean;
  bookings: CalendarBooking[];
  isLoading: boolean;
  onSelect: () => void;
}) {
  const count = bookings.length;

  const time = (
    <span
      className={cn(
        "w-14 shrink-0 tabular text-sm",
        // The hour reads louder than its half-hour partner, which is what
        // makes the grid scannable without gridlines every 30 minutes.
        isHour ? "font-medium text-fg" : "text-fg-subtle",
      )}
    >
      {label}
    </span>
  );

  if (isLoading) {
    return (
      <div className="flex items-center gap-4 px-4 py-2.5">
        {time}
        <Skeleton className="h-7 flex-1" />
      </div>
    );
  }

  if (count === 0) {
    return (
      <div className="flex items-center gap-4 px-4 py-2.5">
        {time}
        {/* An empty row says "free", not nothing. A blank row beside a full
            one reads as a rendering failure. */}
        <span className="text-sm text-fg-subtle">Free</span>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={isSelected}
      className={cn(
        "flex w-full items-start gap-4 px-4 py-2.5 text-left",
        "transition-colors duration-micro ease-brand hover:bg-bg-inset",
        isSelected && "bg-bg-inset",
      )}
    >
      {time}
      <span className="flex min-w-0 flex-1 flex-wrap gap-1.5">
        {bookings.map((booking) => (
          <AppointmentChip key={booking.id} booking={booking} />
        ))}
      </span>
      <span className="sr-only">
        {count === 1 ? "1 appointment" : `${count} appointments`} at {label}. Show
        the full list.
      </span>
    </button>
  );
}

/**
 * One appointment, at the size a half-hour row allows.
 *
 * `pending` is dashed as well as amber — DESIGN.md §7 forbids status by colour
 * alone, and the border style survives both themes and a monochrome print.
 */
function AppointmentChip({ booking }: { booking: CalendarBooking }) {
  const label = chipLabel(booking.customer_name, booking.service_name);
  const isPending = booking.status === "pending";

  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 rounded-pill border px-2.5 py-1",
        "text-[13px] leading-tight",
        isPending
          ? "border-dashed border-warn/50 bg-warn-dim text-warn-fg"
          : "border-line bg-accent-dim text-fg",
      )}
      title={label}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-1.5 shrink-0 rounded-pill",
          isPending ? "bg-warn" : "bg-accent",
        )}
      />
      <span className="truncate">{label}</span>
      {isPending && <span className="sr-only">(pending)</span>}
    </span>
  );
}

/** The heading above the table, naming the slot the reader clicked. */
export function slotHeading(minutes: number): string {
  const end = minutes + 30;
  return `${minutesToLabel(minutes)} – ${minutesToLabel(end)}`;
}
