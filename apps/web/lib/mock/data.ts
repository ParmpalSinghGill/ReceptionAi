/**
 * Mock data for the screens Phase 1's API does not serve yet.
 *
 * PHASE1-API-CONTRACT.md's non-goals are explicit: bookings, knowledge
 * upload, voice, billing and the workflow editor are not in the skeleton. The
 * portal still has to be designed, reviewed and usability-tested against
 * those screens, so they render against this module and swap to `apiFetch`
 * one query at a time.
 *
 * Everything here is PLAUSIBLE — a dental practice in Bristol with real-looking
 * UK numbers, names drawn from a realistic patient mix, timestamps that fall in
 * business hours and durations that match the call types. Placeholder data that
 * reads as "Foo Bar / test test" hides exactly the layout bugs a mock is
 * supposed to surface: how a 34-character name wraps in a table cell, what a
 * missing phone number looks like, whether the transcript handles a caller who
 * interrupts.
 *
 * The generator is SEEDED, so every reload shows the same portal. A dashboard
 * whose numbers change on refresh makes it impossible to tell a real
 * regression from noise.
 */

import type {
  AiConfig,
  AnalyticsSeries,
  AuditEntry,
  Booking,
  BookingResource,
  CalendarBooking,
  CalendarDay,
  ChannelConfig,
  Conversation,
  ConversationDetail,
  ConversationOutcome,
  HolidayException,
  KnowledgeSource,
  Me,
  Member,
  Service,
  TenantSummary,
  Turn,
  WeeklyHours,
} from "@/lib/api/types";

// --- Deterministic RNG -----------------------------------------------------

/** mulberry32 — small, fast, and stable across runs so the portal does not
 *  reshuffle itself on every render. */
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rand = rng(20260805);
const pick = <T>(xs: readonly T[]): T => xs[Math.floor(rand() * xs.length)] as T;
const between = (lo: number, hi: number) => lo + Math.floor(rand() * (hi - lo + 1));

/**
 * A FIXED "now" for the mock world.
 *
 * Using `Date.now()` would make the dashboard's "today" figures drift as the
 * dev server stays up over a lunch break, and the 7-day chart would silently
 * re-bucket. Everything below is relative to this instant.
 */
export const MOCK_NOW = new Date("2026-08-05T16:42:00.000Z");

const iso = (d: Date) => d.toISOString();
const minutesBefore = (m: number) => new Date(MOCK_NOW.getTime() - m * 60_000);
const daysBefore = (d: number) => new Date(MOCK_NOW.getTime() - d * 86_400_000);

// --- Tenants ---------------------------------------------------------------

export const MOCK_TENANTS: TenantSummary[] = [
  {
    id: "019f8c21-4d3a-7c10-9e55-3a1b6d2f8e01",
    name: "Northgate Dental",
    plan: "growth",
    vertical_key: "medical",
    manifest_version: "medical@2.4.1",
    locale: "en-GB",
    timezone: "Europe/London",
    currency: "GBP",
    environment: "production",
  },
  {
    id: "019f8c21-4d3a-7c10-9e55-3a1b6d2f8e02",
    name: "Northgate Dental — Staging",
    plan: "growth",
    vertical_key: "medical",
    manifest_version: "medical@2.5.0-rc.2",
    locale: "en-GB",
    timezone: "Europe/London",
    currency: "GBP",
    environment: "staging",
  },
  {
    id: "019f8c21-4d3a-7c10-9e55-3a1b6d2f8e03",
    name: "Rivergate Veterinary",
    plan: "starter",
    vertical_key: "veterinary",
    manifest_version: "veterinary@1.2.0",
    locale: "en-GB",
    timezone: "Europe/London",
    currency: "GBP",
    environment: "production",
  },
];

export const MOCK_ME: Me = {
  user_id: "019f8c21-51e7-7a44-b3c9-77d2e4f10a55",
  email: "hannah.whitfield@northgatedental.co.uk",
  display_name: "Hannah Whitfield",
  active_tenant_id: MOCK_TENANTS[0]!.id,
  tenants: MOCK_TENANTS,
  roles: ["owner"],
  permissions: [
    "tenant:read", "tenant:update",
    "members:read", "members:invite", "members:update_role", "members:remove",
    "roles:manage",
    "billing:read", "usage:read",
    "conversations:read", "conversations:read_pii", "conversations:export",
    "bookings:read", "bookings:create", "bookings:update", "bookings:cancel",
    "agents:read", "agents:update", "agents:publish",
    "knowledge:read", "knowledge:write", "knowledge:publish",
    "connectors:read", "connectors:manage",
    "channels:read", "channels:manage",
    "apikeys:read", "audit:read",
    "webhooks:manage",
  ],
  // aal 1 deliberately: it lets the step-up path be exercised in the UI. A
  // portal where everything is permitted never shows its own gating.
  aal: 1,
  onboarding_complete: true,
};

// --- People ----------------------------------------------------------------

const CALLERS: Array<{ name: string; number: string }> = [
  { name: "Margaret Ellison", number: "+44 7700 900318" },
  { name: "Dev Patel", number: "+44 7700 900142" },
  { name: "Aoife Brennan", number: "+44 7700 900771" },
  { name: "Tomasz Wiśniewski", number: "+44 7700 900255" },
  { name: "Grace Adeyemi", number: "+44 7700 900604" },
  { name: "Robert Kinnear", number: "+44 117 496 0188" },
  { name: "Priya Raghunathan", number: "+44 7700 900933" },
  { name: "Callum Fraser", number: "+44 7700 900027" },
  { name: "Yusuf Demir", number: "+44 7700 900486" },
  { name: "Eleanor Whitcombe-Hayes", number: "+44 7700 900512" },
  { name: "Sinead O'Callaghan", number: "+44 7700 900369" },
  { name: "Marcus Bell", number: "+44 117 496 0245" },
  { name: "Hiroko Tanaka", number: "+44 7700 900817" },
  { name: "Nathaniel Okonkwo", number: "+44 7700 900158" },
  { name: "Fiona Mackenzie", number: "+44 7700 900740" },
];

const INTENTS = [
  "book_appointment",
  "reschedule",
  "cancel",
  "opening_hours",
  "pricing_enquiry",
  "emergency_triage",
  "prescription_query",
  "insurance_query",
] as const;

