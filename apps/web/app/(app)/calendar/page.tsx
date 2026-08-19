"use client";

import { CalendarX2 } from "lucide-react";
import { useMemo, useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { DateNav } from "@/components/calendar/DateNav";
import { DayGrid, slotHeading } from "@/components/calendar/DayGrid";
import { SlotTable } from "@/components/calendar/SlotTable";
import { useTenant } from "@/components/shell/Providers";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  formatDayHeading,
  peopleAmong,
  slotMinutesFor,
  todayInTimezone,
} from "@/lib/calendar";
import { useCalendarDay, useResources } from "@/lib/query/hooks";

/**
 * The diary, one day at a time.
 *
 * Three pieces of state and no more: which day, which resource, which slot is
 * open. All three live in this component rather than in the URL, which is the
 * deliberate trade — a shareable `?date=` would be nice, but `useSearchParams`
 * forces a Suspense boundary on the whole route and the day is a thing you
 * navigate, not a thing you link to. When someone asks for shareable days, the
 * three values move to the URL together.
 */
export default function CalendarPage() {
  const { timezone, locale } = useTenant();

  // `todayInTimezone` is computed once per mount, not per render: recomputing
  // it would silently roll the "Today" target over at midnight while somebody
  // is looking at the screen, moving the button's meaning under their cursor.
  const today = useMemo(() => todayInTimezone(timezone), [timezone]);

  const [date, setDate] = useState(today);
  const [resourceId, setResourceId] = useState("all");
  const [selectedSlot, setSelectedSlot] = useState<number | null>(null);

  const resources = useResources();
  const day = useCalendarDay(date, { resourceId });

  // The API echoes the zone the day was rendered in. Preferring it over the
  // tenant's default matters for a multi-site tenant whose second location is
  // in another country: the grid must be drawn in the clock the appointments
  // were booked against.
  const dayTimezone = day.data?.timezone ?? timezone;
  const bookings = day.data?.items ?? [];

  /**
   * `isPending` is only true for the FIRST load of a key. `placeholderData`
   * keeps the previous day on screen while the next one resolves, which is the
   * behaviour we want for the grid — but it also means every navigation after
   * the first would render a settled, wrong-looking day with no indication
   * that it is stale. `isFetching` covers both, so the busy treatment appears
   * whenever the numbers on screen do not yet describe the chosen day.
   */
  const isBusy = day.isFetching;
  const showSkeletons = day.isPending;

  // Only the people are offered as filter options. A tenant's manifest decides
  // what a resource IS (01 §5) — the type code comes from the server, and
  // `peopleAmong` falls back to every resource so a tenant with no human
  // resource type gets a working filter rather than an empty picker.
  const filterOptions = useMemo(
    () => peopleAmong(resources.data ?? []),
    [resources.data],
  );

  const slotBookings = useMemo(
    () =>
      selectedSlot === null
        ? []
        : bookings.filter((b) => slotMinutesFor(b.starts_at, dayTimezone) === selectedSlot),
    [bookings, selectedSlot, dayTimezone],
  );

  // A slot that was open on Tuesday is meaningless on Wednesday. Resetting on
  // navigation stops the table from showing an empty "10:30" panel for a day
  // that has nothing at 10:30.
  const changeDate = (next: string) => {
    setDate(next);
    setSelectedSlot(null);
  };
  const changeResource = (next: string) => {
    setResourceId(next);
    setSelectedSlot(null);
  };

  // Gated on `isFetching`, not `isPending`: swapping the empty-day panel in
  // while the next day is still in flight makes a busy Tuesday flash "Nothing
  // booked" on the way to a busy Wednesday.
  const isEmptyDay = !isBusy && bookings.length === 0;

  return (
    <PageShell wide>
      <PageHeader
        title="Calendar"
        description="The day as the front desk sees it. Click a time to read the appointments in it."
        actions={<DateNav date={date} today={today} onChange={changeDate} />}
      />

      <Card>
        <CardContent className="flex flex-wrap items-end justify-between gap-4 p-4">
          <div className="min-w-56 space-y-1.5">
            <Label htmlFor="calendar-clinician">Clinician</Label>
            <Select value={resourceId} onValueChange={changeResource}>
              <SelectTrigger id="calendar-clinician">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All clinicians</SelectItem>
                {filterOptions.map((resource) => (
                  <SelectItem key={resource.id} value={resource.id}>
                    {resource.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="text-right">
            <p className="font-display text-section text-fg">{formatDayHeading(date, locale)}</p>
            <p role="status" aria-live="polite" className="text-sm text-fg-muted">
              {isBusy
                ? "Loading the day…"
                : `${bookings.length} appointment${bookings.length === 1 ? "" : "s"}`}
            </p>
          </div>
        </CardContent>
      </Card>

      {day.isError ? (
        <Card>
          <CardContent className="p-12 text-center">
            <p className="text-sm font-medium text-fg">That day could not be loaded</p>
            <p className="mt-1 text-sm text-fg-muted">
              The diary is temporarily unavailable. Try again in a moment.
            </p>
          </CardContent>
        </Card>
      ) : isEmptyDay ? (
        <EmptyDay
          date={date}
          filtered={resourceId !== "all"}
          resourceName={filterOptions.find((r) => r.id === resourceId)?.name}
        />
      ) : (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
          <DayGrid
            bookings={bookings}
            timezone={dayTimezone}
            selectedSlot={selectedSlot}
            onSelectSlot={setSelectedSlot}
            isLoading={showSkeletons}
            isBusy={isBusy}
          />

          <section aria-live="polite" className="space-y-2">
            {selectedSlot === null ? (
              <Card>
                <CardContent className="p-12 text-center">
                  <p className="text-sm font-medium text-fg">Pick a time</p>
                  <p className="mt-1 text-sm text-fg-muted">
                    Choosing a half hour on the left lists every appointment in it,
                    with the patient&rsquo;s number and reference.
                  </p>
                </CardContent>
              </Card>
            ) : (
              <>
                <h2 className="text-label uppercase text-fg-subtle">
                  {slotHeading(selectedSlot)}
                </h2>
                <SlotTable
                  bookings={slotBookings}
                  timezone={dayTimezone}
                  locale={locale}
                  heading={slotHeading(selectedSlot)}
                />
              </>
            )}
          </section>
        </div>
      )}
    </PageShell>
  );
}

/**
 * A day with nothing in it.
 *
 * Two messages, because "the practice is closed" and "this clinician is free"
 * are different facts and a single "No appointments" makes the reader check
 * whether the filter is still on.
 */
function EmptyDay({
  date,
  filtered,
  resourceName,
}: {
  date: string;
  filtered: boolean;
  resourceName?: string;
}) {
  return (
    <Card>
      <CardContent className="flex flex-col items-center gap-3 p-16 text-center">
        <span className="rounded-pill border border-line bg-bg-inset p-3 text-fg-subtle">
          <CalendarX2 aria-hidden="true" className="size-5" />
        </span>
        <p className="text-sm font-medium text-fg">
          {filtered && resourceName
            ? `${resourceName} has nothing booked`
            : "Nothing booked"}
        </p>
        <p className="max-w-prose text-sm text-fg-muted">
          {formatDayHeading(date)} is clear
          {filtered ? " for this clinician — switch to all clinicians to see the rest of the day." : "."}
        </p>
      </CardContent>
    </Card>
  );
}
