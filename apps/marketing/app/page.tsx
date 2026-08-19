"use client";

import * as React from "react";
import {
  Badge,
  Button,
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
  LiveBadge,
  StatTile,
} from "@receptionai/ui";
import { SiteNav } from "../components/SiteNav";
import { HeroCall } from "../components/HeroCall";
import { ChatPanel } from "../components/ChatPanel";
import { CallPanel } from "../components/CallPanel";
import { Faq } from "../components/Faq";
import { Wordmark } from "../components/Wordmark";

const STATS = [
  {
    value: "24/7",
    label: "Answered",
    description:
      "Nights, weekends, and the ninety seconds your receptionist is on the other line.",
  },
  {
    value: "9",
    label: "Industries",
    description:
      "From dental triage to restaurant covers — each vertical ships its own intake rules.",
  },
  {
    value: "6",
    label: "Channels",
    description:
      "Phone, SMS, web chat, WhatsApp, Instagram DMs, and the contact form on your site.",
  },
  {
    value: "~1.9s",
    label: "To reply",
    description:
      "Median time to first word, measured on live traffic — short enough that nobody says “hello?” twice.",
  },
];

const STEPS = [
  {
    n: "01",
    title: "Forward your number",
    body: "Point your existing line at ReceptionAi — all of it, or only calls that ring out after four rings. Your number, your carrier, no port required. Most practices start with overflow and move to full-time within a fortnight.",
  },
  {
    n: "02",
    title: "Answer twenty questions",
    body: "Hours, services, insurers you accept, parking, what counts as an emergency, who to wake at 2am. That becomes the knowledge base. Edit any answer later and it is live on the very next call.",
  },
  {
    n: "03",
    title: "It books, you get the transcript",
    body: "ReceptionAi checks real availability, holds the slot, confirms with the caller, and writes the appointment to your calendar. You get a transcript, a recording, and a one-line summary in your inbox.",
  },
];

const INDUSTRIES = [
  {
    name: "Medical & dental",
    badge: "HIPAA · BAA signed",
    body: "Triage language reviewed by clinicians. Anything that sounds like chest pain, uncontrolled bleeding, or a dental abscess with facial swelling escalates to a human immediately rather than being booked. Insurance eligibility is quoted from your own published list, never guessed.",
    proof: "Sunrise Dental cut missed calls from 31% to under 4% in six weeks.",
  },
  {
    name: "Legal intake",
    badge: "Conflict-checked",
    body: "Captures the intake facts that actually matter — jurisdiction, dates, opposing party — and runs a conflict check against your matter list before it offers a consultation slot. It never gives legal advice, and it says so plainly when asked.",
    proof: "Average intake call: 4m12s, with zero partner time spent on unqualified leads.",
  },
  {
    name: "Home services",
    badge: "Dispatch-aware",
    body: "Knows which van is closest, which technician holds the gas certification, and what a Saturday callout costs. Books to the route rather than to a generic calendar, so the 8am job and the 9:30 job are not forty minutes apart.",
    proof: "Halstead Plumbing books 22% more emergency jobs on the same headcount.",
  },
  {
    name: "Veterinary",
    badge: "Species-aware triage",
    body: "Asks the questions a vet nurse would ask first — species, breed, weight, whether the animal is eating, and what it might have swallowed. Chocolate, lilies, antifreeze and bloat in a deep-chested dog skip the diary entirely and ring the on-call number. Routine boosters and nail clips book themselves.",
    proof: "Weekend out-of-hours calls answered on the first ring, not the fourth.",
  },
  {
    name: "Salons & spas",
    badge: "Stylist-matched",
    body: "Books to the stylist, not the shop. It knows who does balayage, who is fully booked until March, and that a colour correction needs three hours and a patch test forty-eight hours before. Reschedules move the whole chain of appointments rather than orphaning the second one.",
    proof: "No-shows fall when every booking gets a same-day WhatsApp confirmation.",
  },
  {
    name: "Real estate",
    badge: "Lead-qualified",
    body: "Qualifies before it books a viewing — budget, chain, mortgage in principle, and whether they have actually seen the listing price. Serious buyers get Saturday slots that match the vendor's access instructions; browsers get the brochure and a follow-up, without a negotiator losing an afternoon.",
    proof: "Viewings booked at 9pm on a Sunday, when half of all enquiries arrive.",
  },
  {
    name: "Fitness & wellness",
    badge: "Class-capacity aware",
    body: "Holds the last spot in the 6am class while the caller decides, runs the waitlist when someone drops, and knows a first session needs an induction before it can be booked. Handles the membership questions that make up most of the calls — freezes, cancellations, what the joining fee covers.",
    proof: "Waitlist spots fill in minutes instead of sitting empty at class time.",
  },
  {
    name: "Automotive service",
    badge: "Bay-scheduled",
    body: "Books against bay time and the right technician, not a generic slot — a diagnostic is ninety minutes, a service is half a day, and the courtesy car has to be free for both. Registration first, so the history and the parts are on the desk before the car arrives.",
    proof: "Every booking arrives with the reg, the mileage and the symptom already captured.",
  },
  {
    name: "Hospitality",
    badge: "Cover-managed",
    body: "Takes reservations against real covers and turn times, holds the large-party enquiries that need a deposit, and captures allergies as structured notes the kitchen actually reads. Knows the difference between a fully booked Friday and a Friday with a 6pm table free for ninety minutes.",
    proof: "Allergy and access notes reach the pass on every booking, not most of them.",
  },
];

