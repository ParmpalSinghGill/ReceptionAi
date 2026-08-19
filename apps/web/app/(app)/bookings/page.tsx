"use client";

import { useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { BookingStatusBadge } from "@/components/conversations/ConversationBadges";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { TBody, TD, TH, THead, TR, Table, TableWrap } from "@/components/ui/table";
import { useTenant } from "@/components/shell/Providers";
import { peopleAmong } from "@/lib/calendar";
import { formatDate, formatMoney, formatTime } from "@/lib/format";
import { useBookings } from "@/lib/query/hooks";

export default function BookingsPage() {
  const [status, setStatus] = useState("all");
  const [includePast, setIncludePast] = useState(false);
  const bookings = useBookings({ status, includePast });
  const { timezone, locale } = useTenant();
  const rows = bookings.data ?? [];

  // Grouped by day: a receptionist reads the diary as days, not as a flat list
  // sorted by timestamp.
  //
  // Grouped on `local_date` — the business's own calendar day, written at
  // booking time in the location's zone — and NOT on `starts_at.slice(0, 10)`,
  // which is the UTC date. Those differ for any evening appointment in a
  // positive-offset zone, so slicing would file a 01:00 Tuesday appointment in
  // Sydney under Monday and put it under the wrong heading.
  const byDay = rows.reduce<Record<string, typeof rows>>((acc, b) => {
    (acc[b.local_date] ??= []).push(b);
    return acc;
  }, {});

  return (
    <PageShell wide>
      <PageHeader
        title="Bookings"
        description="Everything in the diary, however it was booked."
      />

      <Card>
        <CardContent className="flex flex-wrap items-end justify-between gap-6 p-4">
          <div className="min-w-56 space-y-1.5">
            <Label htmlFor="booking-status">Status</Label>
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger id="booking-status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All bookings</SelectItem>
                <SelectItem value="confirmed">Confirmed</SelectItem>
                <SelectItem value="pending">Pending</SelectItem>
                <SelectItem value="completed">Completed</SelectItem>
                <SelectItem value="cancelled">Cancelled</SelectItem>
                <SelectItem value="no_show">No-show</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-center gap-3">
            <Switch
              id="booking-include-past"
              checked={includePast}
              onCheckedChange={setIncludePast}
            />
            <div>
              <Label htmlFor="booking-include-past">Include past appointments</Label>
              <p className="text-xs text-fg-subtle">
                {/* Says what the default hides, so an empty list is never
                    mistaken for missing data. */}
                Off by default — the list shows what is still to come.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      <p role="status" aria-live="polite" className="text-sm text-fg-muted">
        {bookings.isFetching
          ? "Loading bookings…"
          : `${rows.length} booking${rows.length === 1 ? "" : "s"}`}
      </p>

      {bookings.isError ? (
        <Card>
          <CardContent className="p-12 text-center">
            <p className="text-sm font-medium text-fg">Bookings could not be loaded</p>
            <p className="mt-1 text-sm text-fg-muted">
              The diary is temporarily unavailable. Try again in a moment.
            </p>
          </CardContent>
        </Card>
      ) : bookings.isPending ? (
        <div className="h-64 animate-pulse rounded-card bg-bg-inset" />
      ) : rows.length === 0 ? (
        <Card>
          <CardContent className="p-12 text-center">
            <p className="text-sm font-medium text-fg">
              {includePast ? "No bookings match that filter" : "Nothing coming up"}
            </p>
            <p className="mt-1 text-sm text-fg-muted">
              {includePast
                ? "Try a different status."
                : "Turn on “include past appointments” to see the ones that have already happened."}
            </p>
          </CardContent>
        </Card>
      ) : (
        Object.entries(byDay).map(([day, dayBookings]) => (
          <section key={day} className="space-y-2">
            <h2 className="text-label uppercase text-fg-subtle">
              {formatDate(`${day}T12:00:00Z`, "UTC", locale)}
            </h2>
            <TableWrap>
              <Table>
                <THead>
                  <TR>
                    <TH className="text-right">Time</TH>
                    <TH>Patient</TH>
                    <TH>Service</TH>
                    <TH>With</TH>
                    <TH>Status</TH>
                    <TH className="text-right">Price</TH>
                    <TH>Reference</TH>
                  </TR>
                </THead>
                <TBody>
                  {dayBookings.map((b) => (
                    <TR key={b.id}>
                      <TD className="tabular whitespace-nowrap text-right text-fg">
                        {formatTime(b.starts_at, b.timezone || timezone, locale)}
                      </TD>
                      <TD>
                        <span className="block font-medium text-fg">
                          {/* Null means "you may not see this" — the API masks
                              it without `conversations:read_pii` — not that
                              nobody booked it. */}
                          {b.customer_name ?? (
                            <span className="font-normal text-fg-subtle">Hidden</span>
                          )}
                        </span>
                        {b.customer_phone && (
                          <span className="block font-mono text-xs text-fg-subtle">
                            {b.customer_phone}
                          </span>
                        )}
                      </TD>
                      <TD>{b.service_name}</TD>
                      <TD className="whitespace-nowrap">
                        {peopleAmong(b.resources)
                          .map((r) => r.name)
                          .join(", ") || "—"}
                      </TD>
                      <TD>
                        <BookingStatusBadge status={b.status} />
                      </TD>
                      <TD className="tabular whitespace-nowrap text-right">
                        {formatMoney(b.price, locale)}
                      </TD>
                      <TD className="font-mono text-xs">{b.reference}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </TableWrap>
          </section>
        ))
      )}
    </PageShell>
  );
}
