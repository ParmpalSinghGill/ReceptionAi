"use client";

import { Check, Search, ShieldX } from "lucide-react";
import { useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TBody, TD, TH, THead, TR, Table, TableEmpty, TableWrap } from "@/components/ui/table";
import { useTenant } from "@/components/shell/Providers";
import { formatDateTime } from "@/lib/format";
import { useAudit } from "@/lib/query/hooks";

export default function AuditPage() {
  const [q, setQ] = useState("");
  const audit = useAudit({ q });
  const { timezone, locale } = useTenant();
  const rows = audit.data ?? [];

  return (
    <PageShell wide>
      <PageHeader
        title="Audit log"
        description="Every change anyone made, and every request that was refused."
      />

      <Card>
        <CardContent className="p-4">
          <div className="max-w-sm space-y-1.5">
            <Label htmlFor="audit-q">Search</Label>
            <div className="relative">
              <Search
                className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-fg-subtle"
                aria-hidden="true"
              />
              <Input
                id="audit-q"
                type="search"
                className="pl-9"
                placeholder="Person, action or target"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      <p role="status" aria-live="polite" className="text-sm text-fg-muted">
        {audit.isPending ? "Loading…" : `${rows.length} entries`}
      </p>

      <TableWrap>
        <Table>
          <THead>
            <TR>
              <TH>When</TH>
              <TH>Who</TH>
              <TH>Action</TH>
              <TH>What changed</TH>
              <TH>Result</TH>
              <TH>IP</TH>
              <TH>Request</TH>
            </TR>
          </THead>
          <TBody>
            {audit.isPending ? (
              <TableEmpty colSpan={7} title="Loading the audit log…" />
            ) : rows.length === 0 ? (
              <TableEmpty colSpan={7} title="Nothing matches that search" />
            ) : (
              rows.map((a) => (
                <TR key={a.id}>
                  <TD className="tabular whitespace-nowrap">
                    {formatDateTime(a.occurred_at, timezone, locale)}
                  </TD>
                  <TD>
                    <span className="block text-fg">{a.actor_name}</span>
                    <span className="block font-mono text-xs text-fg-subtle">{a.actor_email}</span>
                  </TD>
                  <TD className="font-mono text-xs">{a.action}</TD>
                  <TD className="max-w-sm">{a.target_label}</TD>
                  <TD>
                    {a.outcome === "allowed" ? (
                      <Badge tone="neutral" icon={<Check />}>
                        Allowed
                      </Badge>
                    ) : (
                      // A refusal is the interesting row in an audit log, so it
                      // is marked more loudly than a success.
                      <Badge tone="danger" icon={<ShieldX />}>
                        Refused
                      </Badge>
                    )}
                  </TD>
                  <TD className="font-mono text-xs">{a.ip ?? "—"}</TD>
                  <TD className="font-mono text-xs text-fg-subtle">{a.request_id}</TD>
                </TR>
              ))
            )}
          </TBody>
        </Table>
      </TableWrap>

      <p className="max-w-prose text-xs text-fg-subtle">
        Entries are written by the API, not by this portal, and cannot be edited or deleted from
        here. The request id matches the one in your server logs and traces.
      </p>
    </PageShell>
  );
}
