/**
 * API types.
 *
 * In the real build these are GENERATED from the OpenAPI document (06 §4.3)
 * so a backend change that the UI does not know about is a TypeScript error
 * rather than a runtime surprise. Phase 1's contract is narrow
 * (PHASE1-API-CONTRACT.md), so the types beyond it are written here against
 * the documented shapes in 01-DATA-MODEL and 06 §1, ready to be replaced by
 * codegen output with no call-site changes.
 *
 * Conventions from PHASE1-API-CONTRACT.md, applied throughout:
 *   - all ids are UUIDv7 STRINGS
 *   - all timestamps are RFC 3339 UTC with `Z`
 *   - money is a decimal STRING plus an ISO-4217 currency, never a float
 */

/** Cursor pagination. Offset paging is not offered — the conversation tables
 *  are partitioned and offset cannot prune. There is no total count. */
export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
  /** Whether another page exists. Answered by fetching `limit + 1` rows
   *  server-side, never by a COUNT over a partitioned table. */
  has_more?: boolean;
}

export interface Money {
  amount: string; // decimal string, e.g. "85.00" — never a float
  currency: string; // ISO 4217
}

// --- Principal -------------------------------------------------------------

export type RoleKey = "owner" | "admin" | "manager" | "agent" | "viewer";

/** GET /api/v1/me — drives RBAC-conditional UI. */
export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  avatar_url?: string;
  active_tenant_id: string;
  tenants: TenantSummary[];
  roles: RoleKey[];
  permissions: string[];
  /** Authenticator assurance level. Step-up routes require >= 2 (05 §A2). */
  aal: number;
  onboarding_complete: boolean;
}

export interface TenantSummary {
  id: string;
  name: string;
  plan: "starter" | "growth" | "scale";
  vertical_key: string;
  manifest_version: string;
  locale: string;
  timezone: string;
  currency: string;
  environment: "production" | "staging" | "sandbox";
}

// --- Conversations ---------------------------------------------------------

export type Channel = "voice_inbound" | "voice_outbound" | "web_chat" | "sms" | "whatsapp";

export type ConversationOutcome =
  /** The four the runtime writes to `conversations.outcome` (migration 0004). */
  | "resolved"
  | "booked"
  | "transferred"
  | "abandoned"
  /** Pre-0004 rows, where the API still derives an outcome it cannot store.
   *  `answered` in particular is not a value the database knows. */
  | "rescheduled"
  | "cancelled"
  | "answered"
  | "message_taken"
  | "failed";

/** The API's `conversation_status` enum. `completed` is the frontend's older
 *  spelling of `ended`; both are accepted so a stored filter does not break. */
export type ConversationStatus = "active" | "ended" | "completed" | "failed" | "abandoned";

export interface Conversation {
  id: string;
  channel: Channel;
  status: ConversationStatus;
  outcome: ConversationOutcome | null;
  /** E.164. Masked to `conversations:read_pii` holders only. */
  caller_number: string | null;
  caller_name: string | null;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  turn_count: number;
  /** Whether the assistant handled it without a human (the containment metric). */
  contained: boolean;
  median_reply_ms: number | null;
  cost: Money | null;
  summary: string | null;
  intent: string | null;
  sentiment: "positive" | "neutral" | "negative" | null;
}

/**
 * One row of `GET /api/v1/conversations` — the wire shape, as the API sends it.
 *
 * Distinct from `Conversation` above, which is the richer shape the mock data
 * and the detail screen use. The API cannot currently populate every field of
 * that (there is no `outcomes` table yet, and no per-conversation summary
 * text), and inventing values to fill the gap would put numbers on screen that
 * nothing stands behind. `toConversation` in lib/query/hooks widens this into
 * the UI shape, leaving the unknowable fields null.
 *
 * PII NOTE: `opening_line`, `contact_name` and `contact_identifier` are null
 * for any principal without `conversations:read_pii`. That masking happens
 * SERVER-SIDE — these are already null on the wire, not hidden at render time.
 */
