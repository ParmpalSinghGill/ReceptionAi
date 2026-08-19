"use client";

import { Pencil, Plus } from "lucide-react";
import { useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { TBody, TD, TH, THead, TR, Table, TableEmpty, TableWrap } from "@/components/ui/table";
import { useTenant } from "@/components/shell/Providers";
import type { Service } from "@/lib/api/types";
import { formatMoney } from "@/lib/format";
import { useSaveService, useServices, useToggleService } from "@/lib/query/hooks";

export default function ServicesPage() {
  const services = useServices();
  const toggle = useToggleService();
  const { locale } = useTenant();
  const [editing, setEditing] = useState<Service | null>(null);
  const [creating, setCreating] = useState(false);

  const rows = services.data ?? [];

  return (
    <PageShell>
      <PageHeader
        title="Services"
        description="What the assistant can book, how long each takes, and what it costs."
        actions={
          <Button variant="primary" dot onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            New service
          </Button>
        }
      />

      <TableWrap>
        <Table>
          <THead>
            <TR>
              <TH>Service</TH>
              <TH>Category</TH>
              <TH className="text-right">Duration</TH>
              <TH className="text-right">Price</TH>
              <TH>Deposit</TH>
              <TH>Bookable</TH>
              <TH className="text-right">Actions</TH>
            </TR>
          </THead>
          <TBody>
            {services.isPending ? (
              <TableEmpty colSpan={7} title="Loading services…" />
            ) : rows.length === 0 ? (
              <TableEmpty
                colSpan={7}
                title="No services yet"
                hint="Add one so the assistant has something to offer."
              />
            ) : (
              rows.map((s) => (
                <TR key={s.id}>
                  <TD>
                    <span className="block font-medium text-fg">{s.name}</span>
                    {s.description && (
                      <span className="block max-w-md text-xs text-fg-subtle">{s.description}</span>
                    )}
                  </TD>
                  <TD>{s.category ?? "—"}</TD>
                  <TD className="tabular whitespace-nowrap text-right">
                    {s.duration_minutes} min
                    {s.buffer_after_minutes > 0 && (
                      <span className="block text-xs text-fg-subtle">
                        +{s.buffer_after_minutes} buffer
                      </span>
                    )}
                  </TD>
                  <TD className="tabular whitespace-nowrap text-right">
                    {formatMoney(s.price, locale)}
                  </TD>
                  <TD>
                    {s.requires_deposit ? (
                      <Badge tone="info" icon={<span aria-hidden="true">£</span>}>
                        Required
                      </Badge>
                    ) : (
                      <span className="text-fg-subtle">Not required</span>
                    )}
                  </TD>
                  <TD>
                    <div className="flex items-center gap-2">
                      <Switch
                        checked={s.is_active}
                        aria-label={`${s.name} bookable`}
                        onCheckedChange={(v) =>
                          toggle.mutate({ id: s.id, isActive: v, version: s.version })
                        }
                      />
                      {/* The state in words as well as the switch position. */}
                      <span className="text-xs text-fg-muted">{s.is_active ? "Yes" : "No"}</span>
                    </div>
                  </TD>
                  <TD className="text-right">
                    <Button variant="ghost" size="sm" onClick={() => setEditing(s)}>
                      <Pencil aria-hidden="true" />
                      Edit
                    </Button>
                  </TD>
                </TR>
              ))
            )}
          </TBody>
        </Table>
      </TableWrap>

      <ServiceDialog
        service={editing}
        open={Boolean(editing) || creating}
        onClose={() => {
          setEditing(null);
          setCreating(false);
        }}
      />
    </PageShell>
  );
}

function ServiceDialog({
  service,
  open,
  onClose,
}: {
  service: Service | null;
  open: boolean;
  onClose: () => void;
}) {
  const save = useSaveService();
  const [form, setForm] = useState<Partial<Service>>({});

  // Seed from the service being edited each time the dialog opens.
  const seeded = service?.id ?? "new";
  const [seedKey, setSeedKey] = useState(seeded);
  if (seedKey !== seeded) {
    setSeedKey(seeded);
    setForm(service ?? { name: "", duration_minutes: 30, buffer_after_minutes: 5 });
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    save.mutate({ ...form, name: form.name ?? "" } as Service, { onSuccess: onClose });
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{service ? "Edit service" : "New service"}</DialogTitle>
          <DialogDescription>
            Duration is what gets blocked in the diary; the buffer is the gap after it.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="svc-name">Name</Label>
            <Input
              id="svc-name"
              required
              value={form.name ?? ""}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="svc-desc">Description</Label>
            <Textarea
              id="svc-desc"
              rows={2}
              value={form.description ?? ""}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
            <p className="text-xs text-fg-subtle">
              The assistant may read this out, so write it as you would say it.
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="svc-duration">Duration (minutes)</Label>
              <Input
                id="svc-duration"
                type="number"
                min={5}
                step={5}
                required
                value={form.duration_minutes ?? 30}
                onChange={(e) => setForm({ ...form, duration_minutes: Number(e.target.value) })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="svc-buffer">Buffer after (minutes)</Label>
              <Input
                id="svc-buffer"
                type="number"
                min={0}
                step={5}
                value={form.buffer_after_minutes ?? 0}
                onChange={(e) =>
                  setForm({ ...form, buffer_after_minutes: Number(e.target.value) })
                }
              />
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="svc-price">Price</Label>
              <Input
                id="svc-price"
                type="text"
                inputMode="decimal"
                placeholder="68.00"
                value={form.price?.amount ?? ""}
                onChange={(e) =>
                  setForm({ ...form, price: { amount: e.target.value, currency: "GBP" } })
                }
              />
              {/* Money is a decimal STRING plus a currency, never a float. */}
              <p className="text-xs text-fg-subtle">In pounds, e.g. 68.00</p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="svc-category">Category</Label>
              <Input
                id="svc-category"
                value={form.category ?? ""}
                onChange={(e) => setForm({ ...form, category: e.target.value })}
              />
            </div>
          </div>

          <div className="flex items-center gap-3">
            <Switch
              id="svc-deposit"
              checked={form.requires_deposit ?? false}
              onCheckedChange={(v) => setForm({ ...form, requires_deposit: v })}
            />
            <Label htmlFor="svc-deposit" className="cursor-pointer">
              Requires a deposit to book
            </Label>
          </div>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save service"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