const SUMMARIES: Record<string, string[]> = {
  book_appointment: [
    "Booked a 30-minute check-up with Dr Osei for Thursday 13 August at 10:30.",
    "Booked a hygienist appointment for Tuesday 11 August at 09:00.",
    "New patient — booked an initial consultation for Monday 17 August at 15:15.",
  ],
  reschedule: [
    "Moved Friday's crown fitting to Wednesday 12 August at 14:00.",
    "Rescheduled a hygienist visit from 6 August to 20 August at 11:45.",
  ],
  cancel: [
    "Cancelled Thursday's check-up; caller is travelling and will call back in September.",
    "Cancelled a filling appointment inside the 24-hour window; late-cancellation fee applies.",
  ],
  opening_hours: [
    "Confirmed Saturday opening hours (09:00–13:00) and parking arrangements.",
    "Confirmed the practice is closed on the 31 August bank holiday.",
  ],
  pricing_enquiry: [
    "Quoted £68 for a private check-up and explained the NHS band 1 charge.",
    "Explained Invisalign pricing starts at £2,400 and offered a consultation.",
  ],
  emergency_triage: [
    "Severe pain and facial swelling — triaged as urgent, offered the 08:20 emergency slot tomorrow.",
    "Broken tooth, no bleeding — booked into the same-day emergency list at 16:40.",
  ],
  prescription_query: [
    "Antibiotic query passed to Dr Osei; caller will be phoned back before 17:00.",
    "Confirmed a repeat prescription is ready for collection at reception.",
  ],
  insurance_query: [
    "Confirmed Denplan Essentials covers two check-ups a year; sent details by SMS.",
    "Explained the practice does not take Bupa direct billing; caller will claim back.",
  ],
};

// --- Conversations ---------------------------------------------------------

const OUTCOME_BY_INTENT: Record<string, ConversationOutcome[]> = {
  book_appointment: ["booked", "booked", "booked", "abandoned"],
  reschedule: ["rescheduled", "rescheduled", "transferred"],
  cancel: ["cancelled", "cancelled"],
  opening_hours: ["answered", "answered"],
  pricing_enquiry: ["answered", "answered", "message_taken"],
  emergency_triage: ["booked", "transferred", "transferred"],
  prescription_query: ["message_taken", "transferred"],
  insurance_query: ["answered", "message_taken"],
};

function makeConversation(i: number): Conversation {
  const caller = CALLERS[i % CALLERS.length]!;
  const intent = pick(INTENTS);
  const outcome = pick(OUTCOME_BY_INTENT[intent] ?? ["answered"]);
  const channel = pick([
    "voice_inbound", "voice_inbound", "voice_inbound", "voice_inbound",
    "web_chat", "web_chat", "sms", "whatsapp",
  ] as const);

  // Calls cluster in business hours; a mock that spreads them uniformly over
  // 24h makes the "busiest hours" chart meaningless.
  const dayOffset = Math.floor(i / 9);
  const hour = between(8, 18);
  const minute = between(0, 59);
  const started = new Date(MOCK_NOW);
  started.setUTCDate(started.getUTCDate() - dayOffset);
  started.setUTCHours(hour, minute, between(0, 59), 0);

  const duration = outcome === "abandoned" ? between(8, 40) : between(48, 420);
  const contained = !["transferred", "abandoned", "failed"].includes(outcome);
  const turnCount = outcome === "abandoned" ? between(1, 3) : between(4, 16);

  return {
    id: `019f8c${(0x30 + i).toString(16)}-7b21-7d${(i % 90) + 10}-a1c4-${(9e11 + i * 7919).toString().slice(0, 12)}`,
    channel,
    status: "completed",
    outcome,
    caller_number: channel === "web_chat" ? null : caller.number,
    caller_name: rand() > 0.18 ? caller.name : null,
    started_at: iso(started),
    ended_at: iso(new Date(started.getTime() + duration * 1000)),
    duration_seconds: duration,
    turn_count: turnCount,
    contained,
    median_reply_ms: between(720, 2600),
    cost: { amount: (0.012 + rand() * 0.09).toFixed(3), currency: "GBP" },
    summary: pick(SUMMARIES[intent] ?? ["Handled a general enquiry."]),
    intent,
    sentiment: pick(["positive", "positive", "neutral", "neutral", "negative"] as const),
  };
}

export const MOCK_CONVERSATIONS: Conversation[] = Array.from({ length: 64 }, (_, i) =>
  makeConversation(i),
);

/** Calls in progress right now — the dashboard's LiveBadge strip. */
export const MOCK_ACTIVE_CONVERSATIONS: Conversation[] = [
  {
    ...makeConversation(101),
    id: "019f8cf1-7b21-7d01-a1c4-900000000101",
    status: "active",
    outcome: null,
    channel: "voice_inbound",
    caller_name: "Margaret Ellison",
    caller_number: "+44 7700 900318",
    started_at: iso(minutesBefore(2)),
    ended_at: null,
    duration_seconds: null,
    turn_count: 5,
    contained: true,
    intent: "book_appointment",
    summary: null,
  },
  {
    ...makeConversation(102),
    id: "019f8cf1-7b21-7d02-a1c4-900000000102",
    status: "active",
    outcome: null,
    channel: "web_chat",
    caller_name: null,
    caller_number: null,
    started_at: iso(minutesBefore(1)),
    ended_at: null,
    duration_seconds: null,
    turn_count: 2,
    contained: true,
    intent: "pricing_enquiry",
    summary: null,
  },
  {
    ...makeConversation(103),
    id: "019f8cf1-7b21-7d03-a1c4-900000000103",
    status: "active",
    outcome: null,
    channel: "voice_inbound",
    caller_name: "Yusuf Demir",
    caller_number: "+44 7700 900486",
    started_at: iso(minutesBefore(4)),
    ended_at: null,
    duration_seconds: null,
    turn_count: 9,
    contained: true,
    intent: "emergency_triage",
    summary: null,
  },
];

/**
 * A full transcript. Written as a real call rather than a lorem-ipsum
 * exchange, because the transcript viewer's job is to make a real call
 * readable — including the awkward parts: the caller who gives a date before
 * they give a name, the tool call that takes 1.4 seconds, the correction.
 */
