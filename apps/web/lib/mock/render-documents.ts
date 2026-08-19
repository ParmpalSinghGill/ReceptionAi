import type { RenderDocument } from "@/lib/forms/types";

/**
 * Compiled RenderDocuments, as `GET /api/v1/forms/{key}` would return them.
 *
 * These are the PROOF of the claim in 06 §9.9: the medical and restaurant
 * documents below come from different verticals with different fields,
 * different widgets and different conditional logic, and they render through
 * the SAME `DynamicForm` with zero component changes. Adding the restaurant
 * vertical touches a manifest YAML file and a connector row — and zero files
 * under apps/web.
 *
 * The third document deliberately contains a widget this build does not know
 * about, to exercise the degradation path rather than leaving it untested.
 */

export const MEDICAL_INTAKE: RenderDocument = {
  render_version: "1",
  form_key: "patient_intake",
  title: "New patient details",
  description:
    "Collected by the assistant when someone books who is not already registered. The same form is used at reception.",
  purpose: "intake",
  submit_label: "Register patient",
  meta: {
    manifest_version: "medical@2.4.1",
    vertical_key: "medical",
    resolved_hash: "sha256:9f2c41ab7e",
    theme: { density: "comfortable" },
  },
  sections: [
    { key: "who", title: "About you", collapsible: false, field_keys: ["full_name", "date_of_birth", "phone", "email"] },
    {
      key: "clinical",
      title: "Medical information",
      description: "Held under your practice's clinical record retention policy.",
      collapsible: false,
      field_keys: ["registered_gp", "has_conditions", "conditions_detail", "medications", "consent_share"],
    },
    { key: "visit", title: "This appointment", collapsible: false, field_keys: ["reason", "pain_level", "preferred_clinician", "accessibility_needs"] },
  ],
  fields: [
    {
      key: "full_name", widget: "input", input_type: "text", label: "Full name",
      required: true, disabled: false, default_value: "", placeholder: "As it appears on your NHS record",
      autocomplete: "name", layout: { col_span: 6, order: 1 },
      constraints: { min_length: 2, max_length: 120 }, pii: "identity", sensitive: false,
    },
    {
      key: "date_of_birth", widget: "date_picker", input_type: "date", label: "Date of birth",
      required: true, disabled: false, default_value: "", autocomplete: "bday",
      layout: { col_span: 6, order: 2 },
      // ABSOLUTE, resolved server-side against Europe/London (02 §7.2).
      constraints: { min_date: "1906-01-01", max_date: "2026-08-05" },
      pii: "identity", sensitive: false,
      help: "We use this to match you to an existing record.",
    },
    {
      key: "phone", widget: "input", input_type: "tel", label: "Mobile number",
      required: true, disabled: false, default_value: "", placeholder: "07700 900123",
      autocomplete: "tel", layout: { col_span: 6, order: 3 },
      constraints: {}, pii: "contact", sensitive: false,
      help: "We send appointment reminders here.",
    },
    {
      key: "email", widget: "input", input_type: "email", label: "Email address",
      required: false, disabled: false, default_value: "", autocomplete: "email",
      layout: { col_span: 6, order: 4 }, constraints: {}, pii: "contact", sensitive: false,
    },
    {
      key: "registered_gp", widget: "input", input_type: "text", label: "Your GP surgery",
      required: false, disabled: false, default_value: "", layout: { col_span: 6, order: 5 },
      constraints: { max_length: 160 }, pii: "health", sensitive: false,
    },
    {
      key: "has_conditions", widget: "switch", input_type: "checkbox",
      label: "Do you have any medical conditions we should know about?",
      required: false, disabled: false, default_value: false,
      layout: { col_span: 12, order: 6 }, constraints: {}, pii: "health", sensitive: false,
    },
    {
      key: "conditions_detail", widget: "textarea", input_type: "text",
      label: "Please tell us about them",
      help: "Heart conditions, diabetes, blood thinners, allergies to anaesthetic, and anything else your dentist should know.",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 12, order: 7 }, constraints: { max_length: 2000 },
      // Marked sensitive: excluded from analytics, error reports and drafts.
      pii: "health", sensitive: true,
    },
    {
      key: "medications", widget: "textarea", input_type: "text", label: "Current medications",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 12, order: 8 }, constraints: { max_length: 1000 },
      pii: "health", sensitive: true,
    },
    {
      key: "consent_share", widget: "checkbox", input_type: "checkbox",
      label: "Sharing with your GP",
      placeholder: "I agree that Northgate Dental may share relevant clinical information with my GP surgery.",
      required: false, disabled: false, default_value: false,
      layout: { col_span: 12, order: 9 }, constraints: {}, pii: "none", sensitive: false,
    },
    {
      key: "reason", widget: "combobox", input_type: "custom", label: "Reason for your visit",
      required: true, disabled: false, default_value: "",
      layout: { col_span: 6, order: 10 }, constraints: {}, pii: "health", sensitive: false,
      options: [
        { value: "check_up", label: "Routine check-up" },
        { value: "pain", label: "Pain or discomfort", description: "We will offer an emergency slot" },
        { value: "cosmetic", label: "Cosmetic treatment" },
        { value: "hygienist", label: "Hygienist appointment" },
        { value: "second_opinion", label: "Second opinion" },
      ],
    },
    {
      key: "pain_level", widget: "slider", input_type: "number",
      label: "How bad is the pain, from 0 to 10?",
      help: "0 is no pain at all, 10 is the worst pain you can imagine.",
      required: false, disabled: false, default_value: 0,
      layout: { col_span: 6, order: 11 }, constraints: { minimum: 0, maximum: 10, step: 1 },
      pii: "health", sensitive: false,
    },
    {
      key: "preferred_clinician", widget: "radio", input_type: "custom", label: "Preferred clinician",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 6, order: 12 }, constraints: {}, pii: "none", sensitive: false,
      options: [
        { value: "any", label: "No preference", description: "Usually the soonest appointment" },
        { value: "osei", label: "Dr Adaeze Osei" },
        { value: "hartley", label: "Dr Simon Hartley", description: "Tuesdays and Thursdays only" },
      ],
    },
    {
      key: "accessibility_needs", widget: "textarea", input_type: "text",
      label: "Access or communication needs",
      help: "Step-free access, a hearing loop, extra time, or anything else that would help.",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 6, order: 13 }, constraints: { max_length: 500 },
      pii: "health", sensitive: false,
    },
  ],
  rules: [
    {
      id: "r1", kind: "visible_when", target_field: "conditions_detail",
      predicate: { field: "has_conditions", op: "eq", value: true }, server_only: false,
    },
    {
      id: "r2", kind: "visible_when", target_field: "medications",
      predicate: { field: "has_conditions", op: "eq", value: true }, server_only: false,
    },
    {
      id: "r3", kind: "required_when", target_field: "conditions_detail",
      predicate: { field: "has_conditions", op: "eq", value: true }, server_only: false,
    },
    {
      id: "r4", kind: "visible_when", target_field: "pain_level",
      predicate: { field: "reason", op: "eq", value: "pain" }, server_only: false,
    },
    {
      id: "r5", kind: "required_when", target_field: "email",
      predicate: { field: "consent_share", op: "eq", value: true }, server_only: false,
    },
    {
      // A server_only rule: it needs a network call, so the client never
      // evaluates it and never claims the form is valid on its account.
      id: "r6", kind: "validate", target_field: "email",
      predicate: { checks: [{ check: "email_deliverable" }] },
      message: "We could not deliver to that address.", server_only: true,
    },
  ],
};

