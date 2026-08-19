"use client";

import { AlertTriangle, Check, MessageCircle, MessageSquare, Phone, PhoneOutgoing, Unplug } from "lucide-react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { ChannelConfig } from "@/lib/api/types";
import { formatRelative } from "@/lib/format";
import { MOCK_NOW } from "@/lib/mock/data";
import { useChannels } from "@/lib/query/hooks";

const CHANNEL_ICON = {
  voice_inbound: Phone,
  voice_outbound: PhoneOutgoing,
  web_chat: MessageSquare,
  sms: MessageSquare,
  whatsapp: MessageCircle,
} as const;

function HealthBadge({ status }: { status: ChannelConfig["status"] }) {
  // Connector health is text + icon, never a bare coloured dot (§7).
  if (status === "connected") {
    return (
      <Badge tone="ok" icon={<Check />}>
        Connected
      </Badge>
    );
  }
  if (status === "degraded") {
    return (
      <Badge tone="warn" icon={<AlertTriangle />}>
        Needs attention
      </Badge>
    );
  }
  return (
    <Badge tone="neutral" icon={<Unplug />}>
      Not set up
    </Badge>
  );
}

export default function ChannelsPage() {
  const channels = useChannels();
  const rows = channels.data ?? [];

  return (
    <PageShell>
      <PageHeader
        title="Channels"
        description="The phone lines and inboxes the assistant answers."
      />

      {channels.isPending ? (
        <div className="grid min-w-0 gap-4 md:grid-cols-[repeat(2,minmax(0,1fr))]">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-40 animate-pulse rounded-card bg-bg-inset" />
          ))}
        </div>
      ) : (
        <div className="grid min-w-0 gap-4 md:grid-cols-[repeat(2,minmax(0,1fr))]">
          {rows.map((c) => {
            const Icon = CHANNEL_ICON[c.channel];
            return (
              <Card key={c.id}>
                <CardContent className="min-w-0 space-y-3 p-5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex min-w-0 items-start gap-3">
                      <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-pill bg-bg-inset">
                        <Icon className="size-4 text-fg-subtle" aria-hidden="true" />
                      </span>
                      <div className="min-w-0">
                        <h2 className="truncate font-medium text-fg">{c.label}</h2>
                        <p className="truncate font-mono text-xs text-fg-subtle">
                          {c.address ?? "Not configured"}
                        </p>
                      </div>
                    </div>
                    <HealthBadge status={c.status} />
                  </div>

                  {c.detail && <p className="text-sm text-fg-muted">{c.detail}</p>}

                  <div className="flex items-center justify-between gap-3 border-t border-line pt-3">
                    <span className="text-xs text-fg-subtle">
                      {c.provider}
                      {c.last_event_at
                        ? ` · last activity ${formatRelative(c.last_event_at, MOCK_NOW)}`
                        : " · no activity yet"}
                    </span>
                    <Button variant="ghost" size="sm">
                      {c.status === "disconnected" ? "Set up" : "Configure"}
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </PageShell>
  );
}
