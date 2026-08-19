"use client";

import { useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toast";
import { useSession, useTenant } from "@/components/shell/Providers";
import { usePermissions } from "@/lib/auth/permissions";

const TIMEZONES = [
  "Europe/London",
  "Europe/Dublin",
  "Europe/Paris",
  "Europe/Berlin",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
  "Australia/Sydney",
  "Pacific/Auckland",
];

const LOCALES = [
  { value: "en-GB", label: "English (United Kingdom)" },
  { value: "en-US", label: "English (United States)" },
  { value: "en-IE", label: "English (Ireland)" },
  { value: "fr-FR", label: "French (France)" },
  { value: "de-DE", label: "German (Germany)" },
];

export default function SettingsPage() {
  const { tenantName, timezone, locale, currency, plan, verticalKey, manifestVersion, tenantId } =
    useTenant();
  const { displayName, email } = useSession();
  const { can } = usePermissions();
  const editable = can("tenant:update");

  const [business, setBusiness] = useState({
    name: tenantName,
    phone: "+44 117 496 0100",
    website: "https://northgatedental.co.uk",
    address: "182 Gloucester Road\nBishopston\nBristol\nBS7 8NT",
    greeting: "Thanks for calling Northgate Dental — how can I help?",
    timezone,
    locale,
    currency,
  });

  const [profile, setProfile] = useState({ name: displayName, email });

  return (
    <PageShell>
      <PageHeader
        title="Settings"
        description="Your business details, how the assistant introduces itself, and your own profile."
      />

      <Tabs defaultValue="business">
        <TabsList>
          <TabsTrigger value="business">Business</TabsTrigger>
          <TabsTrigger value="branding">Branding</TabsTrigger>
          <TabsTrigger value="profile">My profile</TabsTrigger>
          <TabsTrigger value="workspace">Workspace</TabsTrigger>
        </TabsList>

        <TabsContent value="business">
          <Card>
            <CardHeader>
              <CardTitle>Business details</CardTitle>
              <CardDescription>
                The assistant uses these when a caller asks where you are or when you are open.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="biz-name">Business name</Label>
                  <Input
                    id="biz-name"
                    value={business.name}
                    disabled={!editable}
                    onChange={(e) => setBusiness({ ...business, name: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="biz-phone">Main phone number</Label>
                  <Input
                    id="biz-phone"
                    type="tel"
                    value={business.phone}
                    disabled={!editable}
                    onChange={(e) => setBusiness({ ...business, phone: e.target.value })}
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="biz-website">Website</Label>
                <Input
                  id="biz-website"
                  type="url"
                  value={business.website}
                  disabled={!editable}
                  onChange={(e) => setBusiness({ ...business, website: e.target.value })}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="biz-address">Address</Label>
                <Textarea
                  id="biz-address"
                  rows={4}
                  value={business.address}
                  disabled={!editable}
                  onChange={(e) => setBusiness({ ...business, address: e.target.value })}
                />
              </div>

              <div className="grid gap-4 sm:grid-cols-3">
                <div className="space-y-2">
                  <Label htmlFor="biz-tz">Timezone</Label>
                  <Select
                    value={business.timezone}
                    disabled={!editable}
                    onValueChange={(v) => setBusiness({ ...business, timezone: v })}
                  >
                    <SelectTrigger id="biz-tz">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {TIMEZONES.map((tz) => (
                        <SelectItem key={tz} value={tz}>
                          {tz}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-fg-subtle">
                    Every time in the portal and every appointment is shown in this zone.
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="biz-locale">Language and formats</Label>
                  <Select
                    value={business.locale}
                    disabled={!editable}
                    onValueChange={(v) => setBusiness({ ...business, locale: v })}
                  >
                    <SelectTrigger id="biz-locale">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {LOCALES.map((l) => (
                        <SelectItem key={l.value} value={l.value}>
                          {l.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="biz-currency">Currency</Label>
                  <Select
                    value={business.currency}
                    disabled={!editable}
                    onValueChange={(v) => setBusiness({ ...business, currency: v })}
                  >
                    <SelectTrigger id="biz-currency">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {["GBP", "EUR", "USD", "AUD", "NZD"].map((c) => (
                        <SelectItem key={c} value={c}>
                          {c}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="flex gap-3 border-t border-line pt-4">
                <Button
                  variant="primary"
                  dot
                  disabled={!editable}
                  onClick={() => toast({ title: "Business details saved", variant: "success" })}
                >
                  Save details
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="branding">
          <Card>
            <CardHeader>
              <CardTitle>How the assistant introduces itself</CardTitle>
              <CardDescription>
                The first thing every caller hears. Keep it short — people start talking over long
                greetings.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="brand-greeting">Greeting</Label>
                <Textarea
                  id="brand-greeting"
                  rows={2}
                  maxLength={200}
                  value={business.greeting}
                  disabled={!editable}
                  onChange={(e) => setBusiness({ ...business, greeting: e.target.value })}
                />
                <p className="tabular text-xs text-fg-subtle">
                  {business.greeting.length} of 200 characters · about{" "}
                  {Math.max(1, Math.round(business.greeting.split(/\s+/).length / 2.5))} seconds to
                  say
                </p>
              </div>

              <div className="rounded-card border border-line bg-bg-inset p-4">
                <p className="text-label uppercase text-fg-subtle">Preview</p>
                <div className="mt-2 grid grid-cols-[3.5rem_minmax(0,1fr)] gap-3">
                  <span className="text-label uppercase text-accent">Desk</span>
                  <p className="text-sm text-fg">{business.greeting}</p>
                </div>
              </div>

              <Button
                variant="primary"
                dot
                disabled={!editable}
                onClick={() => toast({ title: "Greeting saved", variant: "success" })}
              >
                Save greeting
              </Button>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="profile">
          <Card>
            <CardHeader>
              <CardTitle>My profile</CardTitle>
              <CardDescription>How you appear to your colleagues in the audit log.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="me-name">Your name</Label>
                  <Input
                    id="me-name"
                    autoComplete="name"
                    value={profile.name}
                    onChange={(e) => setProfile({ ...profile, name: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="me-email">Email address</Label>
                  <Input
                    id="me-email"
                    type="email"
                    autoComplete="email"
                    value={profile.email}
                    onChange={(e) => setProfile({ ...profile, email: e.target.value })}
                  />
                  <p className="text-xs text-fg-subtle">
                    Changing this sends a confirmation link to the new address.
                  </p>
                </div>
              </div>

              <Button
                variant="primary"
                dot
                onClick={() => toast({ title: "Profile saved", variant: "success" })}
              >
                Save profile
              </Button>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="workspace">
          <Card>
            <CardHeader>
              <CardTitle>Workspace</CardTitle>
              <CardDescription>
                Identifiers you may be asked for by support.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <dl className="space-y-3 text-sm">
                <Row label="Workspace id" value={tenantId} mono />
                <Row label="Plan" value={plan} />
                <Row label="Vertical" value={verticalKey} />
                <Row label="Manifest version" value={manifestVersion} mono />
                <Row label="Timezone" value={timezone} />
              </dl>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </PageShell>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-3">
      <dt className="text-fg-subtle">{label}</dt>
      <dd className={mono ? "font-mono text-xs text-fg" : "text-fg"}>{value}</dd>
    </div>
  );
}