export const RESTAURANT_BOOKING: RenderDocument = {
  render_version: "1",
  form_key: "table_booking",
  title: "Table booking",
  description:
    "A different vertical entirely — different fields, different widgets, different logic. Rendered by the same component, with no code change. That is the whole claim.",
  purpose: "intake",
  submit_label: "Book the table",
  meta: {
    manifest_version: "restaurant@1.0.0",
    vertical_key: "restaurant",
    resolved_hash: "sha256:41be09cc22",
    theme: { density: "comfortable" },
  },
  sections: [
    { key: "booking", title: "Your table", collapsible: false, field_keys: ["party_size", "date", "time", "seating"] },
    { key: "details", title: "Anything we should know?", collapsible: false, field_keys: ["high_chairs", "dietary", "occasion", "name", "phone"] },
  ],
  fields: [
    {
      key: "party_size", widget: "slider", input_type: "number", label: "How many people?",
      required: true, disabled: false, default_value: 2,
      layout: { col_span: 6, order: 1 }, constraints: { minimum: 1, maximum: 12, step: 1 },
      pii: "none", sensitive: false,
    },
    {
      key: "date", widget: "date_picker", input_type: "date", label: "Date",
      required: true, disabled: false, default_value: "",
      layout: { col_span: 3, order: 2 },
      constraints: { min_date: "2026-08-05", max_date: "2026-11-05" },
      pii: "none", sensitive: false,
    },
    {
      key: "time", widget: "time_picker", input_type: "time", label: "Time",
      required: true, disabled: false, default_value: "",
      layout: { col_span: 3, order: 3 }, constraints: {}, pii: "none", sensitive: false,
    },
    {
      key: "seating", widget: "combobox", input_type: "custom", label: "Seating preference",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 6, order: 4 }, constraints: {}, pii: "none", sensitive: false,
      options: [
        { value: "any", label: "No preference" },
        { value: "window", label: "By the window" },
        { value: "booth", label: "Booth", description: "Parties of 4 or more" },
        { value: "outside", label: "Terrace", description: "Heated, covered" },
        { value: "bar", label: "At the bar" },
      ],
    },
    {
      key: "high_chairs", widget: "switch", input_type: "checkbox", label: "Do you need a high chair?",
      required: false, disabled: false, default_value: false,
      layout: { col_span: 6, order: 5 }, constraints: {}, pii: "none", sensitive: false,
    },
    {
      key: "dietary", widget: "textarea", input_type: "text", label: "Allergies or dietary requirements",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 12, order: 6 }, constraints: { max_length: 500 },
      pii: "health", sensitive: false,
      help: "The kitchen sees this before you arrive.",
    },
    {
      key: "occasion", widget: "radio", input_type: "custom", label: "Special occasion?",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 6, order: 7 }, constraints: {}, pii: "none", sensitive: false,
      options: [
        { value: "none", label: "Just dinner" },
        { value: "birthday", label: "Birthday" },
        { value: "anniversary", label: "Anniversary" },
        { value: "business", label: "Business dinner" },
      ],
    },
    {
      key: "name", widget: "input", input_type: "text", label: "Name for the booking",
      required: true, disabled: false, default_value: "", autocomplete: "name",
      layout: { col_span: 6, order: 8 }, constraints: { max_length: 80 },
      pii: "identity", sensitive: false,
    },
    {
      key: "phone", widget: "input", input_type: "tel", label: "Contact number",
      required: true, disabled: false, default_value: "", autocomplete: "tel",
      layout: { col_span: 6, order: 9 }, constraints: {}, pii: "contact", sensitive: false,
    },
  ],
  rules: [
    {
      // A conditional driven by a NUMERIC comparison against a slider — the
      // looseEq/compare path in the predicate evaluator.
      id: "r1", kind: "visible_when", target_field: "high_chairs",
      predicate: { field: "party_size", op: "gte", value: 2 }, server_only: false,
    },
    {
      id: "r2", kind: "visible_when", target_field: "occasion",
      predicate: { field: "party_size", op: "gte", value: 4 }, server_only: false,
    },
  ],
};

