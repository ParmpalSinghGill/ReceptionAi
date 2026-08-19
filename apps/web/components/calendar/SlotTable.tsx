"use client";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@receptionai/ui";

import { BookingStatusBadge } from "@/components/conversations/ConversationBadges";
import type { CalendarBooking } from "@/lib/api/types";
import { peopleAmong } from "@/lib/calendar";
import { formatTime } from "@/lib/format";

/**
 * Everything booked at one time — the shared Table from `packages/ui`.
 *
 * Column order follows what a receptionist answering the phone reads out, in
 * order: when, who is seeing them, who is coming, how to reach them, what for,
 * whether it is confirmed, and the reference they will be asked to quote.
 */
export function SlotTable({
  bookings,
  timezone,
  locale,
  heading,
}: {
  bookings: CalendarBooking[];
  timezone: string;
  locale: string;
  heading: string;
}) {
  return (
    <Table caption={`Appointments at ${heading}`}>
      <TableHeader>
        <TableRow>
          <TableHead className="whitespace-nowrap">Time</TableHead>
          <TableHead>With</TableHead>
          <TableHead>Patient</TableHead>
          <TableHead>Phone</TableHead>
          <TableHead>Service</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Reference</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {bookings.map((booking) => (
          <TableRow key={booking.id}>
            <TableCell className="tabular whitespace-nowrap text-fg">
              {formatTime(booking.starts_at, timezone, locale)}
              <span className="text-fg-subtle">
                {" – "}
                {formatTime(booking.ends_at, timezone, locale)}
              </span>
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {resourceNames(booking) || "—"}
            </TableCell>
            <TableCell className="font-medium text-fg">
              {/* Null means "you may not see this", not "nobody booked it" —
                  the API masks the name for a principal without
                  `conversations:read_pii`. Saying so is better than an em dash
                  that reads as missing data. */}
              {booking.customer_name ?? (
                <span className="font-normal text-fg-subtle">Hidden</span>
              )}
            </TableCell>
            <TableCell className="whitespace-nowrap font-mono text-xs">
              {booking.customer_phone ?? "—"}
            </TableCell>
            <TableCell>{booking.service_name}</TableCell>
            <TableCell>
              <BookingStatusBadge status={booking.status} />
            </TableCell>
            <TableCell className="whitespace-nowrap font-mono text-xs">
              {booking.reference}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * The people, not the rooms.
 *
 * A booking consumes several resources and the diary's "With" column means the
 * person. `peopleAmong` owns which type codes count as people, so the two
 * screens that need the distinction cannot drift apart.
 */
function resourceNames(booking: CalendarBooking): string {
  return peopleAmong(booking.resources)
    .map((r) => r.name)
    .join(", ");
}
