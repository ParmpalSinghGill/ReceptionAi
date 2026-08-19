"use client";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { BusiestHours } from "@/components/charts/BusiestHours";
import { OutcomeBars } from "@/components/charts/OutcomeBars";
import { TimeSeriesChart } from "@/components/charts/TimeSeriesChart";
import { StatTile } from "@/components/dashboard/StatTile";
import { Card, CardContent } from "@/components/ui/card";
import { useTenant } from "@/components/shell/Providers";
import { mockBusiestHours } from "@/lib/mock/data";
import {
  useDashboardKpis,
  useLatencySeries,
  useOutcomeMix,
  useVolumeSeries,
} from "@/lib/query/hooks";

export default function AnalyticsPage() {
  const { timezone } = useTenant();
  const kpis = useDashboardKpis();
  const volume = useVolumeSeries();
  const latency = useLatencySeries();
  const outcomes = useOutcomeMix();

  return (
    <PageShell wide>
      <PageHeader
        title="Analytics"
        description="How the assistant has been performing over the last week."
      />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Conversations"
          value={kpis.data?.conversationsToday.value}
          delta={kpis.data?.conversationsToday.delta}
          loading={kpis.isPending}
        />
        <StatTile
          label="Booked"
          value={kpis.data?.bookedToday.value}
          delta={kpis.data?.bookedToday.delta}
          loading={kpis.isPending}
        />
        <StatTile
          label="Containment"
          value={kpis.data?.containmentRate.value}
          unit="percent"
          delta={kpis.data?.containmentRate.delta}
          loading={kpis.isPending}
        />
        <StatTile
          label="Median reply"
          value={kpis.data?.medianReply.value}
          unit="ms"
          delta={kpis.data?.medianReply.delta}
          deltaGoodDirection="down"
          loading={kpis.isPending}
        />
      </div>

      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <Card>
          <CardContent className="p-5">
            {volume.isPending ? (
              <div className="h-[280px] animate-pulse rounded-input bg-bg-inset" />
            ) : (
              <TimeSeriesChart
                title="Conversations by channel"
                description="Stacked, so the total and the mix read at once."
                series={volume.data ?? []}
                timezone={timezone}
                stacked
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-5">
            {latency.isPending ? (
              <div className="h-[280px] animate-pulse rounded-input bg-bg-inset" />
            ) : (
              <TimeSeriesChart
                title="Reply latency"
                // A mean latency line hides the tail, and the tail is the
                // product problem — so the median and the 95th are plotted
                // together, on ONE axis (never a dual axis).
                description="Median and 95th percentile. The 95th is the one callers complain about."
                series={latency.data ?? []}
                timezone={timezone}
                unit="ms"
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-5">
            {outcomes.isPending ? (
              <div className="h-[280px] animate-pulse rounded-input bg-bg-inset" />
            ) : (
              <OutcomeBars
                title="Outcome mix"
                description="Last 30 days, sorted. Lengths compare better than angles, so this is a bar chart and not a pie."
                data={outcomes.data ?? []}
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-5">
            <BusiestHours
              data={mockBusiestHours()}
              title="Busiest hours"
              description="Calls by weekday and hour. Two peaks: just after opening, and just after lunch."
            />
          </CardContent>
        </Card>
      </div>
    </PageShell>
  );
}