const DEMO_TURNS: Turn[] = [
  {
    id: "019f8d01-0001-7000-a000-000000000001",
    role: "desk",
    content: "Good afternoon, Northgate Dental — how can I help?",
    started_at: iso(minutesBefore(14)),
    latency_ms: null,
    tool_calls: [],
    finish_reason: "stop",
    usage: { prompt_tokens: 612, completion_tokens: 14 },
  },
  {
    id: "019f8d01-0002-7000-a000-000000000002",
    role: "caller",
    content:
      "Hi, yes — I've got quite a bad toothache on the bottom left, it's been keeping me up. Is there any chance of getting seen this week?",
    started_at: iso(minutesBefore(14)),
    latency_ms: null,
    tool_calls: [],
    finish_reason: null,
    usage: null,
  },
  {
    id: "019f8d01-0003-7000-a000-000000000003",
    role: "desk",
    content:
      "I'm sorry to hear that — pain that's disturbing your sleep is something we'd want to see quickly. Can I take your name and date of birth so I can find you on the system?",
    started_at: iso(minutesBefore(13)),
    latency_ms: 780,
    tool_calls: [],
    finish_reason: "stop",
    usage: { prompt_tokens: 689, completion_tokens: 38 },
  },
  {
    id: "019f8d01-0004-7000-a000-000000000004",
    role: "caller",
    content: "Margaret Ellison, and it's the 14th of March 1958.",
    started_at: iso(minutesBefore(13)),
    latency_ms: null,
    tool_calls: [],
    finish_reason: null,
    usage: null,
  },
  {
    id: "019f8d01-0005-7000-a000-000000000005",
    role: "desk",
    content:
      "Thank you Margaret — I've found you. You're a registered patient with us and you last saw Dr Osei in February. Because you're in pain I can offer you an emergency assessment slot. Let me check what's free.",
    started_at: iso(minutesBefore(12)),
    latency_ms: 1_240,
    tool_calls: [
      {
        id: "tc_01",
        tool_key: "lookup_patient",
        label: "Look up patient",
        status: "succeeded",
        duration_ms: 340,
        arguments_preview: '{ "surname": "Ellison", "dob": "1958-03-14" }',
        result_preview: '{ "patient_id": "pat_8821", "registered": true, "last_seen": "2026-02-19" }',
      },
    ],
    finish_reason: "tool_calls",
    usage: { prompt_tokens: 754, completion_tokens: 52 },
  },
  {
    id: "019f8d01-0006-7000-a000-000000000006",
    role: "desk",
    content:
      "I have an emergency assessment tomorrow morning at 08:20 with Dr Osei, or there's 16:40 tomorrow afternoon. Would either of those work?",
    started_at: iso(minutesBefore(12)),
    latency_ms: 1_420,
    tool_calls: [
      {
        id: "tc_02",
        tool_key: "check_availability",
        label: "Check availability",
        status: "succeeded",
        duration_ms: 1_090,
        arguments_preview: '{ "service": "emergency_assessment", "window_days": 2 }',
        result_preview: '{ "slots": ["2026-08-06T07:20:00Z", "2026-08-06T15:40:00Z"] }',
      },
    ],
    finish_reason: "stop",
    usage: { prompt_tokens: 812, completion_tokens: 41 },
  },
  {
    id: "019f8d01-0007-7000-a000-000000000007",
    role: "caller",
    content: "The morning one, please. Sorry — actually, can you make it the afternoon? I've got someone coming at nine.",
    started_at: iso(minutesBefore(11)),
    latency_ms: null,
    tool_calls: [],
    finish_reason: null,
    usage: null,
  },
  {
    id: "019f8d01-0008-7000-a000-000000000008",
    role: "desk",
    content:
      "Of course — the afternoon it is. I've booked you in for tomorrow, Thursday 6 August, at 16:40 with Dr Osei for an emergency assessment. You'll get a text confirmation shortly. In the meantime, ibuprofen taken with food usually helps more than paracetamol for dental pain, but do call 111 if the swelling spreads or you have trouble swallowing.",
    started_at: iso(minutesBefore(11)),
    latency_ms: 1_680,
    tool_calls: [
      {
        id: "tc_03",
        tool_key: "create_booking",
        label: "Create booking",
        status: "succeeded",
        duration_ms: 1_240,
        arguments_preview:
          '{ "patient_id": "pat_8821", "service": "emergency_assessment", "starts_at": "2026-08-06T15:40:00Z" }',
        result_preview: '{ "booking_id": "bkg_44192", "reference": "NGD-4419", "status": "confirmed" }',
      },
      {
        id: "tc_04",
        tool_key: "send_sms",
        label: "Send SMS confirmation",
        status: "succeeded",
        duration_ms: 410,
        arguments_preview: '{ "to": "+44 7700 900318", "template": "booking_confirmed" }',
        result_preview: '{ "message_id": "sms_77213", "queued": true }',
      },
    ],
    finish_reason: "stop",
    usage: { prompt_tokens: 918, completion_tokens: 96 },
  },
  {
    id: "019f8d01-0009-7000-a000-000000000009",
    role: "caller",
    content: "That's brilliant, thank you so much.",
    started_at: iso(minutesBefore(10)),
    latency_ms: null,
    tool_calls: [],
    finish_reason: null,
    usage: null,
  },
  {
    id: "019f8d01-0010-7000-a000-000000000010",
    role: "desk",
    content: "You're very welcome, Margaret. I hope you get some rest tonight — see you tomorrow afternoon.",
    started_at: iso(minutesBefore(10)),
    latency_ms: 640,
    tool_calls: [],
    finish_reason: "stop",
    usage: { prompt_tokens: 1_021, completion_tokens: 24 },
  },
];

export function mockConversationDetail(id: string): ConversationDetail {
  const base =
    MOCK_CONVERSATIONS.find((c) => c.id === id) ??
    MOCK_ACTIVE_CONVERSATIONS.find((c) => c.id === id) ??
    MOCK_CONVERSATIONS[0]!;

  // The worked transcript is a spoken phone call, so the detail view presents
  // it as one. Showing a phone conversation under a "WhatsApp" heading — with
  // per-turn speech latencies — is the kind of incoherence that makes a
  // reviewer distrust every other number on the screen.
  const turnLatencies = DEMO_TURNS.map((t) => t.latency_ms).filter(
    (n): n is number => n !== null,
  );
  const sorted = [...turnLatencies].sort((a, b) => a - b);
  const median = sorted.length
    ? sorted.length % 2
      ? sorted[(sorted.length - 1) / 2]!
      : Math.round((sorted[sorted.length / 2 - 1]! + sorted[sorted.length / 2]!) / 2)
    : null;

  return {
    ...base,
    channel: "voice_inbound",
    caller_number: base.caller_number ?? "+44 7700 900318",
    // Every conversation shows the full worked transcript. Generating a
    // different plausible transcript per id is more mock than this needs; the
    // detail screen's job is to render ONE call well — but the SUMMARY figures
    // must then describe that transcript rather than the list row's.
    turns: DEMO_TURNS,
    turn_count: DEMO_TURNS.length,
    median_reply_ms: median,
    transfer_reason:
      base.outcome === "transferred" ? "Caller asked to speak to a named clinician" : null,
    recording_url: "/recordings/demo.mp3",
  };
}

