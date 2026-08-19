"use client";

import { AlertTriangle, Check, Clock, FileText, Globe, MessageCircleQuestion, Plug, Upload } from "lucide-react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { TBody, TD, TH, THead, TR, Table, TableEmpty, TableWrap } from "@/components/ui/table";
import { useTenant } from "@/components/shell/Providers";
import type { KnowledgeSource } from "@/lib/api/types";
import { formatRelative } from "@/lib/format";
import { MOCK_NOW } from "@/lib/mock/data";
import { useKnowledgeSources } from "@/lib/query/hooks";

const KIND_ICON = {
  upload: FileText,
  website: Globe,
  faq: MessageCircleQuestion,
  connector: Plug,
} as const;

function StatusBadge({ source }: { source: KnowledgeSource }) {
  switch (source.status) {
    case "ready":
      return (
        <Badge tone="ok" icon={<Check />}>
          Ready
        </Badge>
      );
    case "ingesting":
      return (
        <Badge tone="info" icon={<Clock />}>
          Reading…
        </Badge>
      );
    case "stale":
      return (
        <Badge tone="warn" icon={<AlertTriangle />}>
          Out of date
        </Badge>
      );
    default:
      return (
        <Badge tone="danger" icon={<AlertTriangle />}>
          Failed
        </Badge>
      );
  }
}

export default function KnowledgePage() {
  const sources = useKnowledgeSources();
  const rows = sources.data ?? [];
  const { tenantName } = useTenant();

  const totalChunks = rows.reduce((n, s) => n + s.chunk_count, 0);

  return (
    <PageShell>
      <PageHeader
        title="Knowledge"
        description={`What the assistant knows about ${tenantName} beyond its instructions.`}
        actions={
          <Button variant="primary" dot>
            <Upload aria-hidden="true" />
            Add a source
          </Button>
        }
      />

      <p className="text-sm text-fg-muted">
        {rows.length} sources · {totalChunks.toLocaleString("en-GB")} passages the assistant can
        quote from.
      </p>

      <TableWrap>
        <Table>
          <THead>
            <TR>
              <TH>Source</TH>
              <TH>Type</TH>
              <TH>Status</TH>
              <TH className="text-right">Documents</TH>
              <TH className="text-right">Passages</TH>
              <TH className="text-right">Last read</TH>
            </TR>
          </THead>
          <TBody>
            {sources.isPending ? (
              <TableEmpty colSpan={6} title="Loading sources…" />
            ) : rows.length === 0 ? (
              <TableEmpty
                colSpan={6}
                title="Nothing added yet"
                hint="Upload a price list or point us at your website and the assistant will start quoting from it."
              />
            ) : (
              rows.map((s) => {
                const Icon = KIND_ICON[s.kind];
                return (
                  <TR key={s.id}>
                    <TD>
                      <span className="flex items-start gap-2">
                        <Icon
                          className="mt-0.5 size-4 shrink-0 text-fg-subtle"
                          aria-hidden="true"
                        />
                        <span className="min-w-0">
                          <span className="block font-medium text-fg">{s.name}</span>
                          {/* An error is stated in full, with what to DO about
                              it — "failed" on its own is not actionable. */}
                          {s.error && (
                            <span className="block max-w-md text-xs text-danger-fg">{s.error}</span>
                          )}
                        </span>
                      </span>
                    </TD>
                    <TD className="capitalize">{s.kind}</TD>
                    <TD>
                      <StatusBadge source={s} />
                    </TD>
                    <TD className="tabular text-right">{s.document_count}</TD>
                    <TD className="tabular text-right">{s.chunk_count.toLocaleString("en-GB")}</TD>
                    <TD className="whitespace-nowrap text-right">
                      {s.last_ingested_at ? formatRelative(s.last_ingested_at, MOCK_NOW) : "—"}
                    </TD>
                  </TR>
                );
              })
            )}
          </TBody>
        </Table>
      </TableWrap>

      <Card>
        <CardContent className="p-5">
          <h2 className="text-section font-semibold text-fg">How the assistant uses this</h2>
          <p className="mt-2 max-w-prose text-sm text-fg-muted">
            When a caller asks something the instructions do not cover, the assistant searches
            these passages and answers from what it finds — quoting rather than guessing. If
            nothing matches, it says it does not know and offers to take a message, which is why
            an out-of-date price list is worse than no price list at all.
          </p>
        </CardContent>
      </Card>
    </PageShell>
  );
}
