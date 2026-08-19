"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatDayHeading, shiftDay } from "@/lib/calendar";

/**
 * Previous / next / a date picker / Today.
 *
 * The picker is a native `<input type="date">`. A custom calendar popover
 * would need its own keyboard model, its own locale handling and its own
 * screen-reader semantics, all of which the platform control already has and
 * gets right — and on a phone it opens the OS date wheel. `value` is the same
 * `YYYY-MM-DD` string the rest of the feature passes around, so nothing has to
 * convert a `Date` at the boundary.
 */
export function DateNav({
  date,
  today,
  onChange,
}: {
  date: string;
  today: string;
  onChange: (date: string) => void;
}) {
  const isToday = date === today;

  return (
    <div className="flex flex-wrap items-end gap-2">
      <div className="flex items-center gap-1">
        <Button
          variant="secondary"
          size="icon"
          onClick={() => onChange(shiftDay(date, -1))}
          aria-label={`Previous day, ${formatDayHeading(shiftDay(date, -1))}`}
        >
          <ChevronLeft />
        </Button>
        <Button
          variant="secondary"
          size="icon"
          onClick={() => onChange(shiftDay(date, 1))}
          aria-label={`Next day, ${formatDayHeading(shiftDay(date, 1))}`}
        >
          <ChevronRight />
        </Button>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="calendar-date" className="sr-only">
          Date
        </Label>
        <Input
          id="calendar-date"
          type="date"
          value={date}
          onChange={(e) => {
            // An empty value happens while the user is mid-edit or has cleared
            // the field; jumping to an "Invalid Date" day would blank the grid
            // under them. Ignoring it leaves the last good day on screen.
            if (e.target.value) onChange(e.target.value);
          }}
          className="w-[10.5rem] tabular"
        />
      </div>

      <Button
        variant={isToday ? "ghost" : "secondary"}
        onClick={() => onChange(today)}
        disabled={isToday}
      >
        Today
      </Button>
    </div>
  );
}