export interface ConversationSummaryDto {
  id: string;
  channel: Channel;
  status: ConversationStatus;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string | null;
  duration_seconds: number | null;
  turn_count: number;
  outcome: ConversationOutcome | null;
  escalated: boolean;
  contained: boolean;
  agent_code: string | null;
  opening_line: string | null;
  contact_identifier: string | null;
  contact_name: string | null;
}

/** `GET /api/v1/conversations/{id}` — the transcript view's wire shape. */
export interface ConversationDetailDto {
  id: string;
  channel: Channel;
  status: ConversationStatus;
  started_at: string;
  ended_at: string | null;
  turn_count: number;
  agent_code: string | null;
  agent_version: string | null;
  escalated: boolean;
  outcome: ConversationOutcome | null;
  turns: TurnDto[];
}

/** One turn as the API sends it. `role` is the DB vocabulary
 *  (`user`/`assistant`/…), which the UI maps to CALLER/DESK. */
export interface TurnDto {
  id: string;
  sequence: number;
  role: "user" | "assistant" | "system" | "tool" | "event";
  /** Null or a redaction notice without `conversations:read_pii`. */
  content: string | null;
  /** Which agent in the graph handled this turn — makes multi-agent routing
   *  visible, which is the point of showing it. */
  agent_node: string | null;
  created_at: string;
  latency_ms: number | null;
  finish_reason: string | null;
  /** A reply the caller never heard: they were still talking when it was
   *  ready, so the voice worker discarded it and answered the whole thought. */
  interrupted?: boolean;
  usage: { prompt_tokens: number; completion_tokens: number } | null;
}

export type TurnRole = "caller" | "desk" | "system";

export interface ToolCall {
  id: string;
  tool_key: string;
  label: string;
  status: "running" | "succeeded" | "failed";
  duration_ms: number | null;
  /** Redacted server-side for PII-bearing arguments. */
  arguments_preview: string | null;
  result_preview: string | null;
}

export interface Turn {
  id: string;
  role: TurnRole;
  content: string;
  /** Which agent handled this turn, when the backend recorded one. */
  agent_node?: string | null;
  started_at: string;
  /** Time from the end of the caller's turn to the first token of the reply. */
  latency_ms: number | null;
  tool_calls: ToolCall[];
  finish_reason: "stop" | "length" | "tool_calls" | "error" | null;
  /** Generated but never spoken — the caller talked over it. */
  interrupted?: boolean;
  usage: { prompt_tokens: number; completion_tokens: number } | null;
}

export interface ConversationDetail extends Conversation {
  turns: Turn[];
  transfer_reason: string | null;
  recording_url: string | null;
}

// --- Bookings --------------------------------------------------------------

export type BookingStatus = "confirmed" | "pending" | "cancelled" | "completed" | "no_show";

export interface Booking {
  id: string;
  reference: string;
  status: BookingStatus;
  service_id: string;
  service_name: string;
  resource_name: string | null;
  customer_name: string;
  customer_phone: string | null;
  starts_at: string;
  ends_at: string;
  created_via: Channel | "manual";
  price: Money | null;
  notes: string | null;
}

// --- The diary -------------------------------------------------------------

/**
 * A resource a booking consumes — `GET /api/v1/resources`.
 *
 * Generic on the wire (01 §5): a resource is a person, a room, or a piece of
 * equipment, and `resource_type_code` says which. The word "clinician" is a
 * value in that field for a dental tenant, never a type in this file — the
 * same screen serves a salon's stylists and a restaurant's tables.
 */
export interface BookingResource {
  id: string;
  code: string;
  name: string;
  resource_type_code: string | null;
  attributes: Record<string, unknown>;
}

/** One booking as the day view renders it — `GET /api/v1/bookings?date=`. */
export interface CalendarBooking {
  id: string;
  reference: string;
  status: BookingStatus;
  starts_at: string;
  ends_at: string;
  /** The business's own calendar day. Never re-derived from `starts_at`. */
  local_date: string;
  timezone: string;
  service_id: string;
  service_code: string;
  service_name: string;
  /** Null when the principal lacks `conversations:read_pii`, not just absent. */
  customer_name: string | null;
  customer_phone: string | null;
  resources: BookingResource[];
  price: Money | null;
}

