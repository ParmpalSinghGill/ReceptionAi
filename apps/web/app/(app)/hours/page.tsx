"use client";

import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { TBody, TD, TH, THead, TR, Table, TableEmpty, TableWrap } from "@/components/ui/table";
import { useTenant } from "@/components/shell/Providers";
import type { HolidayException, WeeklyHours } from "@/lib/api/types";
import { useDeleteHoliday, useHolidays, useSaveHoliday, useSaveHours, useWeeklyHours } from "@/lib/query/hooks";

const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
] as const;

export default function HoursPage() {
  const hours = useWeeklyHours();
  const holidays = useHolidays();
  const saveHours = useSaveHours();
  const { timezone } = useTenant();

  const [draft, setDraft] = useState<WeeklyHours[] | null>(null);
  useEffect(() => {
    if (hours.data && !draft) setDraft(hours.data);
  }, [hours.data, draft]);

  if (!draft) {
    return (
      <PageShell>
        <div className="h-8 w-48 animate-pulse rounded-input bg-bg-inset" />
        <div className="h-96 animate-pulse rounded-card bg-bg-inset" />
      </PageShell>
    );
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(hours.data);

  const update = (weekday: number, patch: Partial<WeeklyHours>) =>
    setDraft(draft.map((d) => (d.weekday === weekday ? { ...d, ...patch } : d)));

  const updateInterval = (weekday: number, idx: number, patch: { opens?: string; closes?: string }) =>
    setDraft(
      draft.map((d) =>
        d.weekday === weekday
          ? {
              ...d,
              intervals: d.intervals.map((iv, i) => (i === idx ? { ...iv, ...patch } : iv)),
            }
          : d,
      ),
    );

  return (
    <PageShell>
      <PageHeader
        title="Opening hours"
        description={`When the assistant offers appointments. All times are ${timezone}.`}
        actions={
          <Button
            variant="primary"
            dot
            disabled={!dirty || saveHours.isPending}
            onClick={() => saveHours.mutate(draft)}
          >
            {saveHours.isPending ? "Saving…" : "Save hours"}
          </Button>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>Weekly pattern</CardTitle>
          <CardDescription>
            A day can have more than one period — a lunch closure is two periods, not one long
            one, because the assistant must not offer a 13:30 appointment.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {draft.map((day) => (
            <div
              key={day.weekday}
              className="grid gap-3 rounded-card border border-line bg-bg-inset p-4 sm:grid-cols-[9rem_minmax(0,1fr)]"
            >
              <div className="flex items-center gap-3">
                <Switch
                  id={`open-${day.weekday}`}
                  checked={!day.closed}
                  onCheckedChange={(open) =>
                    update(day.weekday, {
                      closed: !open,
                      intervals:
                        !open === false && day.intervals.length === 0
                          ? [{ opens: "09:00", closes: "17:00" }]
                          : day.intervals,
                    })
                  }
                />
                <Label htmlFor={`open-${day.weekday}`} className="cursor-pointer">
                  {WEEKDAYS[day.weekday]}
                </Label>
              </div>

              {day.closed ? (
                <p className="self-center text-sm text-fg-subtle">Closed all day</p>
              ) : (
                <div className="space-y-2">
                  {day.intervals.map((iv, idx) => (
                    <div key={idx} className="flex flex-wrap items-center gap-2">
                      <Label htmlFor={`o-${day.weekday}-${idx}`} className="sr-only">
                        {WEEKDAYS[day.weekday]} period {idx + 1} opens
                      </Label>
                      <Input
                        id={`o-${day.weekday}-${idx}`}
                        type="time"
                        value={iv.opens}
                        className="w-32"
                        onChange={(e) => updateInterval(day.weekday, idx, { opens: e.target.value })}
                      />
                      <span aria-hidden="true" className="text-fg-subtle">
                        to
                      </span>
                      <Label htmlFor={`c-${day.weekday}-${idx}`} className="sr-only">
                        {WEEKDAYS[day.weekday]} period {idx + 1} closes
                      </Label>
                      <Input
                        id={`c-${day.weekday}-${idx}`}
                        type="time"
                        value={iv.closes}
                        className="w-32"
                        onChange={(e) =>
                          updateInterval(day.weekday, idx, { closes: e.target.value })
                        }
                      />
                      {/* Validation shown inline: a period that ends before it
                          starts is a mistake the server would reject, and
                          saying so here is faster than a round trip. */}
                      {iv.closes <= iv.opens && (
                        <span className="text-xs text-danger-fg">
                          Closing time must be after opening time.
                        </span>
                      )}
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Remove ${WEEKDAYS[day.weekday]} period ${idx + 1}`}
                        onClick={() =>
                          update(day.weekday, {
                            intervals: day.intervals.filter((_, i) => i !== idx),
                          })
                        }
                      >
                        <Trash2 aria-hidden="true" />
                      </Button>
                    </div>
                  ))}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      update(day.weekday, {
                        intervals: [...day.intervals, { opens: "14:00", closes: "17:00" }],
                      })
                    }
                  >
                    <Plus aria-hidden="true" />
                    Add another period
                  </Button>
                </div>
              )}
            </div>
          ))}
        </CardContent>
      </Card>

      <HolidaySection holidays={holidays.data ?? []} loading={holidays.isPending} />
    </PageShell>
  );
}

function HolidaySection({
  holidays,
  loading,
}: {
  holidays: HolidayException[];
  loading: boolean;
}) {
  const remove = useDeleteHoliday();
  const [open, setOpen] = useState(false);

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between">
        <div>
          <CardTitle>Exceptions</CardTitle>
          <CardDescription>
            Bank holidays, training afternoons, and anything else that overrides the weekly
            pattern.
          </CardDescription>
        </div>
        <HolidayDialog open={open} onOpenChange={setOpen} />
      </CardHeader>
      <CardContent>
        <TableWrap className="border-0">
          <Table>
            <THead>
              <TR>
                <TH>Date</TH>
                <TH>Name</TH>
                <TH>Hours</TH>
                <TH>Repeats</TH>
                <TH className="text-right">Actions</TH>
              </TR>
            </THead>
            <TBody>
              {loading ? (
                <TableEmpty colSpan={5} title="Loading exceptions…" />
              ) : holidays.length === 0 ? (
                <TableEmpty
                  colSpan={5}
                  title="No exceptions set"
                  hint="Add one so the assistant does not book appointments on a bank holiday."
                />
              ) : (
                holidays.map((h) => (
                  <TR key={h.id}>
                    <TD className="tabular whitespace-nowrap text-fg">
                      {new Intl.DateTimeFormat("en-GB", {
                        day: "numeric",
                        month: "short",
                        year: "numeric",
                      }).format(new Date(h.date))}
                    </TD>
                    <TD className="text-fg">{h.name}</TD>
                    <TD>
                      {h.closed ? (
                        <Badge tone="danger" icon={<span aria-hidden="true">×</span>}>
                          Closed
                        </Badge>
                      ) : (
                        <span className="tabular">
                          {h.intervals.map((i) => `${i.opens}–${i.closes}`).join(", ")}
                        </span>
                      )}
                    </TD>
                    <TD>{h.recurring_annually ? "Every year" : "Once"}</TD>
                    <TD className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Remove ${h.name}`}
                        onClick={() => remove.mutate(h.id)}
                      >
                        <Trash2 aria-hidden="true" />
                        Remove
                      </Button>
                    </TD>
                  </TR>
                ))
              )}
            </TBody>
          </Table>
        </TableWrap>
      </CardContent>
    </Card>
  );
}

function HolidayDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const save = useSaveHoliday();
  const [name, setName] = useState("");
  const [date, setDate] = useState("");
  const [closed, setClosed] = useState(true);
  const [recurring, setRecurring] = useState(false);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    save.mutate(
      {
        id: "",
        name,
        date,
        closed,
        intervals: closed ? [] : [{ opens: "09:00", closes: "13:00" }],
        recurring_annually: recurring,
      },
      {
        onSuccess: () => {
          onOpenChange(false);
          setName("");
          setDate("");
          setClosed(true);
          setRecurring(false);
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button variant="secondary" size="sm">
          <Plus aria-hidden="true" />
          Add exception
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add an exception</DialogTitle>
          <DialogDescription>
            The assistant will not offer appointments outside these hours on this date.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="hol-name">Name</Label>
            <Input
              id="hol-name"
              required
              value={name}
              placeholder="Summer bank holiday"
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="hol-date">Date</Label>
            <Input
              id="hol-date"
              type="date"
              required
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </div>
          <div className="flex items-center gap-3">
            <Switch id="hol-closed" checked={closed} onCheckedChange={setClosed} />
            <Label htmlFor="hol-closed" className="cursor-pointer">
              Closed all day
            </Label>
          </div>
          <div className="flex items-center gap-3">
            <Switch id="hol-recurring" checked={recurring} onCheckedChange={setRecurring} />
            <Label htmlFor="hol-recurring" className="cursor-pointer">
              Repeats every year
            </Label>
          </div>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={save.isPending}>
              {save.isPending ? "Adding…" : "Add exception"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
