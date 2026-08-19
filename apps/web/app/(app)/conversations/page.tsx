"use client";

import Link from "next/link";
import { Search } from "lucide-react";
import { useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { ContainedBadge, OutcomeBadge } from "@/components/conversations/ConversationBadges";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { TBody, TD, TH, THead, TR, Table, TableEmpty, TableWrap } from "@/components/ui/table";
import { useConversations, type ConversationFilters } from "@/lib/query/hooks";
import { CHANNEL_LABELS, OUTCOME_LABELS, formatDateTime, formatDuration, formatLatency } from "@/lib/format";
import { useTenant } from "@/components/shell/Providers";

export default function ConversationsPage() {
  const { timezone } = useTenant();
  const [filters, setFilters] = useState<ConversationFilters>({
    q: "",
    channel: "all",
    outcome: "all",
    contained: "all",
  });
  const conversations = useConversations(filters);
  const rows = conversations.data ?? [];

  const set = (patch: Partial<ConversationFilters>) =>
    setFilters((f) => ({ ...f, ...patch }));

  return (
    <PageShell wide>
      <PageHeader
        title="Conversations"
        description="Every call, chat and message the assistant handled."
      />

      <Card>
        <CardContent className="p-4">
          {/* Filters live in one row above the table. Each control is labelled
              — a placeholder is not a label. */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-1.5">
              <Label htmlFor="filter-q">Search</Label>
              <div className="relative">
                <Search
                  className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-fg-subtle"
                  aria-hidden="true"
                />
                <Input
                  id="filter-q"
                  type="search"
                  className="pl-9"
                  placeholder="Name, number or summary"
                  value={filters.q ?? ""}
                  onChange={(e) => set({ q: e.target.value })}
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="filter-channel">Channel</Label>
              <Select value={filters.channel} onValueChange={(v) => set({ channel: v })}>
                <SelectTrigger id="filter-channel">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All channels</SelectItem>
                  <SelectItem value="voice_inbound">Phone</SelectItem>
                  <SelectItem value="web_chat">Web chat</SelectItem>
                  <SelectItem value="sms">SMS</SelectItem>
                  <SelectItem value="whatsapp">WhatsApp</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="filter-outcome">Outcome</Label>
              <Select value={filters.outcome} onValueChange={(v) => set({ outcome: v })}>
                <SelectTrigger id="filter-outcome">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All outcomes</SelectItem>
                  {Object.entries(OUTCOME_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="filter-contained">Handled by the assistant</Label>
              <Select value={filters.contained} onValueChange={(v) => set({ contained: v })}>
                <SelectTrigger id="filter-contained">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">Either</SelectItem>
                  <SelectItem value="yes">Handled without a person</SelectItem>
                  <SelectItem value="no">Needed a person</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* The result count is announced, so a filter change is perceivable
          without sight (the table silently re-rendering is not). */}
      <p role="status" aria-live="polite" className="text-sm text-fg-muted">
        {conversations.isPending
          ? "Loading conversations…"
          : `${rows.length} conversation${rows.length === 1 ? "" : "s"}`}
      </p>

      <TableWrap>
        <Table>
          <THead>
            <TR>
              <TH>Caller</TH>
              <TH>Channel</TH>
              <TH>Started</TH>
              <TH>Summary</TH>
              <TH>Outcome</TH>
              <TH>Handled</TH>
              <TH className="text-right">Length</TH>
              <TH className="text-right">Median reply</TH>
            </TR>
          </THead>
          <TBody>
            {conversations.isPending ? (
              <TableEmpty colSpan={8} title="Loading conversations…" />
            ) : rows.length === 0 ? (
              <TableEmpty
                colSpan={8}
                title="No conversations match those filters"
                hint="Try widening the channel or outcome filter."
              />
            ) : (
              rows.map((c) => (
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
                  <TD className="whitespace-nowrap">{formatDateTime(c.started_at, timezone)}</TD>
                  <TD className="max-w-sm">
                    <span className="line-clamp-2">{c.summary}</span>
                  </TD>
                  <TD>
                    <OutcomeBadge outcome={c.outcome} />
                  </TD>
                  <TD>
                    <ContainedBadge contained={c.contained} />
                  </TD>
                  <TD className="tabular whitespace-nowrap text-right">
                    {formatDuration(c.duration_seconds)}
                  </TD>
                  <TD className="tabular whitespace-nowrap text-right">
                    {formatLatency(c.median_reply_ms)}
                  </TD>
                </TR>
              ))
            )}
          </TBody>
        </Table>
      </TableWrap>
    </PageShell>
  );
}
