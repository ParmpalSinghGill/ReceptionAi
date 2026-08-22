"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { ActiveCalls } from "@/components/dashboard/ActiveCalls";
import { StatTile } from "@/components/dashboard/StatTile";
import { OutcomeBars } from "@/components/charts/OutcomeBars";
import { TimeSeriesChart } from "@/components/charts/TimeSeriesChart";
import { OutcomeBadge } from "@/components/conversations/ConversationBadges";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TBody, TD, TH, THead, TR, Table, TableEmpty, TableWrap } from "@/components/ui/table";
import { useSession, useTenant } from "@/components/shell/Providers";
import {
  useConversations,
  useDashboardKpis,
  useLiveConversations,
  useOutcomeMix,
  useVolumeSeries,
} from "@/lib/query/hooks";
import { CHANNEL_LABELS, formatDuration, formatRelative } from "@/lib/format";
import { MOCK_NOW } from "@/lib/mock/data";

export default function DashboardPage() {
  const { timezone, tenantName } = useTenant();
  const { displayName } = useSession();
  const kpis = useDashboardKpis();
  const volume = useVolumeSeries();
  const outcomes = useOutcomeMix();
  const live = useLiveConversations();
  const recent = useConversations({});

  const firstName = displayName.split(" ")[0];
  // `.rows` rather than `.data`: the list is cursor-paginated, so `data` is
  // the InfiniteData envelope of pages. The dashboard wants the first handful
  // of the newest page, which is exactly what the flattened rows give it.
  const recentRows = recent.rows.slice(0, 8);

  return (
    <PageShell>
      <PageHeader
        title={`Good afternoon, ${firstName}`}
        description={`What ${tenantName} answered today, and what is happening right now.`}
        actions={
          <Button asChild variant="secondary">
            <Link href="/conversations">
              All conversations
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        }
      />

      <ActiveCalls conversations={live.data ?? []} />

      {/* Four tiles: the numbers a practice manager checks before lunch. */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Conversations today"
          value={kpis.data?.conversationsToday.value}
          delta={kpis.data?.conversationsToday.delta}
          sparkline={kpis.data?.conversationsToday.sparkline}
          loading={kpis.isPending}
          hint="Calls, chats and messages answered since midnight."
        />
        <StatTile
          label="Booked"
          value={kpis.data?.bookedToday.value}
          delta={kpis.data?.bookedToday.delta}
          sparkline={kpis.data?.bookedToday.sparkline}
          loading={kpis.isPending}
          hint="Appointments the assistant put in the diary."
        />
        <StatTile
          label="Containment rate"
          value={kpis.data?.containmentRate.value}
          unit="percent"
          delta={kpis.data?.containmentRate.delta}
          sparkline={kpis.data?.containmentRate.sparkline}
          loading={kpis.isPending}
          hint="Finished without needing a person."
        />
        <StatTile
          label="Median reply"
          value={kpis.data?.medianReply.value}
          unit="ms"
          delta={kpis.data?.medianReply.delta}
          // Faster is better, so a NEGATIVE delta is the good direction. Without
          // this the tile would paint a latency improvement as a regression.
          deltaGoodDirection="down"
          sparkline={kpis.data?.medianReply.sparkline}
          loading={kpis.isPending}
          hint="Time from the caller finishing to the first word back."
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardContent className="p-5">
            {volume.isPending ? (
              <ChartSkeleton />
            ) : (
              <TimeSeriesChart
                title="Conversation volume"
                description="The last 7 days by channel. Today is still filling."
                series={volume.data ?? []}
                granularity="day"
                timezone={timezone}
                stacked
                height={260}
              />
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardContent className="p-5">
            {outcomes.isPending ? (
              <ChartSkeleton />
            ) : (
              <OutcomeBars
                title="How they ended"
                description="Last 30 days."
                data={outcomes.data ?? []}
              />
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle>Recent conversations</CardTitle>
          <Button asChild variant="ghost" size="sm">
            <Link href="/conversations">View all</Link>
          </Button>
        </CardHeader>
        <CardContent>
          <TableWrap className="border-0">
            <Table>
              <THead>
                <TR>
                  <TH>Caller</TH>
                  <TH>Channel</TH>
                  <TH>Summary</TH>
                  <TH>Outcome</TH>
                  <TH className="text-right">Length</TH>
                  <TH className="text-right">When</TH>
                </TR>
              </THead>
              <TBody>
                {recent.isPending ? (
                  <TableEmpty colSpan={6} title="Loading conversations…" />
                ) : recentRows.length === 0 ? (
                  <TableEmpty
                    colSpan={6}
                    title="No conversations yet"
                    hint="They appear here as soon as the assistant answers its first call."
                  />
                ) : (
                  recentRows.map((c) => (
                    <TR key={c.id}>
                      <TD>
                        <Link
                          href={`/conversations/${c.id}`}
                          className="font-medium text-fg hover:text-accent"
                        >
                          {c.caller_name ?? "Unknown caller"}
                        </Link>
                        {c.caller_number && (
                          <span className="block font-mono text-xs text-fg-subtle">
                            {c.caller_number}
                          </span>
                        )}
                      </TD>
                      <TD className="whitespace-nowrap">{CHANNEL_LABELS[c.channel]}</TD>
                      <TD className="max-w-md">
                        <span className="line-clamp-2">{c.summary}</span>
                      </TD>
                      <TD>
                        <OutcomeBadge outcome={c.outcome} status={c.status} />
                      </TD>
                      <TD className="tabular text-right whitespace-nowrap">
                        {formatDuration(c.duration_seconds)}
                      </TD>
                      <TD className="text-right whitespace-nowrap">
                        {formatRelative(c.started_at, MOCK_NOW)}
                      </TD>
                    </TR>
                  ))
                )}
              </TBody>
            </Table>
          </TableWrap>
        </CardContent>
      </Card>
    </PageShell>
  );
}

function ChartSkeleton() {
  return (
    <div className="space-y-3">
      <div className="h-4 w-40 animate-pulse rounded-input bg-bg-inset" />
      <div className="h-[240px] animate-pulse rounded-input bg-bg-inset" />
    </div>
  );
}