export interface CalendarDay {
  /** The single day asked for, or null when a range was. */
  date: string | null;
  /** Both ends of what was actually queried, inclusive. */
  from_date: string;
  to_date: string;
  timezone: string;
  items: CalendarBooking[];
}

// --- Services --------------------------------------------------------------

export interface Service {
  id: string;
  name: string;
  description: string | null;
  duration_minutes: number;
  price: Money | null;
  is_active: boolean;
  category: string | null;
  requires_deposit: boolean;
  buffer_after_minutes: number;
  /** Optimistic-concurrency version; PATCH without If-Match is a 428 (06 §2.6). */
  version: number;
}

// --- Hours -----------------------------------------------------------------

/** 0 = Monday, per ISO-8601 weekday ordering used throughout the product. */
export type Weekday = 0 | 1 | 2 | 3 | 4 | 5 | 6;

export interface HoursInterval {
  /** "HH:MM" in the tenant's timezone. */
  opens: string;
  closes: string;
}

export interface WeeklyHours {
  weekday: Weekday;
  closed: boolean;
  intervals: HoursInterval[];
}

export interface HolidayException {
  id: string;
  name: string;
  /** "YYYY-MM-DD" in the tenant's timezone. */
  date: string;
  closed: boolean;
  intervals: HoursInterval[];
  recurring_annually: boolean;
}

// --- AI config -------------------------------------------------------------

export interface ModelOption {
  id: string;
  provider: "anthropic" | "openai" | "google" | "azure";
  label: string;
  context_window: number;
  /** Cost per million tokens, as decimal strings. */
  input_cost_per_mtok: string;
  output_cost_per_mtok: string;
  supports_tools: boolean;
  latency_class: "fastest" | "balanced" | "deepest";
}

/**
 * The layered prompt composition (03 §2). The portal shows the platform and
 * vertical layers READ-ONLY above the tenant's editable layer, because a
 * tenant editing text they did not write and cannot see the origin of is how
 * prompt regressions get introduced.
 */
export interface PromptLayer {
  key: "platform" | "vertical" | "tenant";
  label: string;
  description: string;
  content: string;
  editable: boolean;
  /** Where this layer came from, shown so the reader can trace it. */
  source: string;
  token_estimate: number;
}

export interface AiConfig {
  model_id: string;
  temperature: number;
  max_output_tokens: number;
  layers: PromptLayer[];
  tools_enabled: string[];
  fallback_model_id: string | null;
  version: number;
}

// --- Knowledge -------------------------------------------------------------

export interface KnowledgeSource {
  id: string;
  name: string;
  kind: "upload" | "website" | "faq" | "connector";
  status: "ready" | "ingesting" | "failed" | "stale";
  document_count: number;
  chunk_count: number;
  last_ingested_at: string | null;
  error: string | null;
}

// --- Channels --------------------------------------------------------------

export interface ChannelConfig {
  id: string;
  channel: Channel;
  label: string;
  status: "connected" | "degraded" | "disconnected";
  /** E.164 for telephony channels, handle for messaging ones. */
  address: string | null;
  provider: string;
  last_event_at: string | null;
  detail: string | null;
}

// --- Members / RBAC --------------------------------------------------------

export interface Member {
  id: string;
  user_id: string;
  display_name: string;
  email: string;
  role: RoleKey;
  status: "active" | "invited" | "suspended";
  last_active_at: string | null;
  invited_at: string;
}

// --- Audit -----------------------------------------------------------------

export interface AuditEntry {
  id: string;
  occurred_at: string;
  actor_name: string;
  actor_email: string;
  action: string;
  target_type: string;
  target_label: string;
  ip: string | null;
  /** RFC 9457 request id, echoed into logs and traces. */
  request_id: string;
  outcome: "allowed" | "denied";
}

// --- Analytics -------------------------------------------------------------

export interface SeriesPoint {
  /** RFC 3339 bucket start. */
  t: string;
  v: number;
}

export interface AnalyticsSeries {
  key: string;
  label: string;
  points: SeriesPoint[];
  /** The trailing bucket is still filling (06 §1.2). Rendered dashed. */
  partial_tail?: boolean;
}