// --- Bookings --------------------------------------------------------------

const SERVICE_NAMES = [
  "Routine check-up",
  "Hygienist — 30 min",
  "Emergency assessment",
  "Composite filling",
  "Crown fitting",
  "Root canal — molar",
  "Invisalign consultation",
  "Child check-up",
];

const CLINICIANS = ["Dr Adaeze Osei", "Dr Simon Hartley", "Marie Colville (Hygienist)"];

export const MOCK_BOOKINGS: Booking[] = Array.from({ length: 28 }, (_, i) => {
  const caller = CALLERS[(i * 3) % CALLERS.length]!;
  const service = SERVICE_NAMES[i % SERVICE_NAMES.length]!;
  const start = new Date(MOCK_NOW);
  start.setUTCDate(start.getUTCDate() + Math.floor(i / 4) - 2);
  start.setUTCHours(between(8, 17), pick([0, 15, 30, 45]), 0, 0);
  const durationMin = service.includes("Root canal") ? 90 : service.includes("Crown") ? 60 : 30;

  return {
    id: `019f8e${(0x40 + i).toString(16)}-7c33-7e${(i % 90) + 10}-b2d5-${(8e11 + i * 6733).toString().slice(0, 12)}`,
    reference: `NGD-${4200 + i * 7}`,
    status: pick([
      "confirmed", "confirmed", "confirmed", "confirmed",
      "pending", "completed", "cancelled", "no_show",
    ] as const),
    service_id: `svc_${(i % SERVICE_NAMES.length) + 1}`,
    service_name: service,
    resource_name: CLINICIANS[i % CLINICIANS.length]!,
    customer_name: caller.name,
    customer_phone: caller.number,
    starts_at: iso(start),
    ends_at: iso(new Date(start.getTime() + durationMin * 60_000)),
    created_via: pick(["voice_inbound", "voice_inbound", "web_chat", "manual", "whatsapp"] as const),
    price: { amount: pick(["25.80", "68.00", "34.00", "145.00", "320.00", "0.00"]), currency: "GBP" },
    notes: rand() > 0.75 ? "Patient asked for a ground-floor surgery." : null,
  };
});

// --- The diary (calendar day view) -----------------------------------------

/**
 * Resources, in the generic sense 01 §5 means: this tenant's are people, and
 * a `resource_type_code` of "clinician" is a value their manifest chose. The
 * calendar filters on the TYPE CODE, never on the word "clinician" being
 * hardcoded in a component.
 *
 * These ids are UUIDv7-shaped because the cache guard (06 §11.2, mechanism 3)
 * only checks the tenant prefix, but a resource id that looked like `res_1`
 * would hide the day the query key factory is passed the wrong thing.
 */
export const MOCK_RESOURCES: BookingResource[] = [
  {
    id: "019f8d10-2a41-7c02-9f31-4b7e10c5a001",
    code: "dr-osei",
    name: "Dr Adaeze Osei",
    resource_type_code: "clinician",
    attributes: { scope: "General dentistry and same-day emergencies" },
  },
  {
    id: "019f8d10-2a41-7c02-9f31-4b7e10c5a002",
    code: "dr-hartley",
    name: "Dr Simon Hartley",
    resource_type_code: "clinician",
    attributes: { scope: "Root canal treatment and restorative work" },
  },
  {
    id: "019f8d10-2a41-7c02-9f31-4b7e10c5a003",
    code: "dr-whitcombe",
    name: "Dr Elena Whitcombe",
    resource_type_code: "clinician",
    attributes: { scope: "Cosmetic dentistry" },
  },
  {
    id: "019f8d10-2a41-7c02-9f31-4b7e10c5a004",
    code: "hyg-colville",
    name: "Marie Colville",
    resource_type_code: "clinician",
    attributes: { scope: "Hygiene" },
  },
  {
    id: "019f8d10-2a41-7c02-9f31-4b7e10c5b001",
    code: "surgery-1",
    name: "Surgery 1",
    resource_type_code: "treatment_room",
    attributes: { step_free: true },
  },
  {
    id: "019f8d10-2a41-7c02-9f31-4b7e10c5b002",
    code: "surgery-2",
    name: "Surgery 2",
    resource_type_code: "treatment_room",
    attributes: { step_free: false },
  },
];

const MOCK_CLINICIANS = MOCK_RESOURCES.filter(
  (r) => r.resource_type_code === "clinician",
);
const MOCK_ROOMS = MOCK_RESOURCES.filter(
  (r) => r.resource_type_code === "treatment_room",
);

/** (HH:MM local, service, clinician index, minutes) — a plausible working day. */
const DIARY_PLAN: Array<[string, string, number, number]> = [
  ["09:00", "Routine check-up", 0, 30],
  ["09:00", "Hygienist", 3, 30],
  ["09:30", "New patient examination", 1, 30],
  ["10:00", "Routine check-up", 0, 30],
  ["10:30", "Emergency assessment", 1, 30],
  ["11:00", "Invisalign consultation", 2, 60],
  ["11:00", "Hygienist", 3, 30],
  ["12:00", "Composite filling", 0, 30],
  ["13:30", "Routine check-up", 0, 30],
  ["14:00", "Crown fitting", 2, 60],
  ["14:30", "Child check-up", 1, 30],
  ["15:30", "Emergency assessment", 1, 30],
  ["16:00", "Routine check-up", 0, 30],
  ["16:30", "Hygienist", 3, 30],
  ["17:00", "Root canal — molar", 1, 60],
];

const DIARY_PRICES: Record<string, string> = {
  "Routine check-up": "35.00",
  Hygienist: "68.00",
  "New patient examination": "45.00",
  "Emergency assessment": "75.00",
  "Invisalign consultation": "0.00",
  "Composite filling": "120.00",
  "Crown fitting": "320.00",
  "Child check-up": "25.00",
  "Root canal — molar": "410.00",
};

/**
 * A tenant-local wall-clock time as the UTC instant it actually is.
 *
 * The mock has to do this properly rather than emitting `${date}T09:00:00Z`,
 * because Europe/London is UTC+1 for seven months of the year: the naive form
 * puts every summer appointment on screen an hour late, and the grid would be
 * tuned against times no booking actually has. Same derivation the server does
 * when it writes `local_date` — modelled here so the mock and the API agree.
 *
 * Two passes: format a guess back into the zone, measure how far off it landed,
 * and subtract. One pass is enough for every real offset; the second settles
 * the hour either side of a DST boundary.
 */