/**
 * A document containing a widget this build does not implement, so the
 * degradation path is exercised on a real screen rather than only in a test.
 * `nps_wheel` is not in WidgetName; the renderer falls back to a labelled
 * text control and says so, honestly.
 */
export const FUTURE_FEEDBACK: RenderDocument = {
  render_version: "1",
  form_key: "post_visit_feedback",
  title: "After your visit",
  description:
    "This form was compiled by a NEWER manifest schema than this portal build knows. One field uses a widget that does not exist here yet.",
  purpose: "feedback",
  submit_label: "Send feedback",
  meta: {
    manifest_version: "medical@2.9.0",
    vertical_key: "medical",
    resolved_hash: "sha256:77aa10df93",
    theme: { density: "comfortable" },
  },
  sections: [],
  fields: [
    {
      key: "rating", widget: "rating", input_type: "number", label: "How was your visit?",
      required: true, disabled: false, default_value: 0,
      layout: { col_span: 12, order: 1 }, constraints: { minimum: 0, maximum: 5 },
      pii: "none", sensitive: false,
    },
    {
      // Not in WIDGETS. Must degrade, never crash, never silently drop.
      key: "nps", widget: "nps_wheel" as never, input_type: "number",
      label: "How likely are you to recommend us, from 0 to 10?",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 12, order: 2 }, constraints: { minimum: 0, maximum: 10 },
      pii: "none", sensitive: false,
    },
    {
      key: "comments", widget: "textarea", input_type: "text", label: "Anything else?",
      required: false, disabled: false, default_value: "",
      layout: { col_span: 12, order: 3 }, constraints: { max_length: 1000 },
      pii: "none", sensitive: false,
    },
  ],
  rules: [
    {
      // An operator from a newer schema this build predates. It must not throw;
      // for a visible_when it resolves to VISIBLE.
      id: "r1", kind: "visible_when", target_field: "comments",
      predicate: { field: "rating", op: "resembles" as never, value: 3 }, server_only: false,
    },
  ],
};

export const RENDER_DOCUMENTS: Record<string, RenderDocument> = {
  patient_intake: MEDICAL_INTAKE,
  table_booking: RESTAURANT_BOOKING,
  post_visit_feedback: FUTURE_FEEDBACK,
};
