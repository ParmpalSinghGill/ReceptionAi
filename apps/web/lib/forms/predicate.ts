import type { FormValues, PredicateOp, SerializedPredicate } from "./types";

/**
 * Evaluate a manifest predicate against current form values.
 *
 * This mirrors `packages/verticals/predicate.py`. The two implementations MUST
 * agree, and a shared fixture file keeps them agreeing — a Python test and a
 * Vitest test over the same table of (predicate, values, expected) triples.
 * Divergence between them is the single most likely defect in the entire form
 * system, because it presents as "the field was hidden but the server says it
 * is required", with nothing in either codebase looking wrong on its own.
 *
 * `onUnknown` is the answer for anything unevaluable, and the CALLER chooses
 * it per rule kind — see the note at the bottom of this file.
 */
export function evaluatePredicate(
  predicate: SerializedPredicate | undefined,
  values: FormValues,
  opts: { onUnknown: boolean },
): boolean {
  if (!predicate) return true;

  if ("all" in predicate) {
    return predicate.all.every((p) => evaluatePredicate(p, values, opts));
  }
  if ("any" in predicate) {
    return predicate.any.some((p) => evaluatePredicate(p, values, opts));
  }
  if ("not" in predicate) {
    return !evaluatePredicate(predicate.not, values, opts);
  }
  if ("checks" in predicate) {
    // Named server-side checks (email_deliverable, no_duplicate_active_booking).
    // The compiler marks these server_only, so they should never reach here;
    // if one does, it is a compiler bug and we must not guess an answer.
    return opts.onUnknown;
  }
  if ("field" in predicate) {
    return evaluateLeaf(predicate.field, predicate.op, predicate.value, values, opts);
  }
  return opts.onUnknown;
}

function evaluateLeaf(
  field: string,
  op: PredicateOp,
  expected: unknown,
  values: FormValues,
  opts: { onUnknown: boolean },
): boolean {
  const actual = resolvePath(field, values);

  switch (op) {
    case "is_empty":
      return isEmpty(actual);
    case "is_not_empty":
      return !isEmpty(actual);
    case "eq":
      return looseEq(actual, expected);
    case "ne":
      return !looseEq(actual, expected);
    case "gt":
      return compare(actual, expected) > 0;
    case "gte":
      return compare(actual, expected) >= 0;
    case "lt":
      return compare(actual, expected) < 0;
    case "lte":
      return compare(actual, expected) <= 0;
    case "in":
      return Array.isArray(expected) && expected.some((e) => looseEq(actual, e));
    case "not_in":
      return Array.isArray(expected) && !expected.some((e) => looseEq(actual, e));
    case "contains":
      if (Array.isArray(actual)) return actual.some((a) => looseEq(a, expected));
      return String(actual ?? "").includes(String(expected));
    case "starts_with":
      return String(actual ?? "").startsWith(String(expected));
    case "matches":
      // The manifest validator already rejected nested unbounded quantifiers
      // (02 §1.9's ReDoS guard), so compiling here is safe. A malformed regex
      // that slipped through must not take down the form.
      try {
        return new RegExp(String(expected)).test(String(actual ?? ""));
      } catch {
        return opts.onUnknown;
      }
    case "before": {
      const a = toTime(actual);
      const b = toTime(expected);
      return a !== null && b !== null ? a < b : opts.onUnknown;
    }
    case "after": {
      const a = toTime(actual);
      const b = toTime(expected);
      return a !== null && b !== null ? a > b : opts.onUnknown;
    }
    case "within": {
      // value is an ISO 8601 duration, e.g. "P30D": actual is within
      // [now - d, now + d].
      const ms = parseDuration(String(expected));
      const t = toTime(actual);
      if (ms === null || t === null) return opts.onUnknown;
      return Math.abs(t - Date.now()) <= ms;
    }
    case "older_than": {
      const ms = parseDuration(String(expected));
      const t = toTime(actual);
      if (ms === null || t === null) return opts.onUnknown;
      return Date.now() - t > ms;
    }
    case "younger_than": {
      const ms = parseDuration(String(expected));
      const t = toTime(actual);
      if (ms === null || t === null) return opts.onUnknown;
      return Date.now() - t < ms;
    }
    default: {
      // A manifest schema MINOR added an operator this build predates. Never
      // throw: one unknown operator must not white-screen a whole intake form.
      const _exhaustive: never = op;
      void _exhaustive;
      return opts.onUnknown;
    }
  }
}

