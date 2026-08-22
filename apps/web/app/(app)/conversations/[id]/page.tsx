"use client";

import Link from "next/link";
import { ArrowLeft, Check } from "lucide-react";
import { use } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { ContainedBadge, OutcomeBadge } from "@/components/conversations/ConversationBadges";
import { TranscriptViewer } from "@/components/conversations/TranscriptViewer";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useTenant } from "@/components/shell/Providers";
import { useConversation } from "@/lib/query/hooks";
import {
  CHANNEL_LABELS,
  formatDateTime,
  formatDuration,
  formatLatency,
  formatMoney,
} from "@/lib/format";

export default function ConversationDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { timezone, locale } = useTenant();
  const conversation = useConversation(id);
  const c = conversation.data;

  if (conversation.isPending) {
    return (
      <PageShell>
        <div className="h-8 w-64 animate-pulse rounded-input bg-bg-inset" />
        <div className="h-96 animate-pulse rounded-card bg-bg-inset" />
      </PageShell>
    );
  }

  if (!c) {
    return (
      <PageShell>
        <PageHeader
          title="Conversation not found"
          description="It may have been deleted, or the link may be wrong."
        />
        <Button asChild variant="secondary">
          <Link href="/conversations">
            <ArrowLeft aria-hidden="true" />
            Back to conversations
          </Link>
        </Button>
      </PageShell>
    );
  }

  const totalToolCalls = c.turns.reduce((n, t) => n + t.tool_calls.length, 0);
  const deskTurns = c.turns.filter((t) => t.latency_ms !== null);

  /**
   * BOTH figures are derived from the turns on screen.
   *
   * The conversation summary carries its own `median_reply_ms`, and using it
   * here produced a detail panel that read "median 2.42s / slowest 1.68s" —
   * an impossibility that makes a reader distrust every other number on the
   * page. When a screen shows a statistic beside the rows it summarises, it
   * must compute it from those rows.
   */
  const latencies = deskTurns
    .map((t) => t.latency_ms ?? 0)
    .sort((a, b) => a - b);
  const median = latencies.length
    ? latencies.length % 2
      ? latencies[(latencies.length - 1) / 2]!
      : Math.round((latencies[latencies.length / 2 - 1]! + latencies[latencies.length / 2]!) / 2)
    : null;
  const slowest = latencies.length ? latencies[latencies.length - 1]! : 0;

  return (
    <PageShell>
      <Button asChild variant="ghost" size="sm" className="-ml-3 self-start">
        <Link href="/conversations">
          <ArrowLeft aria-hidden="true" />
          All conversations
        </Link>
      </Button>

      <PageHeader
        title={c.caller_name ?? "Unknown caller"}
        description={
          c.caller_number
            ? `${CHANNEL_LABELS[c.channel]} · ${c.caller_number}`
            : CHANNEL_LABELS[c.channel]
        }
        actions={
          <>
            <OutcomeBadge outcome={c.outcome} status={c.status} />
            <ContainedBadge contained={c.contained} />
          </>
        }
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>Transcript</CardTitle>
            </CardHeader>
            <CardContent>
              <TranscriptViewer turns={c.turns} />
            </CardContent>

            {/* DESIGN.md §5's footer result strip: --accent-dim with a check
                icon. The outcome is stated in words as well as colour. */}
            {c.outcome && (
              <div className="flex items-center gap-2 rounded-b-card border-t border-line bg-accent-dim px-5 py-3">
                <Check className="size-4 shrink-0 text-accent" aria-hidden="true" />
                <p className="text-sm text-fg">{c.summary}</p>
              </div>
            )}
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="space-y-3 text-sm">
                <Row label="Started" value={formatDateTime(c.started_at, timezone, locale)} />
                <Row label="Length" value={formatDuration(c.duration_seconds)} />
                {/* Counted from the transcript, not from the list summary, for
                    the same reason as the latencies below: a count that
                    disagrees with the rows beneath it undermines the page. */}
                <Row label="Turns" value={String(c.turns.length)} />
                <Row label="Tool calls" value={String(totalToolCalls)} />
                <Row label="Median reply" value={formatLatency(median)} />
                <Row label="Slowest reply" value={formatLatency(slowest || null)} />
                <Row label="Cost" value={formatMoney(c.cost, locale)} />
                {c.intent && <Row label="Intent" value={c.intent} mono />}
                {c.sentiment && <Row label="Sentiment" value={c.sentiment} />}
                {c.transfer_reason && <Row label="Transferred because" value={c.transfer_reason} />}
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Turn latency</CardTitle>
            </CardHeader>
            <CardContent>
              {/* A per-turn latency list rather than a chart: eight values is
                  not a distribution, and the reader's question here is "which
                  turn was slow", which a labelled list answers directly. */}
              <ul className="space-y-2">
                {deskTurns.map((t, i) => {
                  const pct = slowest ? ((t.latency_ms ?? 0) / slowest) * 100 : 0;
                  return (
                    <li
                      key={t.id}
                      className="grid grid-cols-[2.5rem_minmax(0,1fr)_auto] items-center gap-2"
                    >
                      <span className="tabular text-xs text-fg-subtle">#{i + 1}</span>
                      <span className="relative h-2 rounded-pill bg-bg-inset">
                        <span
                          className="absolute inset-y-0 left-0 rounded-pill bg-chart-1"
                          style={{ width: `${pct}%` }}
                        />
                      </span>
                      <span className="tabular text-xs text-fg">
                        {formatLatency(t.latency_ms)}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </CardContent>
          </Card>
        </div>
      </div>
    </PageShell>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-fg-subtle">{label}</dt>
      <dd className={mono ? "font-mono text-xs text-fg" : "tabular text-right text-fg"}>{value}</dd>
    </div>
  );
}
