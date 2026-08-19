/**
 * The RenderDocument contract (06 §9.1).
 *
 * GENERATED from the Pydantic response model in the real build — the same
 * codegen path as every other API type. 02 §7 compiles manifest + tenant
 * overlay + live option sources into this; THE PORTAL NEVER SEES A MANIFEST.
 * That is the whole point: the frontend knows about widgets and predicates,
 * not about verticals.
 */
export interface RenderDocument {
  render_version: "1";
  form_key: string;
  title: string;
  description?: string;
  purpose: "intake" | "admin" | "confirmation" | "feedback";
  submit_label: string;
  sections: RenderSection[];
  fields: RenderField[];
  rules: RenderRule[];
  meta: {
    manifest_version: string;
    vertical_key: string;
    resolved_hash: string;
    theme: { accent?: string; density: "comfortable" | "compact" };
  };
}

export interface RenderSection {
  key: string;
  title: string;
  description?: string;
  collapsible: boolean;
  field_keys: string[];
}

/**
 * The CLOSED widget set from 02 §7.1.
 *
 * Closed is the point: new manifest FIELD TYPES map onto existing widgets by
 * default, so adding "nhs_number" as a field type needs a backend line, not a
 * frontend release. A frontend change is required only for a genuinely new
 * INTERACTION, which is rare and deserves review.
 */
export type WidgetName =
  | "input"
  | "textarea"
  | "radio"
  | "checkbox"
  | "switch"
  | "combobox"
  | "date_picker"
  | "time_picker"
  | "slider"
  | "rating"
  | "file_drop"
  | "signature_pad"
  | "address";

export interface RenderFieldOption {
  value: string;
  label: string;
  description?: string;
}

export interface RenderField {
  key: string;
  widget: WidgetName;
  input_type:
    | "text"
    | "email"
    | "tel"
    | "number"
    | "date"
    | "time"
    | "datetime-local"
    | "checkbox"
    | "file"
    | "custom";
  label: string;
  help?: string;
  placeholder?: string;
  required: boolean;
  disabled: boolean;
  default_value: unknown;
  options?: RenderFieldOption[];
  options_endpoint?: string;
  layout: { col_span: number; order: number };
  mask?: string;
  autocomplete?: string;
  constraints: {
    min_length?: number;
    max_length?: number;
    pattern?: string;
    minimum?: number;
    maximum?: number;
    step?: number;
    /** ABSOLUTE ISO — resolved server-side (02 §7.2) against the tenant's
     *  timezone, precisely so a browser in Auckland and a server in Virginia
     *  agree on what "today" is. Never re-derive these client-side. */
    min_date?: string;
    max_date?: string;
    accept?: string[];
    max_file_size_bytes?: number;
  };
  pii: "none" | "contact" | "identity" | "financial" | "health" | "government_id";
  sensitive: boolean;
}

export interface RenderRule {
  id: string;
  kind: "visible_when" | "required_when" | "validate";
  target_field: string;
  predicate: SerializedPredicate;
  message?: string;
  severity?: "error" | "warning" | "info";
  /** Marked when the rule needs a network call (email_deliverable,
   *  no_duplicate_active_booking). NEVER evaluated client-side. */
  server_only: boolean;
}

/** 02 §1.9's closed vocabulary, verbatim. There is deliberately no `expr` —
 *  an arbitrary expression language in a manifest is a sandbox escape. */
export type PredicateOp =
  | "eq" | "ne" | "gt" | "gte" | "lt" | "lte" | "in" | "not_in"
  | "contains" | "starts_with" | "matches" | "is_empty" | "is_not_empty"
  | "before" | "after" | "within" | "older_than" | "younger_than";

export type SerializedPredicate =
  | { all: SerializedPredicate[] }
  | { any: SerializedPredicate[] }
  | { not: SerializedPredicate }
  | { field: string; op: PredicateOp; value?: unknown }
  | { checks: Array<{ check: string; [k: string]: unknown }> };

export type FormValues = Record<string, unknown>;

export interface FieldError {
  message: string;
  severity: "error" | "warning" | "info";
  ruleId?: string;
}