function localWallClockToUtc(date: string, hhmm: string, timezone: string): Date {
  let instant = new Date(`${date}T${hhmm}:00.000Z`);
  for (let pass = 0; pass < 2; pass++) {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: timezone,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(instant);
    const hour = Number(parts.find((p) => p.type === "hour")?.value ?? "0") % 24;
    const minute = Number(parts.find((p) => p.type === "minute")?.value ?? "0");
    const [wantHour, wantMinute] = hhmm.split(":").map(Number) as [number, number];
    const driftMinutes = hour * 60 + minute - (wantHour * 60 + wantMinute);
    if (driftMinutes === 0) break;
    instant = new Date(instant.getTime() - driftMinutes * 60_000);
  }
  return instant;
}

/**
 * One mocked day of the diary.
 *
 * Generated per date rather than stored as a fixture so date navigation has
 * somewhere to navigate TO — a fixture for one day would make "next day" look
 * like a bug. Weekends come back empty on purpose: the empty state is a screen
 * that has to be designed, and a mock where every day is busy never shows it.
 */
export function mockCalendarDay(date: string): CalendarDay {
  const weekday = new Date(`${date}T12:00:00Z`).getUTCDay();
  const timezone = "Europe/London";
  if (weekday === 0 || weekday === 6)
    return { date, from_date: date, to_date: date, timezone, items: [] };

  // A per-date seed: the same day always looks the same, and two different
  // days do not. A shared generator would reshuffle yesterday when you look
  // at tomorrow.
  const dayRand = rng(Number(date.replaceAll("-", "")));
  const items = DIARY_PLAN.map((entry, i): CalendarBooking => {
    const [hhmm, service, clinicianIndex, minutes] = entry;
    const start = localWallClockToUtc(date, hhmm, timezone);
    const end = new Date(start.getTime() + minutes * 60_000);
    const caller = CALLERS[(i * 5 + weekday * 3) % CALLERS.length]!;
    const clinician = MOCK_CLINICIANS[clinicianIndex]!;
    const room = MOCK_ROOMS[i % MOCK_ROOMS.length]!;
    const roll = dayRand();

    return {
      id: `019f8d1${weekday}-2a41-7c02-9f31-4b7e10c5c0${String(i).padStart(2, "0")}`,
      reference: `NGD-${5100 + weekday * 40 + i * 3}`,
      status: roll > 0.93 ? "cancelled" : roll > 0.88 ? "pending" : "confirmed",
      starts_at: iso(start),
      ends_at: iso(end),
      local_date: date,
      timezone,
      service_id: `svc_${clinicianIndex + 1}`,
      service_code: service.toLowerCase().replaceAll(" ", "-"),
      service_name: service,
      customer_name: caller.name,
      customer_phone: caller.number,
      resources: [clinician, room],
      price: { amount: DIARY_PRICES[service] ?? "0.00", currency: "GBP" },
    };
  })
    // Cancelled bookings are hidden by the real endpoint's default, so the
    // mock hides them too — otherwise the two worlds disagree about how full
    // a day looks, and the layout gets tuned against the wrong density.
    .filter((b) => b.status !== "cancelled");

  return { date, from_date: date, to_date: date, timezone, items };
}

/**
 * The list view's rows, over a range of days.
 *
 * Mirrors the server's semantics exactly — including that "upcoming" is
 * measured against the appointment's END, so one that is currently running
 * stays in the list. A mock that filtered on `starts_at` would let the two
 * worlds disagree about the single case most likely to be got wrong, and the
 * bug would only appear once the API was switched on.
 */
export function mockBookingList({
  from,
  to,
  status,
  includePast,
  now = new Date(),
}: {
  from: string;
  to: string;
  status: string;
  includePast: boolean;
  now?: Date;
}): CalendarBooking[] {
  const out: CalendarBooking[] = [];
  for (let day = from; day <= to; day = shiftIsoDay(day, 1)) {
    for (const booking of mockCalendarDay(day).items) {
      if (status !== "all" && booking.status !== status) continue;
      if (!includePast && new Date(booking.ends_at) < now) continue;
      out.push(booking);
    }
  }
  return out.sort((a, b) => a.starts_at.localeCompare(b.starts_at));
}

/** Local to this module so the mock does not depend on the calendar helpers. */
function shiftIsoDay(date: string, delta: number): string {
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}

// --- Services --------------------------------------------------------------

export const MOCK_SERVICES: Service[] = [
  {
    id: "svc_1", name: "Routine check-up",
    description: "Examination, scale and polish assessment, and treatment planning.",
    duration_minutes: 30, price: { amount: "68.00", currency: "GBP" },
    is_active: true, category: "General", requires_deposit: false,
    buffer_after_minutes: 5, version: 7,
  },
  {
    id: "svc_2", name: "Hygienist — 30 min",
    description: "Scale and polish with a registered hygienist.",
    duration_minutes: 30, price: { amount: "62.00", currency: "GBP" },
    is_active: true, category: "Hygiene", requires_deposit: false,
    buffer_after_minutes: 5, version: 4,
  },
  {
    id: "svc_3", name: "Emergency assessment",
    description: "Same-day triage and pain relief. Held open until 15:00 each day.",
    duration_minutes: 20, price: { amount: "85.00", currency: "GBP" },
    is_active: true, category: "Urgent", requires_deposit: false,
    buffer_after_minutes: 10, version: 12,
  },
  {
    id: "svc_4", name: "Composite filling",
    description: "Tooth-coloured filling, single surface.",
    duration_minutes: 45, price: { amount: "145.00", currency: "GBP" },
    is_active: true, category: "Restorative", requires_deposit: false,
    buffer_after_minutes: 10, version: 3,
  },
  {
    id: "svc_5", name: "Crown fitting",
    description: "Fitting appointment following a prepared impression.",
    duration_minutes: 60, price: { amount: "620.00", currency: "GBP" },
    is_active: true, category: "Restorative", requires_deposit: true,
    buffer_after_minutes: 15, version: 5,
  },
  {
    id: "svc_6", name: "Root canal — molar",
    description: "Endodontic treatment. Usually two visits.",
    duration_minutes: 90, price: { amount: "780.00", currency: "GBP" },
    is_active: true, category: "Endodontics", requires_deposit: true,
    buffer_after_minutes: 15, version: 2,
  },
  {
    id: "svc_7", name: "Invisalign consultation",
    description: "Assessment, scan and treatment quotation.",
    duration_minutes: 45, price: { amount: "0.00", currency: "GBP" },
    is_active: true, category: "Orthodontics", requires_deposit: false,
    buffer_after_minutes: 5, version: 9,
  },
  {
    id: "svc_8", name: "Teeth whitening — home kit",
    description: "Impressions, custom trays and gel. Withdrawn pending new supplier.",
    duration_minutes: 30, price: { amount: "295.00", currency: "GBP" },
    is_active: false, category: "Cosmetic", requires_deposit: true,
    buffer_after_minutes: 5, version: 6,
  },
];

