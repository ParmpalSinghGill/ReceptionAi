import { evaluatePredicate } from "./predicate";
import type { FieldError, FormValues, RenderDocument, RenderField } from "./types";

/**
 * Client-side validation that mirrors the server's rules.
 *
 * 02 §7.4 rule 1: "The client is a convenience, the server is the authority."
 * Everything here is re-run on submit. Its job is LATENCY — telling the user
 * about a bad phone number while their cursor is still in the field — not
 * correctness.
 *
 * Rules with `server_only` are NEVER evaluated. The compiler marks a rule
 * server_only when it needs a network call (email_deliverable,
 * no_duplicate_active_booking). Guessing at them produces a form that says
 * "valid" and then fails on submit, which is worse than saying nothing.
 */
export function validate(
  doc: RenderDocument,
  values: FormValues,
  visibility: Record<string, boolean>,
): Record<string, FieldError[]> {
  const errors: Record<string, FieldError[]> = {};
  const push = (key: string, err: FieldError) => {
    (errors[key] ??= []).push(err);
  };

  const requiredByRule = new Set<string>();
  for (const rule of doc.rules) {
    if (rule.server_only || rule.kind !== "required_when") continue;
    // onUnknown: false — an unevaluable requirement is NOT enforced here; the
    // server will say so if it matters.
    if (evaluatePredicate(rule.predicate, values, { onUnknown: false })) {
      requiredByRule.add(rule.target_field);
    }
  }

  for (const field of doc.fields) {
    // A hidden field is not validated and its value is not submitted.
    // Validating an invisible field yields an error the user cannot see, on a
    // control they cannot reach — a permanently unsubmittable form.
    if (visibility[field.key] === false) continue;

    const value = values[field.key];
    const isRequired = field.required || requiredByRule.has(field.key);

    if (isRequired && isBlank(value)) {
      push(field.key, { message: `${field.label} is required.`, severity: "error" });
      continue; // Do not pile format errors on an empty required field.
    }
    if (isBlank(value)) continue;

    checkConstraints(field, value, push);
  }

  for (const rule of doc.rules) {
    if (rule.server_only || rule.kind !== "validate") continue;
    if (visibility[rule.target_field] === false) continue;
    // A `validate` predicate asserts what must be TRUE; failing it is an error.
    // onUnknown: true — an unevaluable assertion passes rather than blocking.
    if (!evaluatePredicate(rule.predicate, values, { onUnknown: true })) {
      push(rule.target_field, {
        message: rule.message ?? "This value is not valid.",
        severity: rule.severity ?? "error",
        ruleId: rule.id,
      });
    }
  }

  return errors;
}

function checkConstraints(
  field: RenderField,
  value: unknown,
  push: (key: string, err: FieldError) => void,
): void {
  const c = field.constraints;
  const err = (message: string) => push(field.key, { message, severity: "error" });

  if (typeof value === "string") {
    if (c.min_length !== undefined && value.length < c.min_length) {
      err(`${field.label} must be at least ${c.min_length} characters.`);
    }
    if (c.max_length !== undefined && value.length > c.max_length) {
      err(`${field.label} must be at most ${c.max_length} characters.`);
    }
    if (c.pattern) {
      try {
        if (!new RegExp(c.pattern).test(value)) {
          err(`${field.label} is not in the expected format.`);
        }
      } catch {
        /* invalid pattern: the server validated it; do not block the user */
      }
    }
    if (field.input_type === "email" && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) {
      // Deliberately permissive. The RFC 5322 grammar is not usefully
      // expressible in a regex, and every attempt rejects addresses that work.
      // "Has an @ and a dot after it" catches typos; the server's
      // email_deliverable check (server_only) is the real answer.
      err("Enter a valid email address.");
    }
    if (field.input_type === "tel" && !/^\+?[0-9\s().-]{6,20}$/.test(value)) {
      err("Enter a valid phone number.");
    }
  }

  if (field.input_type === "number") {
    const n = typeof value === "number" ? value : Number(value);
    if (Number.isNaN(n)) {
      err(`${field.label} must be a number.`);
    } else {
      if (c.minimum !== undefined && n < c.minimum) {
        err(`${field.label} must be at least ${c.minimum}.`);
      }
      if (c.maximum !== undefined && n > c.maximum) {
        err(`${field.label} must be at most ${c.maximum}.`);
      }
    }
  }

  if (c.min_date || c.max_date) {
    // These arrived ABSOLUTE. 02 §7.2 resolves relative tokens ("today-P18Y")
    // server-side against the tenant's location timezone, precisely so a
    // browser in Auckland and a server in Virginia agree on what "today" is.
    // Re-deriving them here would reintroduce the bug the server fixed.
    const t = Date.parse(String(value));
    if (!Number.isNaN(t)) {
      if (c.min_date && t < Date.parse(c.min_date)) {
        err(`${field.label} must be on or after ${humanDate(c.min_date)}.`);
      }
      if (c.max_date && t > Date.parse(c.max_date)) {
        err(`${field.label} must be on or before ${humanDate(c.max_date)}.`);
      }
    }
  }

  if (typeof File !== "undefined" && value instanceof File) {
    if (c.max_file_size_bytes && value.size > c.max_file_size_bytes) {
      err(`File must be smaller than ${humanBytes(c.max_file_size_bytes)}.`);
    }
    if (c.accept?.length && !c.accept.some((a) => matchesAccept(value, a))) {
      err(`File must be one of: ${c.accept.join(", ")}.`);
    }
  }
}

function isBlank(v: unknown): boolean {
  return v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0);
}

function matchesAccept(file: File, accept: string): boolean {
  if (accept.startsWith(".")) return file.name.toLowerCase().endsWith(accept.toLowerCase());
  if (accept.endsWith("/*")) return file.type.startsWith(accept.slice(0, -1));
  return file.type === accept;
}

function humanDate(iso: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(new Date(iso));
}

function humanBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * 02 §7.4 rule 4: `sensitive: true` fields are excluded from client-side
 * analytics, error reports and autosaved drafts.
 *
 * The store honours it STRUCTURALLY — dropping the keys entirely rather than
 * masking them — so there is nothing to leak even if a lint rule is missed. A
 * drafted card number or diagnosis in localStorage survives logout and is
 * readable by any XSS on the origin.
 */
export function draftPayload(doc: RenderDocument, values: FormValues): FormValues {
  const sensitive = new Set(doc.fields.filter((f) => f.sensitive).map((f) => f.key));
  return Object.fromEntries(Object.entries(values).filter(([k]) => !sensitive.has(k)));
}