const REASONS = [
  {
    title: "It hears the whole sentence",
    body: "Callers interrupt, change their mind mid-booking, and give a date as “the Tuesday after next”. ReceptionAi resolves all three in one turn instead of restarting the menu, because it is holding the conversation in context rather than matching keywords.",
  },
  {
    title: "It checks before it promises",
    body: "Availability is read live from your calendar at the moment of the offer, and the slot is held while the caller decides. A booking that cannot be honoured is worse than a missed call; this is the part most voice bots get wrong.",
  },
  {
    title: "It knows what it doesn't know",
    body: "Every answer is grounded in the knowledge base you wrote. Ask something outside it and you get an honest handoff, not a confident invention — and the gap shows up in your dashboard so you can fill it.",
  },
  {
    title: "It leaves a paper trail",
    body: "Transcript, recording, structured outcome, and the exact calendar write for every conversation. When a patient insists they were told something else, you can check in about nine seconds.",
  },
];

/**
 * Which demo occupies the hero card slot.
 *
 * One slot, three states, rather than two panels that can both be open: a
 * visitor holding a voice call while a chat streams underneath would be two
 * conversations with the same agent and no way to tell which one answered.
 */
type Demo = "none" | "voice" | "chat";

export default function Home() {
  const [demo, setDemo] = React.useState<Demo>("none");
  const chatRef = React.useRef<HTMLDivElement>(null);

  function openDemo(which: Exclude<Demo, "none">) {
    setDemo(which);
    // Focus moves into the panel on mount; scrolling it into view matters for
    // the case where the visitor clicked the nav CTA far down the page.
    window.requestAnimationFrame(() => {
      chatRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }

  const openVoice = () => openDemo("voice");
  const openChat = () => openDemo("chat");

  return (
    <>
      <SiteNav onRequestCall={openVoice} />

      <main id="main">
        {/* ---------------------------------------------------------- hero -- */}
        <section className="mx-auto max-w-content px-step5 pb-step9 pt-step8">
          {/* 50/50 above 1024px, stacked below (DESIGN.md §6). */}
          <div className="grid items-center gap-step8 lg:grid-cols-2">
            <div className="flex flex-col items-start gap-step5">
              <LiveBadge>Live · answering as Hospital / Clinic</LiveBadge>

              <h1 className="font-display text-hero text-fg">
                The front desk that{" "}
                <em className="italic text-accent">always</em> picks up.
              </h1>

              <p className="max-w-prose text-body text-fg-muted">
                ReceptionAi answers every call, text, and web chat for your
                practice. It books, moves and cancels appointments in your
                diary, answers the questions people actually ring about, and
                escalates the emergencies. No hold music, no voicemail, no
                missed Tuesday morning.
              </p>

              <div className="flex flex-wrap items-center gap-3">
                <Button
                  size="lg"
                  onClick={openVoice}
                  aria-expanded={demo === "voice"}
                  aria-controls="chat-demo"
                >
                  Request a call
                </Button>
                <Button
                  size="lg"
                  variant="outline"
                  dot={false}
                  onClick={openChat}
                  aria-expanded={demo === "chat"}
                  aria-controls="chat-demo"
                >
                  Try the chat instead
                </Button>
              </div>

              <p className="text-body-sm text-fg-subtle">
                No credit card. The call runs in your browser &mdash; allow the
                microphone and talk to it, and you can hang up on the robot
                guilt-free.
              </p>
            </div>

            <div ref={chatRef} id="chat-demo" className="w-full">
              {demo === "voice" ? (
                <CallPanel onClose={() => setDemo("none")} />
              ) : demo === "chat" ? (
                <ChatPanel onClose={() => setDemo("none")} />
              ) : (
                <HeroCall />
              )}
            </div>
          </div>
        </section>

        {/* --------------------------------------------------------- stats -- */}
        <section
          aria-label="By the numbers"
          className="border-y border-line bg-bg-raised"
        >
          <div className="mx-auto grid max-w-content gap-step6 px-step5 py-step7 sm:grid-cols-2 lg:grid-cols-4">
            {STATS.map((s) => (
              <StatTile
                key={s.label}
                value={s.value}
                label={s.label}
                description={s.description}
              />
            ))}
          </div>
        </section>

        {/* -------------------------------------------------- how it works -- */}
        <section id="how-it-works" className="mx-auto max-w-content px-step5 py-step9">
          <SectionHeading
            eyebrow="How it works"
            title="Three steps, one afternoon."
            lede="There is no phone system to rip out and no hardware. The longest part is deciding what counts as an emergency."
          />
          <ol className="mt-step6 grid gap-step5 md:grid-cols-3">
            {STEPS.map((s) => (
              <li key={s.n}>
                <Card className="h-full">
                  <CardHeader>
                    <span
                      aria-hidden="true"
                      className="font-display text-stat text-accent"
                    >
                      {s.n}
                    </span>
                    <CardTitle>{s.title}</CardTitle>
                    <CardDescription>{s.body}</CardDescription>
                  </CardHeader>
                </Card>
              </li>
            ))}
          </ol>
        </section>

        {/* ---------------------------------------------------- industries -- */}
        <section
          id="industries"
          className="border-y border-line bg-bg-raised"
        >
          <div className="mx-auto max-w-content px-step5 py-step9">
            <SectionHeading
              eyebrow="Industries"
              title="Nine verticals, not one generic bot."
              lede="Each vertical ships its own intake questions, escalation rules, and vocabulary. A dental abscess and a burst pipe are both urgent; they are not urgent in the same way."
            />
            <div className="mt-step6 grid gap-step5 md:grid-cols-3">
              {INDUSTRIES.map((v) => (
                <Card key={v.name} className="flex h-full flex-col p-step5">
                  <div className="flex flex-col gap-3">
                    <Badge variant="accent" glyph={null}>
                      {v.badge}
                    </Badge>
                    <h3 className="font-sans text-section font-semibold text-fg">
                      {v.name}
                    </h3>
                    <p className="text-body-sm text-fg-muted">{v.body}</p>
                  </div>
                  <p className="mt-4 border-t border-line pt-4 text-body-sm text-fg-subtle">
                    {v.proof}
                  </p>
                </Card>
              ))}
            </div>
          </div>
        </section>

        {/* -------------------------------------------------- why it works -- */}
        <section id="why-it-works" className="mx-auto max-w-content px-step5 py-step9">
          <SectionHeading
            eyebrow="Why it works"
            title="The difference is what happens on turn three."
            lede="Any system can read a greeting. The value shows up when the caller says something the script did not anticipate."
          />
          <div className="mt-step6 grid gap-step5 sm:grid-cols-2">
            {REASONS.map((r) => (
              <div
                key={r.title}
                className="flex flex-col gap-2 border-l-2 border-accent pl-step5"
              >
                <h3 className="font-sans text-section font-semibold text-fg">
                  {r.title}
                </h3>
                <p className="max-w-prose text-body-sm text-fg-muted">
                  {r.body}
                </p>
              </div>
            ))}
          </div>
        </section>

        {/* ----------------------------------------------------------- faq -- */}
        <section id="faq" className="border-t border-line bg-bg-raised">
          <div className="mx-auto max-w-content px-step5 py-step9">
            <SectionHeading
              eyebrow="FAQ"
              title="The questions practices actually ask."
              lede="Mostly about disclosure, escalation, and what happens when it gets something wrong."
            />
            <div className="mt-step6 max-w-3xl">
              <Faq />
            </div>
          </div>
        </section>

        {/* ---------------------------------------------------- closing cta -- */}
        <section className="mx-auto max-w-content px-step5 py-step9">
          <div className="flex flex-col items-start gap-step5 rounded-card border border-line bg-bg-inset p-step7">
            <h2 className="max-w-prose font-display text-page-title text-fg">
              Hear it answer your own phone.
            </h2>
            <p className="max-w-prose text-body-sm text-fg-muted">
              Talk to it right here in your browser. ReceptionAi plays the part
              of your front desk, books a real appointment, and you can try to
              break it.
            </p>
            <Button size="lg" onClick={openVoice}>
              Request a call
            </Button>
          </div>
        </section>
      </main>

      <SiteFooter />
    </>
  );
}

function SectionHeading({
  eyebrow,
  title,
  lede,
}: {
  eyebrow: string;
  title: string;
  lede: string;
}) {
  return (
    <div className="flex flex-col gap-3">
      <p className="font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)] text-fg-subtle">
        {eyebrow}
      </p>
      <h2 className="max-w-prose font-display text-page-title text-fg">
        {title}
      </h2>
      <p className="max-w-prose text-body-sm text-fg-muted">{lede}</p>
    </div>
  );
}

function SiteFooter() {
  return (
    <footer className="border-t border-line bg-bg-header">
      <div className="mx-auto flex max-w-content flex-col gap-step6 px-step5 py-step7">
        <div className="flex flex-wrap items-start justify-between gap-step6">
          <div className="flex max-w-sm flex-col gap-3">
            <Wordmark />
            <p className="text-body-sm text-fg-muted">
              The front desk that always picks up. Built for practices where a
              missed call is a missed patient.
            </p>
          </div>

          <nav aria-label="Footer" className="flex gap-step8">
            <FooterColumn
              heading="Product"
              links={[
                { href: "#how-it-works", label: "How it works" },
                { href: "#industries", label: "Industries" },
                { href: "#why-it-works", label: "Why it works" },
                { href: "#faq", label: "FAQ" },
              ]}
            />
            <FooterColumn
              heading="Company"
              links={[
                { href: "/security", label: "Security" },
                { href: "/privacy", label: "Privacy" },
                { href: "/terms", label: "Terms" },
                { href: "mailto:hello@receptionai.dev", label: "Contact" },
              ]}
            />
          </nav>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-step5">
          <p className="text-body-sm text-fg-subtle">
            © {new Date().getFullYear()} ReceptionAi. All rights reserved.
          </p>
          <p className="text-body-sm text-fg-subtle">
            HIPAA-ready · SOC 2 Type II in progress
          </p>
        </div>
      </div>
    </footer>
  );
}

function FooterColumn({
  heading,
  links,
}: {
  heading: string;
  links: { href: string; label: string }[];
}) {
  return (
    <div className="flex flex-col gap-3">
      <h2 className="font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)] text-fg-subtle">
        {heading}
      </h2>
      <ul className="flex flex-col gap-2">
        {links.map((l) => (
          <li key={l.label}>
            <a
              href={l.href}
              className="rounded-pill text-body-sm text-fg-muted transition-colors duration-micro hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
            >
              {l.label}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
