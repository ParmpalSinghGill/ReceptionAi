"use client";

import { SEQUENTIAL_RAMP, sequentialStep } from "@/lib/charts/series";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/**
 * Weekday × hour heatmap: two categorical dimensions with one magnitude
 * (06 §12.5). Magnitude means a SEQUENTIAL ramp — one hue, light to dark —
 * never the categorical palette and never a rainbow.
 *
 * Every cell carries its value in a `title` and in the table below, so the
 * data is never colour-only.
 */
export function BusiestHours({
  data,
  title,
  description,
}: {
  data: Array<{ weekday: number; hour: number; value: number }>;
  title: string;
  description?: string;
}) {
  const hours = [...new Set(data.map((d) => d.hour))].sort((a, b) => a - b);
  const max = Math.max(...data.map((d) => d.value), 1);
  const valueAt = (weekday: number, hour: number) =>
    data.find((d) => d.weekday === weekday && d.hour === hour)?.value ?? 0;

  return (
    <figure className="space-y-3">
      <figcaption>
        <h3 className="text-sm font-medium text-fg">{title}</h3>
        {description && <p className="text-xs text-fg-muted">{description}</p>}
      </figcaption>

      <div className="scroll-x">
        <table className="border-separate border-spacing-0.5 text-xs">
          <caption className="sr-only">
            {title}. Calls received by weekday and hour of the day.
          </caption>
          <thead>
            <tr>
              <th scope="col" className="sr-only">
                Day
              </th>
              {hours.map((h) => (
                <th
                  key={h}
                  scope="col"
                  className="tabular px-1 pb-1 text-center font-normal text-fg-subtle"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {WEEKDAYS.map((label, weekday) => (
              <tr key={label}>
                <th
                  scope="row"
                  className="pr-2 text-right font-normal text-fg-subtle whitespace-nowrap"
                >
                  {label}
                </th>
                {hours.map((h) => {
                  const v = valueAt(weekday, h);
                  return (
                    <td key={h} className="p-0">
                      <span
                        className="block size-6 rounded-[3px]"
                        style={{ background: v === 0 ? "var(--bg-inset)" : sequentialStep(v, max) }}
                        title={`${label} ${h}:00 — ${v} call${v === 1 ? "" : "s"}`}
                      >
                        <span className="sr-only">
                          {label} {h}:00, {v} calls
                        </span>
                      </span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* The ramp legend. Ordered, so the reader can see it is a scale rather
          than a set of categories. */}
      <div className="flex items-center gap-2 text-xs text-fg-subtle">
        <span>Quieter</span>
        <span className="flex gap-0.5" aria-hidden="true">
          {SEQUENTIAL_RAMP.map((c) => (
            <span key={c} className="block size-3 rounded-[2px]" style={{ background: c }} />
          ))}
        </span>
        <span>Busier</span>
        <span className="tabular ml-2">(up to {max} calls an hour)</span>
      </div>
    </figure>
  );
}