function resolvePath(path: string, values: FormValues): unknown {
  return path
    .split(".")
    .reduce<unknown>(
      (acc, part) =>
        acc && typeof acc === "object" ? (acc as Record<string, unknown>)[part] : undefined,
      values,
    );
}

function isEmpty(v: unknown): boolean {
  if (v === null || v === undefined) return true;
  if (typeof v === "string") return v.trim() === "";
  if (Array.isArray(v)) return v.length === 0;
  return false;
}

/**
 * Loose equality across the string/number boundary ONLY.
 *
 * A select's value is always a string ("2"), while a manifest predicate may
 * compare it against a number (2) because YAML parsed it as one. Refusing to
 * bridge that would make `{field: party_size, op: gte, value: 5}` silently
 * false forever — a conditional field that never appears, with nothing in the
 * console. JS's `==` would also equate `"" == 0` and `null == undefined`,
 * which is why this is explicit rather than `==`.
 */
function looseEq(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a === null || a === undefined || b === null || b === undefined) return false;
  if (typeof a === "number" && typeof b === "string") return a === Number(b);
  if (typeof a === "string" && typeof b === "number") return Number(a) === b;
  if (typeof a === "boolean" && typeof b === "string") return String(a) === b;
  if (typeof a === "string" && typeof b === "boolean") return a === String(b);
  return false;
}

function compare(a: unknown, b: unknown): number {
  const ta = toTime(a);
  const tb = toTime(b);
  if (ta !== null && tb !== null) return ta - tb;
  const na = Number(a);
  const nb = Number(b);
  if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
  return String(a ?? "").localeCompare(String(b ?? ""));
}

function toTime(v: unknown): number | null {
  if (v instanceof Date) return v.getTime();
  if (typeof v !== "string") return null;
  // A bare number in a string ("5") parses as a year in some engines; require
  // something date-shaped before trusting Date.parse.
  if (!/[-:T]/.test(v)) return null;
  const t = Date.parse(v);
  return Number.isNaN(t) ? null : t;
}

/**
 * ISO 8601 duration → milliseconds. Years and months use calendar averages,
 * which is correct for "older_than P18Y" within a day. Exactness there is not
 * achievable client-side anyway: the server resolved the authoritative
 * boundary into constraints.min_date/max_date against the tenant timezone.
 */
const DURATION =
  /^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/;

export function parseDuration(iso: string): number | null {
  const m = DURATION.exec(iso);
  if (!m) return null;
  const [, y, mo, w, d, h, mi, s] = m.map((x) => (x ? Number(x) : 0));
  return (
    y! * 31_557_600_000 +
    mo! * 2_629_800_000 +
    w! * 604_800_000 +
    d! * 86_400_000 +
    h! * 3_600_000 +
    mi! * 60_000 +
    s! * 1_000
  );
}

/**
 * WHY THE UNKNOWN DIRECTION DIFFERS BY RULE KIND (06 §9.2).
 *
 * `visible_when` with an unevaluable predicate resolves to VISIBLE: showing a
 * field the user did not need is a small annoyance, while hiding a field the
 * server requires produces an unsubmittable form with no visible cause.
 *
 * `required_when` resolves to NOT REQUIRED: the server re-evaluates every rule
 * on submit (02 §7.4 rule 1), so a missed client-side requirement surfaces as
 * a server error attached to the right field, whereas a false requirement
 * blocks a legitimate submit with an error the user cannot clear.
 */
