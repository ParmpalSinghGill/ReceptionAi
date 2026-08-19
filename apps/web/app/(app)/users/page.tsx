"use client";

import { Check, Minus, Pencil, UserPlus } from "lucide-react";
import { useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TBody, TD, TH, THead, TR, Table, TableEmpty, TableWrap } from "@/components/ui/table";
import { RBAC_MATRIX, ROLE_ORDER } from "@/lib/auth/permissions";
import type { Member, RoleKey } from "@/lib/api/types";
import { formatRelative } from "@/lib/format";
import { MOCK_NOW } from "@/lib/mock/data";
import { useMembers, useSaveMember } from "@/lib/query/hooks";

const ROLE_DESCRIPTIONS: Record<RoleKey, string> = {
  owner: "Everything, including billing and deleting the workspace.",
  admin: "Everything except billing and workspace deletion.",
  manager: "Day-to-day running: bookings, knowledge, the diary.",
  agent: "Handles calls and bookings. Cannot change how the assistant behaves.",
  viewer: "Read-only. For accountants and auditors.",
};

export default function UsersPage() {
  const members = useMembers();
  const [editing, setEditing] = useState<Member | null>(null);
  const [inviting, setInviting] = useState(false);

  const rows = members.data ?? [];

  return (
    <PageShell>
      <PageHeader
        title="Users and roles"
        description="Who can see and change what."
        actions={
          <Button variant="primary" dot onClick={() => setInviting(true)}>
            <UserPlus aria-hidden="true" />
            Invite someone
          </Button>
        }
      />

      <Tabs defaultValue="people">
        <TabsList>
          <TabsTrigger value="people">People</TabsTrigger>
          <TabsTrigger value="roles">What each role can do</TabsTrigger>
        </TabsList>

        <TabsContent value="people">
          <TableWrap>
            <Table>
              <THead>
                <TR>
                  <TH>Name</TH>
                  <TH>Email</TH>
                  <TH>Role</TH>
                  <TH>Status</TH>
                  <TH className="text-right">Last active</TH>
                  <TH className="text-right">Actions</TH>
                </TR>
              </THead>
              <TBody>
                {members.isPending ? (
                  <TableEmpty colSpan={6} title="Loading people…" />
                ) : rows.length === 0 ? (
                  <TableEmpty colSpan={6} title="No one else has access yet" />
                ) : (
                  rows.map((m) => (
                    <TR key={m.id}>
                      <TD className="font-medium text-fg">{m.display_name}</TD>
                      <TD className="font-mono text-xs">{m.email}</TD>
                      <TD>
                        <Badge tone={m.role === "owner" ? "accent" : "neutral"}>{m.role}</Badge>
                      </TD>
                      <TD>
                        {m.status === "active" ? (
                          <Badge tone="ok" icon={<Check />}>
                            Active
                          </Badge>
                        ) : m.status === "invited" ? (
                          <Badge tone="warn" icon={<span aria-hidden="true">✉</span>}>
                            Invited
                          </Badge>
                        ) : (
                          <Badge tone="danger" icon={<Minus />}>
                            Suspended
                          </Badge>
                        )}
                      </TD>
                      <TD className="whitespace-nowrap text-right">
                        {m.last_active_at ? formatRelative(m.last_active_at, MOCK_NOW) : "Never"}
                      </TD>
                      <TD className="text-right">
                        <Button variant="ghost" size="sm" onClick={() => setEditing(m)}>
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
        </TabsContent>

        <TabsContent value="roles">
          <RbacGrid />
        </TabsContent>
      </Tabs>

      <MemberDialog
        member={editing}
        open={Boolean(editing) || inviting}
        onClose={() => {
          setEditing(null);
          setInviting(false);
        }}
      />
    </PageShell>
  );
}

/**
 * The RBAC grid.
 *
 * A DISPLAY of the server's model, not a second source of truth — the server's
 * `requires()` is what actually decides. Rendering it lets an administrator
 * answer "what can a manager do?" without reading the docs, which is the
 * question that otherwise gets answered by granting admin to everyone.
 *
 * The tick is a GLYPH, not a coloured cell: a grid that encodes permission as
 * green-vs-grey is unreadable to a screen reader and to anyone with a colour
 * vision deficiency, and this grid is exactly the kind of thing people export
 * to a spreadsheet and print in black and white.
 */
function RbacGrid() {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {ROLE_ORDER.map((role) => (
          <Card key={role} className="p-4">
            <p className="text-label uppercase text-accent">{role}</p>
            <p className="mt-1 text-xs text-fg-muted">{ROLE_DESCRIPTIONS[role]}</p>
          </Card>
        ))}
      </div>

      {RBAC_MATRIX.map((group) => (
        <Card key={group.label}>
          <CardHeader>
            <CardTitle>{group.label}</CardTitle>
            <CardDescription>{group.description}</CardDescription>
          </CardHeader>
          <CardContent>
            <TableWrap className="border-0">
              <Table>
                <THead>
                  <TR>
                    <TH>Permission</TH>
                    {ROLE_ORDER.map((r) => (
                      <TH key={r} className="text-center">
                        {r}
                      </TH>
                    ))}
                  </TR>
                </THead>
                <TBody>
                  {group.permissions.map((p) => (
                    <TR key={p.perm}>
                      <TD>
                        <span className="block text-fg">{p.label}</span>
                        <span className="block font-mono text-xs text-fg-subtle">{p.perm}</span>
                      </TD>
                      {ROLE_ORDER.map((role) => {
                        const allowed = p.roles.includes(role);
                        return (
                          <TD key={role} className="text-center">
                            {allowed ? (
                              <>
                                <Check
                                  className="mx-auto size-4 text-ok-fg"
                                  aria-hidden="true"
                                />
                                <span className="sr-only">
                                  {role} can {p.label}
                                </span>
                              </>
                            ) : (
                              <>
                                <Minus
                                  className="mx-auto size-4 text-fg-subtle"
                                  aria-hidden="true"
                                />
                                <span className="sr-only">
                                  {role} cannot {p.label}
                                </span>
                              </>
                            )}
                          </TD>
                        );
                      })}
                    </TR>
                  ))}
                </TBody>
              </Table>
            </TableWrap>
          </CardContent>
        </Card>
      ))}

      <p className="max-w-prose text-xs text-fg-subtle">
        Hiding a button is a convenience, not a security control. Every action above is checked
        again on the server, so a user who reaches an endpoint directly is refused regardless of
        what this portal showed them.
      </p>
    </div>
  );
}

function MemberDialog({
  member,
  open,
  onClose,
}: {
  member: Member | null;
  open: boolean;
  onClose: () => void;
}) {
  const save = useSaveMember();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<RoleKey>("agent");

  const seeded = member?.id ?? "new";
  const [seedKey, setSeedKey] = useState(seeded);
  if (seedKey !== seeded) {
    setSeedKey(seeded);
    setEmail(member?.email ?? "");
    setRole(member?.role ?? "agent");
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    save.mutate({ id: member?.id, email, role, display_name: member?.display_name }, {
      onSuccess: onClose,
    });
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{member ? `Edit ${member.display_name}` : "Invite someone"}</DialogTitle>
          <DialogDescription>
            {member
              ? "Changing a role takes effect the next time they load the portal."
              : "They will get an email with a link to set a password."}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="mem-email">Email address</Label>
            <Input
              id="mem-email"
              type="email"
              required
              disabled={Boolean(member)}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="mem-role">Role</Label>
            <Select value={role} onValueChange={(v) => setRole(v as RoleKey)}>
              <SelectTrigger id="mem-role">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ROLE_ORDER.map((r) => (
                  <SelectItem key={r} value={r}>
                    {r}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-fg-muted">{ROLE_DESCRIPTIONS[role]}</p>
          </div>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={save.isPending}>
              {save.isPending ? "Saving…" : member ? "Save changes" : "Send invitation"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