// --- Hours -----------------------------------------------------------------

export const MOCK_HOURS: WeeklyHours[] = [
  { weekday: 0, closed: false, intervals: [{ opens: "08:30", closes: "13:00" }, { opens: "14:00", closes: "17:30" }] },
  { weekday: 1, closed: false, intervals: [{ opens: "08:30", closes: "13:00" }, { opens: "14:00", closes: "17:30" }] },
  { weekday: 2, closed: false, intervals: [{ opens: "08:30", closes: "13:00" }, { opens: "14:00", closes: "19:00" }] },
  { weekday: 3, closed: false, intervals: [{ opens: "08:30", closes: "13:00" }, { opens: "14:00", closes: "17:30" }] },
  { weekday: 4, closed: false, intervals: [{ opens: "08:30", closes: "15:00" }] },
  { weekday: 5, closed: false, intervals: [{ opens: "09:00", closes: "13:00" }] },
  { weekday: 6, closed: true, intervals: [] },
];

export const MOCK_HOLIDAYS: HolidayException[] = [
  { id: "hol_1", name: "Summer bank holiday", date: "2026-08-31", closed: true, intervals: [], recurring_annually: false },
  { id: "hol_2", name: "Staff training afternoon", date: "2026-09-16", closed: false, intervals: [{ opens: "08:30", closes: "12:30" }], recurring_annually: false },
  { id: "hol_3", name: "Christmas Day", date: "2026-12-25", closed: true, intervals: [], recurring_annually: true },
  { id: "hol_4", name: "Boxing Day", date: "2026-12-26", closed: true, intervals: [], recurring_annually: true },
  { id: "hol_5", name: "New Year's Day", date: "2027-01-01", closed: true, intervals: [], recurring_annually: true },
];

// --- AI config -------------------------------------------------------------

export const MOCK_MODELS = [
  {
    id: "claude-sonnet-4-6", provider: "anthropic" as const, label: "Claude Sonnet 4.6",
    context_window: 200_000, input_cost_per_mtok: "3.00", output_cost_per_mtok: "15.00",
    supports_tools: true, latency_class: "balanced" as const,
  },
  {
    id: "claude-haiku-4-5", provider: "anthropic" as const, label: "Claude Haiku 4.5",
    context_window: 200_000, input_cost_per_mtok: "1.00", output_cost_per_mtok: "5.00",
    supports_tools: true, latency_class: "fastest" as const,
  },
  {
    id: "claude-opus-4-1", provider: "anthropic" as const, label: "Claude Opus 4.1",
    context_window: 200_000, input_cost_per_mtok: "15.00", output_cost_per_mtok: "75.00",
    supports_tools: true, latency_class: "deepest" as const,
  },
  {
    id: "gpt-4o", provider: "openai" as const, label: "GPT-4o",
    context_window: 128_000, input_cost_per_mtok: "2.50", output_cost_per_mtok: "10.00",
    supports_tools: true, latency_class: "balanced" as const,
  },
  {
    id: "gemini-2-5-flash", provider: "google" as const, label: "Gemini 2.5 Flash",
    context_window: 1_000_000, input_cost_per_mtok: "0.30", output_cost_per_mtok: "2.50",
    supports_tools: true, latency_class: "fastest" as const,
  },
];

export const MOCK_AI_CONFIG: AiConfig = {
  model_id: "claude-sonnet-4-6",
  temperature: 0.3,
  max_output_tokens: 1024,
  fallback_model_id: "claude-haiku-4-5",
  tools_enabled: [
    "check_availability", "create_booking", "reschedule_booking",
    "cancel_booking", "lookup_patient", "send_sms", "search_knowledge",
    "transfer_to_human",
  ],
  version: 23,
  layers: [
    {
      key: "platform",
      label: "Platform layer",
      description:
        "Safety, identity disclosure and escalation rules that apply to every ReceptionAi deployment. Maintained by ReceptionAi and versioned with the platform.",
      editable: false,
      source: "platform@6.2.0",
      token_estimate: 412,
      content: `You are a front-desk assistant answering on behalf of a business. You are not a human and must say so plainly if asked, without evasion or roleplay.

Never invent facts about the business. If you do not know something, say so and offer to take a message or transfer.

Escalate to a human immediately if the caller: reports a medical emergency, expresses intent to harm themselves or others, becomes abusive, or asks three times for a human.

Never collect payment card details. Never repeat a full date of birth or address back on a recorded line beyond what is needed to confirm identity.`,
    },
    {
      key: "vertical",
      label: "Vertical layer — medical",
      description:
        "Clinical-safety and triage rules for the medical vertical. Maintained in packages/verticals and published with the manifest; the tenant cannot edit it, but can see exactly what it says.",
      editable: false,
      source: "medical@2.4.1",
      token_estimate: 587,
      content: `You are answering for a healthcare practice. You must never give clinical advice, diagnose, or recommend a course of treatment. You may relay general practical guidance the practice has approved (for example, standard post-operative instructions) but never tailor it to the caller's situation.

Triage rules — apply in order:
1. Difficulty breathing, uncontrolled bleeding, facial swelling affecting the eye or throat, or a head injury → tell the caller to call 999 immediately, and end with that instruction.
2. Severe pain, swelling, trauma to a tooth, or a lost filling causing pain → offer the same-day emergency list.
3. Everything else → routine booking.

If the caller is under 16 and unaccompanied, take a message for a clinician rather than booking.`,
    },
    {
      key: "tenant",
      label: "Your practice",
      description:
        "Your own instructions. These are appended last and can override tone and detail, but never the safety rules above.",
      editable: true,
      source: "northgate-dental (edited by Hannah Whitfield, 2 August 2026)",
      token_estimate: 214,
      content: `We are Northgate Dental, a mixed NHS and private practice on Gloucester Road in Bristol.

Clinicians: Dr Adaeze Osei (principal), Dr Simon Hartley (Tue/Thu only), Marie Colville (hygienist).

Tone: warm and unhurried. Many of our patients are elderly and dislike being rushed. Use their preferred name once you have it.

Practical notes:
- We hold two emergency slots each morning; do not release them before 15:00 for routine work.
- We do not take Bupa direct billing — patients pay and claim back.
- Parking is on the street, free after 18:00 and on Sundays.
- If someone asks about Invisalign, book a free consultation rather than quoting a total.`,
    },
  ],
};

