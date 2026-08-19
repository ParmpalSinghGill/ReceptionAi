"use client";

import * as React from "react";
import {
  Avatar,
  Badge,
  Button,
  ButtonLink,
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
  ConversationCard,
  ConversationResult,
  Dialog,
  Input,
  Label,
  LiveBadge,
  Select,
  Skeleton,
  StatTile,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
  ToastProvider,
  TranscriptRow,
  Waveform,
  useToast,
} from "@receptionai/ui";

/**
 * The component gallery.
 *
 * Renders every component twice — once in each theme — by stamping
 * `data-theme` on a wrapper. That relies on the scoped (non-:root)
 * `[data-theme]` rules in tokens.css, and it is the fastest way to catch a
 * component that hardcoded a colour: it will look identical in both columns.
 */
export default function Preview() {
  return (
    <ToastProvider>
      <div className="min-h-dvh bg-bg p-step5">
        <div className="mx-auto flex max-w-content flex-col gap-step5">
          <header className="flex flex-col gap-2">
            <h1 className="font-display text-page-title text-fg">
              ReceptionAi component gallery
            </h1>
            <p className="max-w-prose text-body-sm text-fg-muted">
              Every component in both themes. If a component looks the same in
              both columns, it has hardcoded a colour and is wrong.
            </p>
          </header>

          <div className="grid gap-step5 xl:grid-cols-2">
            <ThemeColumn theme="dark" />
            <ThemeColumn theme="light" />
          </div>
        </div>
      </div>
    </ToastProvider>
  );
}

function ThemeColumn({ theme }: { theme: "dark" | "light" }) {
  return (
    <div
      data-theme={theme}
      className="flex flex-col gap-step5 rounded-card border border-line bg-bg p-step5"
    >
      <h2 className="font-sans text-section font-semibold capitalize text-fg">
        {theme} theme
      </h2>
      <Gallery />
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-3">
      <h3 className="font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)] text-fg-subtle">
        {title}
      </h3>
      <div className="flex flex-col gap-3">{children}</div>
    </section>
  );
}

