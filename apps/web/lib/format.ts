import type { Money } from "@/lib/api/types";

/**
 * Formatting helpers.
 *
 * All of them take an explicit locale/timezone rather than relying on the
 * browser's: the tenant's timezone is authoritative (a practice in London
 * whose owner is on holiday in Spain must still see London times), and 02
 * §7.2 resolves date boundaries server-side against it for the same reason.
 */

export function formatTime(iso: string, timezone: string, locale = "en-GB"): string {
  return new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(iso));
}

export function formatDate(iso: string, timezone: string, locale = "en-GB"): string {
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: timezone,
  }).format(new Date(iso));
}

export function formatDateTime(iso: string, timezone: string, locale = "en-GB"): string {
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(iso));
}

export function formatWeekdayShort(iso: string, timezone: string, locale = "en-GB"): string {
  return new Intl.DateTimeFormat(locale, { weekday: "short", timeZone: timezone }).format(
    new Date(iso),
  );
}

/** Money is a decimal string plus a currency — never a float. */
export function formatMoney(money: Money | null, locale = "en-GB"): string {
  if (!money) return "—";
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: money.currency,
  }).format(Number(money.amount));
}

/** Durations read as "4m 12s", not "252000ms". */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

/** Latency in milliseconds, at the precision a reader can act on. */
export function formatLatency(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function formatPercent(fraction: number, digits = 0): string {
  return `${(fraction * 100).toFixed(digits)}%`;
}

/** "3 minutes ago". `now` is injectable so the mock world's fixed clock works. */
export function formatRelative(iso: string, now: Date): string {
  const diffMs = now.getTime() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

/** Elapsed time for a call in progress — mm:ss, tabular so it does not jitter. */
export function formatElapsed(startIso: string, now: Date): string {
  const secs = Math.max(0, Math.floor((now.getTime() - new Date(startIso).getTime()) / 1000));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export const CHANNEL_LABELS: Record<string, string> = {
  voice_inbound: "Phone",
  voice_outbound: "Outbound call",
  web_chat: "Web chat",
  sms: "SMS",
  whatsapp: "WhatsApp",
  manual: "Added by hand",
};

export const OUTCOME_LABELS: Record<string, string> = {
  booked: "Booked",
  rescheduled: "Rescheduled",
  cancelled: "Cancelled",
  answered: "Answered",
  message_taken: "Message taken",
  transferred: "Transferred",
  abandoned: "Abandoned",
  failed: "Failed",
};