// --- Knowledge -------------------------------------------------------------

export const MOCK_KNOWLEDGE: KnowledgeSource[] = [
  { id: "kb_1", name: "Practice information pack (PDF)", kind: "upload", status: "ready", document_count: 1, chunk_count: 84, last_ingested_at: iso(daysBefore(11)), error: null },
  { id: "kb_2", name: "northgatedental.co.uk", kind: "website", status: "ready", document_count: 23, chunk_count: 316, last_ingested_at: iso(daysBefore(2)), error: null },
  { id: "kb_3", name: "Patient FAQs", kind: "faq", status: "ready", document_count: 41, chunk_count: 41, last_ingested_at: iso(daysBefore(6)), error: null },
  { id: "kb_4", name: "Treatment price list — August 2026", kind: "upload", status: "ingesting", document_count: 1, chunk_count: 0, last_ingested_at: null, error: null },
  { id: "kb_5", name: "Denplan scheme terms", kind: "upload", status: "failed", document_count: 1, chunk_count: 0, last_ingested_at: iso(daysBefore(1)), error: "The PDF is a scanned image with no text layer. Re-upload with OCR, or paste the text as an FAQ." },
  { id: "kb_6", name: "Dentally patient records", kind: "connector", status: "stale", document_count: 0, chunk_count: 0, last_ingested_at: iso(daysBefore(19)), error: "The connector's credentials expired on 22 July." },
];

// --- Channels --------------------------------------------------------------

export const MOCK_CHANNELS: ChannelConfig[] = [
  { id: "ch_1", channel: "voice_inbound", label: "Main practice line", status: "connected", address: "+44 117 496 0100", provider: "Twilio", last_event_at: iso(minutesBefore(2)), detail: "Diverts from the practice's BT line after 4 rings." },
  { id: "ch_2", channel: "voice_inbound", label: "Out-of-hours line", status: "connected", address: "+44 117 496 0101", provider: "Twilio", last_event_at: iso(minutesBefore(680)), detail: "Active 17:30–08:30 and all day Sunday." },
  { id: "ch_3", channel: "web_chat", label: "Website widget", status: "connected", address: "northgatedental.co.uk", provider: "ReceptionAi", last_event_at: iso(minutesBefore(1)), detail: null },
  { id: "ch_4", channel: "whatsapp", label: "WhatsApp Business", status: "degraded", address: "+44 117 496 0100", provider: "Meta", last_event_at: iso(minutesBefore(47)), detail: "Template 'booking_confirmed' is pending re-approval after an edit on 3 August." },
  { id: "ch_5", channel: "sms", label: "SMS confirmations", status: "connected", address: "NGDental", provider: "Twilio", last_event_at: iso(minutesBefore(11)), detail: "Alphanumeric sender — replies are not delivered." },
  { id: "ch_6", channel: "voice_outbound", label: "Recall campaign line", status: "disconnected", address: null, provider: "Twilio", last_event_at: null, detail: "Not configured. Outbound recall calls are disabled." },
];

// --- Members ---------------------------------------------------------------

export const MOCK_MEMBERS: Member[] = [
  { id: "mem_1", user_id: MOCK_ME.user_id, display_name: "Hannah Whitfield", email: "hannah.whitfield@northgatedental.co.uk", role: "owner", status: "active", last_active_at: iso(minutesBefore(3)), invited_at: iso(daysBefore(412)) },
  { id: "mem_2", user_id: "u_2", display_name: "Adaeze Osei", email: "a.osei@northgatedental.co.uk", role: "admin", status: "active", last_active_at: iso(minutesBefore(94)), invited_at: iso(daysBefore(408)) },
  { id: "mem_3", user_id: "u_3", display_name: "Simon Hartley", email: "s.hartley@northgatedental.co.uk", role: "manager", status: "active", last_active_at: iso(daysBefore(2)), invited_at: iso(daysBefore(300)) },
  { id: "mem_4", user_id: "u_4", display_name: "Marie Colville", email: "m.colville@northgatedental.co.uk", role: "agent", status: "active", last_active_at: iso(daysBefore(1)), invited_at: iso(daysBefore(221)) },
  { id: "mem_5", user_id: "u_5", display_name: "Joanne Peart", email: "reception@northgatedental.co.uk", role: "agent", status: "active", last_active_at: iso(minutesBefore(28)), invited_at: iso(daysBefore(190)) },
  { id: "mem_6", user_id: "u_6", display_name: "Ravi Shankar Iyer", email: "r.iyer@bramleyaccounts.co.uk", role: "viewer", status: "active", last_active_at: iso(daysBefore(9)), invited_at: iso(daysBefore(88)) },
  { id: "mem_7", user_id: "u_7", display_name: "Chloe Baptiste", email: "c.baptiste@northgatedental.co.uk", role: "agent", status: "invited", last_active_at: null, invited_at: iso(daysBefore(3)) },
  { id: "mem_8", user_id: "u_8", display_name: "Daniel Okoro", email: "d.okoro@northgatedental.co.uk", role: "agent", status: "suspended", last_active_at: iso(daysBefore(64)), invited_at: iso(daysBefore(300)) },
];

// --- Audit -----------------------------------------------------------------