function Gallery() {
  const [dialogOpen, setDialogOpen] = React.useState(false);
  const { toast } = useToast();
  const nameId = React.useId();

  return (
    <>
      <Section title="Buttons">
        <div className="flex flex-wrap items-center gap-3">
          <Button>Request a call</Button>
          <Button variant="outline" dot={false}>
            Try the chat instead
          </Button>
          <Button variant="ghost" dot={false}>
            Ghost
          </Button>
          <Button variant="danger" dot={false}>
            Delete
          </Button>
          <Button disabled>Disabled</Button>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button size="sm">Small</Button>
          <Button size="md">Medium</Button>
          <Button size="lg">Large</Button>
          <ButtonLink href="#" variant="outline" dot={false}>
            Link as button
          </ButtonLink>
        </div>
      </Section>

      <Section title="Badges and live badge">
        <div className="flex flex-wrap items-center gap-3">
          <Badge>Neutral</Badge>
          <Badge variant="accent" glyph={null}>
            Accent
          </Badge>
          <Badge variant="ok">Confirmed</Badge>
          <Badge variant="warn">Needs review</Badge>
          <Badge variant="danger">Failed</Badge>
          <Badge variant="info">Notice</Badge>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <LiveBadge>Live · answering as Hospital / Clinic</LiveBadge>
          <LiveBadge live={false}>Idle · no active call</LiveBadge>
        </div>
      </Section>

      <Section title="Stat tiles">
        <div className="grid gap-step5 sm:grid-cols-2">
          <StatTile
            value="24/7"
            label="Answered"
            description="Nights, weekends, and the ninety seconds your receptionist is on the other line."
          />
          <StatTile
            value="~1.9s"
            label="To reply"
            live
            description="Median time to first word, measured on live traffic."
          />
        </div>
      </Section>

      <Section title="Card">
        <Card>
          <CardHeader>
            <CardTitle>Sunrise Dental</CardTitle>
            <CardDescription>
              Medical vertical · 412 conversations this month
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-body-sm text-fg-muted">
              Missed calls fell from 31% to 3.8% in the first six weeks.
            </p>
          </CardContent>
          <CardFooter>
            <Button size="sm">Open</Button>
            <Button size="sm" variant="outline" dot={false}>
              Export
            </Button>
          </CardFooter>
        </Card>
      </Section>

      <Section title="Form controls">
        <div className="flex flex-col gap-2">
          <Label htmlFor={nameId} required>
            Practice name
          </Label>
          <Input id={nameId} placeholder="Sunrise Dental" />
        </div>
        <div className="flex flex-col gap-2">
          <Label>Vertical</Label>
          <Select defaultValue="medical">
            <option value="medical">Medical &amp; dental</option>
            <option value="legal">Legal intake</option>
            <option value="home">Home services</option>
          </Select>
        </div>
        <div className="flex flex-col gap-2">
          <Label>What counts as an emergency?</Label>
          <Textarea placeholder="Uncontrolled bleeding, facial swelling…" />
        </div>
        <div className="flex flex-col gap-2">
          <Label>Invalid example</Label>
          <Input invalid defaultValue="not-a-phone-number" />
          <p className="text-body-sm text-danger-fg">
            <span aria-hidden="true">✕ </span>
            Enter a phone number in E.164 format.
          </p>
        </div>
      </Section>

      <Section title="Tabs">
        <Tabs defaultValue="transcript">
          <TabsList aria-label="Conversation detail">
            <TabsTrigger value="transcript">Transcript</TabsTrigger>
            <TabsTrigger value="outcome">Outcome</TabsTrigger>
            <TabsTrigger value="audio">Audio</TabsTrigger>
          </TabsList>
          <TabsContent value="transcript">
            <p className="text-body-sm text-fg-muted">
              Arrow keys move between tabs; only the selected tab is in the tab
              order.
            </p>
          </TabsContent>
          <TabsContent value="outcome">
            <p className="text-body-sm text-fg-muted">
              Booked 3:40pm with Dr. Achebe.
            </p>
          </TabsContent>
          <TabsContent value="audio">
            <Waveform
              amplitudes={[0.2, 0.5, 0.8, 0.4, 0.6, 0.3, 0.7, 0.5, 0.2, 0.4]}
              label="Call audio"
            />
          </TabsContent>
        </Tabs>
      </Section>

      <Section title="Table">
        <Table caption="Recent conversations">
          <TableHeader>
            <TableRow>
              <TableHead>Caller</TableHead>
              <TableHead>Channel</TableHead>
              <TableHead>Outcome</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow>
              <TableCell>+1 415 555 0142</TableCell>
              <TableCell>Phone</TableCell>
              <TableCell>
                <Badge variant="ok">Booked</Badge>
              </TableCell>
            </TableRow>
            <TableRow>
              <TableCell>+1 415 555 0177</TableCell>
              <TableCell>SMS</TableCell>
              <TableCell>
                <Badge variant="warn">Escalated</Badge>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </Section>

      <Section title="Avatar and skeleton">
        <div className="flex items-center gap-3">
          <Avatar name="Ada Achebe" size="sm" />
          <Avatar name="Ada Achebe" size="md" />
          <Avatar name="Halstead Plumbing" size="lg" />
        </div>
        <div className="flex flex-col gap-2">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      </Section>

      <Section title="Waveform">
        <Waveform
          amplitudes={[
            0.12, 0.3, 0.55, 0.78, 0.62, 0.4, 0.28, 0.45, 0.7, 0.85, 0.6, 0.35,
            0.2, 0.32, 0.5, 0.68,
          ]}
          label="Example call audio"
        />
        <p className="text-body-sm text-fg-subtle">Idle (no audio):</p>
        <Waveform height={24} bars={32} />
      </Section>

      <Section title="Conversation card">
        <ConversationCard
          status="Incoming call"
          elapsed="0:24"
          waveform={<Waveform amplitudes={[0.2, 0.5, 0.8, 0.4, 0.6, 0.3, 0.7]} />}
          footer={
            <ConversationResult>
              Confirmed and written to your calendar
            </ConversationResult>
          }
        >
          <TranscriptRow speaker="Caller">
            Can anyone see me today? I chipped a tooth.
          </TranscriptRow>
          <TranscriptRow speaker="Desk" tone="desk" streaming>
            I can get you in with Dr. Achebe at 3:40 this afternoon
          </TranscriptRow>
        </ConversationCard>
      </Section>

      <Section title="Dialog and toast">
        <div className="flex flex-wrap gap-3">
          <Button dot={false} variant="outline" onClick={() => setDialogOpen(true)}>
            Open dialog
          </Button>
          <Button
            dot={false}
            variant="outline"
            onClick={() =>
              toast({
                title: "Booking confirmed",
                description: "3:40pm today with Dr. Achebe.",
                variant: "ok",
              })
            }
          >
            Show toast
          </Button>
        </div>
        <Dialog
          open={dialogOpen}
          onOpenChange={setDialogOpen}
          title="Delete this conversation?"
          description="The transcript and recording are removed permanently."
          footer={
            <>
              <Button
                variant="outline"
                dot={false}
                onClick={() => setDialogOpen(false)}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                dot={false}
                onClick={() => setDialogOpen(false)}
              >
                Delete
              </Button>
            </>
          }
        >
          <p className="text-body-sm text-fg-muted">
            Tab is trapped inside this dialog, Escape closes it, and focus
            returns to the button that opened it.
          </p>
        </Dialog>
      </Section>
    </>
  );
}
