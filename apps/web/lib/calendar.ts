/**
 * Day-view arithmetic for the diary.
 *
 * The rule this module exists to enforce: **a business day is a `YYYY-MM-DD`
 * string in the tenant's timezone, never a `Date`.** A `Date` is an instant,
 * and turning one into "which day is this" needs a timezone the caller usually
 * forgets to pass. Every function here takes and returns the string form, so
 * the ambiguous conversion happens in exactly two places — `todayInTimezone`
 * and `slotInstant` — and both take the timezone explicitly.
 */

/**
 * The resource types the diary treats as "who you are seeing".
 *
 * This is the ONE place a vertical word is written down, and it lives in
 * `apps/web` deliberately — `packages/booking` and `packages/ai` may not name
 * a vertical at all (invariant I4), and the codes below come from a tenant's
 * manifest rather than from the engine. `human` is the generic type code; a
 * dental manifest happens to call its people `clinician`.
 *
 * Every consumer falls back to "all resources" when nothing matches, so a
 * tenant whose manifest uses neither code still gets a working filter rather
 * than an empty one.
 */
const PERSON_TYPE_CODES: ReadonlySet<string> = new Set(["clinician", "human"]);

/** The people among a booking's resources, or all of them if none are people. */
export function peopleAmong<T extends { resource_type_code: string | null }>(
  resources: readonly T[],
): readonly T[] {
  const people = resources.filter(
    (r) => r.resource_type_code !== null && PERSON_TYPE_CODES.has(r.resource_type_code),
  );
  return people.length > 0 ? people : resources;
}

/** The first slot rendered. 9:00am, per the practice's opening hours. */
export const DAY_START_MINUTES = 9 * 60;
/** The last slot ENDS here: 6:00pm, so the final row is 17:30–18:00. */
export const DAY_END_MINUTES = 18 * 60;
/** Half-hour rows. */
export const SLOT_MINUTES = 30;

export interface Slot {
  /** Minutes from midnight, tenant-local. */
  minutes: number;
  /** "09:30" — the row's label and its stable React key. */
  label: string;
}

/** Every half-hour row between 9:00am and 6:00pm. */
export function daySlots(): Slot[] {
  const slots: Slot[] = [];
  for (let m = DAY_START_MINUTES; m < DAY_END_MINUTES; m += SLOT_MINUTES) {
    slots.push({ minutes: m, label: minutesToLabel(m) });
  }
  return slots;
}

export function minutesToLabel(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/**
 * Which minute-of-day an instant falls on, in the tenant's zone.
 *
 * Uses `Intl` rather than `getHours()` because `getHours()` answers in the
 * BROWSER's zone. A receptionist in London and a practice owner checking the
 * diary from Spain must see the same grid, and the tenant's zone is the one
 * that is authoritative.
 */
export function localMinutesOfDay(iso: string, timezone: string): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: timezone,
  }).formatToParts(new Date(iso));

  const hour = Number(parts.find((p) => p.type === "hour")?.value ?? "0");
  const minute = Number(parts.find((p) => p.type === "minute")?.value ?? "0");
  // Intl renders midnight as "24" in some locales/engines; normalise so a
  // 00:15 appointment does not sort to the end of the day.
  return (hour % 24) * 60 + minute;
}

/**
 * The slot a booking belongs to: its start, floored to the half hour.
 *
 * Flooring rather than spanning is deliberate. An appointment appears once, in
 * the row it STARTS in, the way a paper diary is written. Painting a 60-minute
 * appointment across two rows makes the 10:30 row claim a booking that nobody
 * arrives for at 10:30, and the receptionist reading down the column counts it
 * twice.
 */
export function slotMinutesFor(iso: string, timezone: string): number {
  const minutes = localMinutesOfDay(iso, timezone);
  return Math.floor(minutes / SLOT_MINUTES) * SLOT_MINUTES;
}

/** Today, as the tenant's calendar reckons it. */
export function todayInTimezone(timezone: string, now: Date = new Date()): string {
  // `en-CA` formats as YYYY-MM-DD, which is the shape the API wants, without
  // hand-assembling parts.
  return new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: timezone,
  }).format(now);
}

/**
 * The day `delta` days from `date`.
 *
 * Arithmetic on the calendar date at NOON UTC, not midnight: adding 24h to a
 * midnight instant lands on the previous day in any zone west of UTC, and on
 * the same day twice across a DST fall-back. Noon has twelve hours of slack in
 * both directions, which no real offset shift comes close to.
 */
export function shiftDay(date: string, delta: number): string {
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}

/** "Thursday, 13 August 2026" — the heading a person reads. */
export function formatDayHeading(date: string, locale = "en-GB"): string {
  return new Intl.DateTimeFormat(locale, {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${date}T12:00:00Z`));
}

/**
 * A compact chip label: "T. Ellis · Check-up".
 *
 * The initial-plus-surname form is what fits in a half-hour row at four
 * columns wide, and it is also the form that does not put a full patient name
 * on a screen visible from the waiting room. A name with one word ("Madonna",
 * or a business) is left whole rather than being reduced to a bare initial.
 */
export function chipLabel(
  customerName: string | null,
  serviceName: string,
): string {
  const person = shortName(customerName);
  const service = shortService(serviceName);
  return person ? `${person} · ${service}` : service;
}

export function shortName(customerName: string | null): string {
  if (!customerName) return "";
  const parts = customerName.trim().split(/\s+/);
  if (parts.length === 1) return parts[0]!;
  const last = parts[parts.length - 1]!;
  return `${parts[0]![0]!.toUpperCase()}. ${last}`;
}

/** Services are named for a booking form, not for a 90px chip. */
export function shortService(serviceName: string): string {
  const trimmed = serviceName
    .replace(/^routine\s+/i, "")
    .replace(/\s*[—–-]\s*\d+\s*min$/i, "")
    .replace(/\s+appointment$/i, "")
    .replace(/\s+examination$/i, " exam")
    .replace(/\s+assessment$/i, "")
    .replace(/\s+visit$/i, "")
    .trim();
  const cased = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return cased.length > 22 ? `${cased.slice(0, 21)}…` : cased;
}