export const MOCK_AUDIT: AuditEntry[] = [
  { id: "aud_1", occurred_at: iso(minutesBefore(18)), actor_name: "Hannah Whitfield", actor_email: MOCK_ME.email, action: "agent.config.updated", target_type: "ai_config", target_label: "Temperature 0.4 → 0.3", ip: "82.16.44.201", request_id: "01JQK7X2P4A9", outcome: "allowed" },
  { id: "aud_2", occurred_at: iso(minutesBefore(96)), actor_name: "Adaeze Osei", actor_email: "a.osei@northgatedental.co.uk", action: "service.updated", target_type: "service", target_label: "Emergency assessment — price £80.00 → £85.00", ip: "82.16.44.201", request_id: "01JQK5M8T1B2", outcome: "allowed" },
  { id: "aud_3", occurred_at: iso(minutesBefore(240)), actor_name: "Joanne Peart", actor_email: "reception@northgatedental.co.uk", action: "conversations.export", target_type: "conversation", target_label: "14 conversations, 1–31 July", ip: "82.16.44.201", request_id: "01JQK2Z9R7C4", outcome: "denied" },
  { id: "aud_4", occurred_at: iso(daysBefore(1)), actor_name: "Hannah Whitfield", actor_email: MOCK_ME.email, action: "member.invited", target_type: "member", target_label: "c.baptiste@northgatedental.co.uk as agent", ip: "82.16.44.201", request_id: "01JQJ8K3W5D6", outcome: "allowed" },
  { id: "aud_5", occurred_at: iso(daysBefore(1)), actor_name: "Hannah Whitfield", actor_email: MOCK_ME.email, action: "member.suspended", target_type: "member", target_label: "d.okoro@northgatedental.co.uk", ip: "82.16.44.201", request_id: "01JQJ7F1V2E8", outcome: "allowed" },
  { id: "aud_6", occurred_at: iso(daysBefore(2)), actor_name: "Adaeze Osei", actor_email: "a.osei@northgatedental.co.uk", action: "knowledge.source.created", target_type: "knowledge_source", target_label: "northgatedental.co.uk", ip: "94.12.208.17", request_id: "01JQH4D6Q9F1", outcome: "allowed" },
  { id: "aud_7", occurred_at: iso(daysBefore(3)), actor_name: "Ravi Shankar Iyer", actor_email: "r.iyer@bramleyaccounts.co.uk", action: "billing.invoice.read", target_type: "invoice", target_label: "INV-2026-07", ip: "212.58.233.90", request_id: "01JQG1S5N3G7", outcome: "allowed" },
  { id: "aud_8", occurred_at: iso(daysBefore(4)), actor_name: "Simon Hartley", actor_email: "s.hartley@northgatedental.co.uk", action: "hours.updated", target_type: "hours", target_label: "Wednesday closing 17:30 → 19:00", ip: "31.94.12.7", request_id: "01JQF9B2M8H3", outcome: "allowed" },
  { id: "aud_9", occurred_at: iso(daysBefore(5)), actor_name: "Hannah Whitfield", actor_email: MOCK_ME.email, action: "channel.connected", target_type: "channel", target_label: "WhatsApp Business +44 117 496 0100", ip: "82.16.44.201", request_id: "01JQE6C4L1J5", outcome: "allowed" },
  { id: "aud_10", occurred_at: iso(daysBefore(6)), actor_name: "Joanne Peart", actor_email: "reception@northgatedental.co.uk", action: "booking.cancelled", target_type: "booking", target_label: "NGD-4207 — Tomasz Wiśniewski", ip: "82.16.44.201", request_id: "01JQD3A7K6L9", outcome: "allowed" },
];

// --- Analytics -------------------------------------------------------------

/** Conversation volume by channel over the last 7 days, for the dashboard. */
export function mockVolumeSeries(): AnalyticsSeries[] {
  const days = Array.from({ length: 7 }, (_, i) => {
    const d = daysBefore(6 - i);
    d.setUTCHours(0, 0, 0, 0);
    return iso(d);
  });

  // A dental practice: quiet Sunday, Monday spike, half-day Friday. Shaped
  // rather than random, because a chart of noise cannot be design-reviewed.
  const voiceShape = [12, 47, 39, 41, 36, 22, 8];
  const chatShape = [4, 14, 11, 13, 12, 9, 5];
  const msgShape = [2, 7, 6, 5, 6, 4, 2];

  return [
    {
      key: "voice_inbound", label: "Phone",
      points: days.map((t, i) => ({ t, v: voiceShape[i]! })),
      partial_tail: true,
    },
    {
      key: "web_chat", label: "Web chat",
      points: days.map((t, i) => ({ t, v: chatShape[i]! })),
      partial_tail: true,
    },
    {
      key: "messaging", label: "SMS & WhatsApp",
      points: days.map((t, i) => ({ t, v: msgShape[i]! })),
      partial_tail: true,
    },
  ];
}

/** Outcome mix — a horizontal sorted bar, never a pie (06 §12.5). */
export function mockOutcomeMix(): Array<{ key: string; label: string; value: number }> {
  return [
    { key: "booked", label: "Booked", value: 148 },
    { key: "answered", label: "Answered", value: 96 },
    { key: "rescheduled", label: "Rescheduled", value: 41 },
    { key: "message_taken", label: "Message taken", value: 33 },
    { key: "transferred", label: "Transferred", value: 27 },
    { key: "cancelled", label: "Cancelled", value: 19 },
    { key: "abandoned", label: "Abandoned", value: 11 },
  ];
}

/** Median and p95 reply latency, in milliseconds, over 7 days. */
export function mockLatencySeries(): AnalyticsSeries[] {
  const days = Array.from({ length: 7 }, (_, i) => {
    const d = daysBefore(6 - i);
    d.setUTCHours(0, 0, 0, 0);
    return iso(d);
  });
  const p50 = [1180, 1240, 1310, 1190, 1220, 1160, 1210];
  const p95 = [2840, 3120, 4210, 2960, 3040, 2780, 2910];
  return [
    { key: "p50", label: "Median", points: days.map((t, i) => ({ t, v: p50[i]! })) },
    { key: "p95", label: "95th percentile", points: days.map((t, i) => ({ t, v: p95[i]! })) },
  ];
}

/** Weekday × hour call heatmap. */
export function mockBusiestHours(): Array<{ weekday: number; hour: number; value: number }> {
  const out: Array<{ weekday: number; hour: number; value: number }> = [];
  // Peaks just after opening and just after lunch — the real shape of a
  // practice phone line.
  const hourWeight: Record<number, number> = {
    8: 0.85, 9: 1.0, 10: 0.72, 11: 0.6, 12: 0.38, 13: 0.3,
    14: 0.78, 15: 0.66, 16: 0.5, 17: 0.34, 18: 0.15,
  };
  const dayWeight = [1.0, 0.82, 0.78, 0.8, 0.62, 0.28, 0.06];
  for (let weekday = 0; weekday < 7; weekday++) {
    for (let hour = 8; hour <= 18; hour++) {
      const base = (hourWeight[hour] ?? 0.2) * (dayWeight[weekday] ?? 0.5);
      out.push({ weekday, hour, value: Math.round(base * 14) });
    }
  }
  return out;
}

// --- Dashboard KPIs --------------------------------------------------------

export const MOCK_KPIS = {
  conversationsToday: { value: 38, delta: 0.12, sparkline: [22, 31, 27, 34, 29, 41, 38] },
  bookedToday: { value: 21, delta: 0.08, sparkline: [12, 18, 14, 19, 16, 23, 21] },
  containmentRate: { value: 0.83, delta: 0.03, sparkline: [0.76, 0.79, 0.78, 0.81, 0.8, 0.84, 0.83] },
  medianReply: { value: 1210, delta: -0.04, sparkline: [1280, 1240, 1310, 1190, 1220, 1160, 1210] },
};
