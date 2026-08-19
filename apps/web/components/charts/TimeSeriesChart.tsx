"use client";

import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { AnalyticsSeries } from "@/lib/api/types";
import { formatAxisTick, formatValue, type ChartUnit } from "@/lib/charts/format";
import { makeColorScale } from "@/lib/charts/series";
import { ChartFrame, ChartTooltip, SeriesTable, describeSeries } from "./ChartFrame";

/**
 * The rules embedded here, stated plainly (06 §12.3):
 *
 *  - NO DUAL AXES, ever. Two measures of different scale become two charts.
 *    A dual-axis chart lets the author position the crossover anywhere they
 *    like, which makes it an argument rather than a measurement.
 *  - No 3D, no gradients under lines, no drop shadows. Recharts makes all of
 *    these one prop away, which is precisely why the ban is written down.
 *  - `isAnimationActive={false}`. Entry animation delays the reading of the
 *    data by 800ms every single time and is meaningless on a re-render.
 *  - Horizontal grid only. You read a time series ACROSS, not up, so vertical
 *    gridlines add ink without adding a comparison anyone makes.
 *  - Counts start at zero. A truncated y-axis on a count makes a 3% change
 *    look like a collapse, and someone will screenshot it into a board deck.
 */
export function TimeSeriesChart({
  series,
  granularity = "day",
  timezone,
  unit = "count",
  title,
  description,
  height = 280,
  stacked = false,
}: {
  series: AnalyticsSeries[];
  granularity?: "hour" | "day" | "week" | "month";
  timezone: string;
  unit?: ChartUnit;
  title: string;
  description?: string;
  height?: number;
  /** Stacked area shows composition AND total in one read (06 §12.5). */
  stacked?: boolean;
}) {
  const colorOf = useMemo(() => makeColorScale(series.map((s) => s.key)), [series]);

  // Recharts wants row-shaped data; the API returns column-shaped series.
  const rows = useMemo(() => {
    const byTime = new Map<string, Record<string, number | string>>();
    for (const s of series) {
      for (const point of s.points) {
        const row = byTime.get(point.t) ?? { t: point.t };
        row[s.key] = point.v;
        byTime.set(point.t, row);
      }
    }
    return [...byTime.values()].sort((a, b) => String(a.t).localeCompare(String(b.t)));
  }, [series]);

  const Chart = stacked ? AreaChart : LineChart;

  return (
    <ChartFrame
      title={title}
      description={description}
      height={height}
      ariaLabel={describeSeries(title, series, unit)}
      table={
        <SeriesTable series={series} granularity={granularity} timezone={timezone} unit={unit} />
      }
    >
      <ResponsiveContainer width="100%" height="100%">
        <Chart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
          <CartesianGrid stroke="var(--chart-grid)" strokeDasharray="0" vertical={false} />
          <XAxis
            dataKey="t"
            tickFormatter={(t) => formatAxisTick(String(t), granularity, timezone)}
            stroke="var(--chart-axis)"
            tickLine={false}
            axisLine={false}
            minTickGap={24}
            tick={{ fontSize: 11, fill: "var(--chart-axis)" }}
          />
          <YAxis
            stroke="var(--chart-axis)"
            tickLine={false}
            axisLine={false}
            width={48}
            // tabular-nums on the axis so ticks do not shift width as values
            // change (DESIGN.md §2). On an SVG <text> the attribute is
            // `font-variant-numeric`; React's SVG typings spell it
            // `fontVariant`, which sets the same shorthand.
            tick={{ fontSize: 11, fill: "var(--chart-axis)", fontVariant: "tabular-nums" }}
            tickFormatter={(v) => formatValue(Number(v), unit, { compact: true })}
            domain={unit === "count" ? [0, "auto"] : ["auto", "auto"]}
          />
          <Tooltip
            content={
              <ChartTooltip unit={unit} granularity={granularity} timezone={timezone} />
            }
            cursor={{ stroke: "var(--chart-axis)", strokeWidth: 1, strokeOpacity: 0.4 }}
          />
          {series.length > 1 && (
            // A legend is always present for >= 2 series, so identity is never
            // carried by colour alone.
            <Legend
              verticalAlign="top"
              align="left"
              height={28}
              iconType="plainline"
              wrapperStyle={{ fontSize: 11, paddingBottom: 8, color: "var(--chart-axis)" }}
            />
          )}
          {series.map((s) =>
            stacked ? (
              <Area
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.label}
                stackId="1"
                stroke={colorOf(s.key)}
                fill={colorOf(s.key)}
                fillOpacity={0.22}
                strokeWidth={2}
                isAnimationActive={false}
              />
            ) : (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.label}
                stroke={colorOf(s.key)}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--chart-surface)" }}
                isAnimationActive={false}
              />
            ),
          )}
        </Chart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
