"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { evaluatePredicate } from "@/lib/forms/predicate";
import { validate } from "@/lib/forms/validation";
import type {
  FieldError,
  FormValues,
  RenderDocument,
  RenderField,
  RenderSection,
  WidgetName,
} from "@/lib/forms/types";

import { ErrorSummary } from "./ErrorSummary";
import { FieldShell } from "./FieldShell";
import {
  AddressWidget,
  CheckboxWidget,
  ComboboxWidget,
  DatePickerWidget,
  FileDropWidget,
  RadioWidget,
  RatingWidget,
  SignatureWidget,
  SliderWidget,
  SwitchWidget,
  TextWidget,
  TextareaWidget,
  TimePickerWidget,
  UnsupportedWidget,
  type WidgetProps,
} from "./widgets";

const WIDGETS: Record<WidgetName, React.FC<WidgetProps>> = {
  input: TextWidget,
  textarea: TextareaWidget,
  radio: RadioWidget,
  checkbox: CheckboxWidget,
  switch: SwitchWidget,
  combobox: ComboboxWidget,
  date_picker: DatePickerWidget,
  time_picker: TimePickerWidget,
  slider: SliderWidget,
  rating: RatingWidget,
  file_drop: FileDropWidget,
  signature_pad: SignatureWidget,
  address: AddressWidget,
};

export interface SubmitResult {
  ok: boolean;
  fieldErrors?: Record<string, string[]>;
  newDocument?: RenderDocument;
}

export function DynamicForm({
  doc,
  initialValues,
  onSubmit,
  submitting = false,
  readOnly = false,
}: {
  doc: RenderDocument;
  initialValues?: FormValues;
  onSubmit: (values: FormValues, resolvedHash: string) => Promise<SubmitResult>;
  submitting?: boolean;
  readOnly?: boolean;
}) {
  const [values, setValues] = useState<FormValues>(() => seed(doc, initialValues));
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [serverErrors, setServerErrors] = useState<Record<string, string[]>>({});
  const [attempted, setAttempted] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const formId = useId();
  const summaryRef = useRef<HTMLDivElement>(null);
  const fieldRefs = useRef(new Map<string, HTMLElement>());

  const visibility = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const field of doc.fields) map[field.key] = true;
    for (const rule of doc.rules) {
      if (rule.kind !== "visible_when" || rule.server_only) continue;
      // Unknown predicate → VISIBLE. Showing a field the user did not need is
      // a small annoyance; hiding one the server requires is an unsubmittable
      // form with no visible cause.
      map[rule.target_field] = evaluatePredicate(rule.predicate, values, { onUnknown: true });
    }
    return map;
  }, [doc.rules, doc.fields, values]);

  const clientErrors = useMemo(
    () => validate(doc, values, visibility),
    [doc, values, visibility],
  );

  /**
   * FOCUS MANAGEMENT for conditional fields (06 §14.4, points 3 and 4).
   *
   * 3. Fields APPEARING must not steal focus — a visible_when revealing three
   *    fields while the user is typing must not move the caret. Focus stays
   *    put and an aria-live region announces the change instead.
   * 4. A field REMOVED while focused moves focus to the preceding visible
   *    field rather than letting it fall to <body>, where a screen reader
   *    loses its place entirely and the user restarts from the top.
   */
  const prevVisible = useRef<Record<string, boolean>>(visibility);
  useEffect(() => {
    const before = prevVisible.current;
    const appeared: string[] = [];
    const disappeared: string[] = [];

    for (const field of doc.fields) {
      const wasVisible = before[field.key] !== false;
      const isVisible = visibility[field.key] !== false;
      if (!wasVisible && isVisible) appeared.push(field.key);
      if (wasVisible && !isVisible) disappeared.push(field.key);
    }

    if (disappeared.length) {
      const active = document.activeElement;
      const lostFocus = disappeared.some((key) => {
        const el = fieldRefs.current.get(key);
        return el && active instanceof Node && el.contains(active);
      });
      if (lostFocus) {
        // Move to the nearest PRECEDING still-visible field.
        const order = doc.fields.map((f) => f.key);
        const firstGoneIdx = Math.min(...disappeared.map((k) => order.indexOf(k)));
        for (let i = firstGoneIdx - 1; i >= 0; i--) {
          const key = order[i]!;
          if (visibility[key] === false) continue;
          fieldRefs.current.get(key)?.focus();
          break;
        }
      }
      disappeared.forEach((k) => fieldRefs.current.delete(k));
    }

    if (appeared.length) {
      setAnnouncement(
        appeared.length === 1
          ? "1 additional field added."
          : `${appeared.length} additional fields added.`,
      );
    } else if (disappeared.length) {
      setAnnouncement(
        disappeared.length === 1 ? "1 field removed." : `${disappeared.length} fields removed.`,
      );
    }

    prevVisible.current = visibility;
  }, [visibility, doc.fields]);

  const errorsFor = useCallback(
    (key: string): FieldError[] => {
      const server = (serverErrors[key] ?? []).map<FieldError>((message) => ({
        message,
        severity: "error",
      }));
      // Server errors always show. Client errors show only after the field has
      // been touched or a submit was attempted — validating an untouched form
      // paints every required field red before the user has typed a character,
      // which reads as "you have already failed".
      const client = touched[key] || attempted ? (clientErrors[key] ?? []) : [];
      return [...server, ...client];
    },
    [serverErrors, clientErrors, touched, attempted],
  );

  const blocking = useMemo(
    () =>
      Object.entries(clientErrors).some(
        ([key, errs]) => visibility[key] !== false && errs.some((e) => e.severity === "error"),
      ),
    [clientErrors, visibility],
  );

  const setValue = useCallback((key: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [key]: value }));
    // Clear the server error for this field the moment the user edits it: a
    // stale server error next to a changed value is actively misleading.
    setServerErrors((prev) => {
      if (!(key in prev)) return prev;
      const { [key]: _drop, ...rest } = prev;
      return rest;
    });
  }, []);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setAttempted(true);

    if (blocking) {
      // Focus moves to the ERROR SUMMARY, not to the first bad field. The
      // summary announces HOW MANY problems there are and lets the user
      // choose where to go. Jumping straight to field one hides the scale of
      // the problem from a screen-reader user, who then fixes one error,
      // resubmits, and discovers a second — three times.
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }

    // Hidden fields are EXCLUDED from the payload, not sent as null. A
    // conditional field the user never saw has no value — sending null asserts
    // "the user answered nothing", which for a nullable clinical field is a
    // different and wrong claim.
    const payload = Object.fromEntries(
      Object.entries(values).filter(([key]) => visibility[key] !== false),
    );

    const result = await onSubmit(payload, doc.meta.resolved_hash);
    if (!result.ok && result.fieldErrors) {
      setServerErrors(result.fieldErrors);
      requestAnimationFrame(() => summaryRef.current?.focus());
    }
  };

  // 02 §7.4 rule 2: the manifest changed between render and submit. The parent
  // swaps `doc`; we re-seed while PRESERVING values whose fields survived, so
  // a user does not lose ten minutes of typing to a manifest publish.
  useEffect(() => {
    setValues((prev) => {
      const next = seed(doc, undefined);
      for (const field of doc.fields) {
        if (field.key in prev) next[field.key] = prev[field.key];
      }
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc.meta.resolved_hash]);

  const groups: Array<{ section: RenderSection | null; fields: RenderField[] }> = doc.sections
    .length
    ? doc.sections.map((section) => ({
        section,
        fields: section.field_keys
          .map((k) => doc.fields.find((f) => f.key === k))
          .filter((f): f is RenderField => Boolean(f)),
      }))
    : [{ section: null, fields: doc.fields }];

  const summaryErrors = collectSummary(doc, clientErrors, serverErrors, visibility, attempted);
  const visibleErrorCount = summaryErrors.length;

  return (
    <form
      onSubmit={handleSubmit}
      // We own validation; native bubbles are unstyleable and unlocalized.
      noValidate
      aria-labelledby={`${formId}-title`}
      data-density={doc.meta.theme.density}
      className="space-y-8"
    >
      <div>
        <h2 id={`${formId}-title`} className="font-display text-page-title text-fg">
          {doc.title}
        </h2>
        {doc.description && <p className="mt-2 max-w-prose text-sm text-fg-muted">{doc.description}</p>}
      </div>

      <ErrorSummary
        ref={summaryRef}
        count={visibleErrorCount}
        errors={summaryErrors}
        onJump={(key) => fieldRefs.current.get(key)?.focus()}
      />

      {/* Conditional-field changes are announced here rather than by moving
          focus (§14.4 point 3). */}
      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>

      {groups.map(({ section, fields }) => {
        const anyVisible = fields.some((f) => visibility[f.key] !== false);
        if (!anyVisible) return null; // an entirely-hidden section renders nothing

        return (
          <fieldset key={section?.key ?? "_default"} className="space-y-5">
            {section && (
              <legend className="text-section font-semibold text-fg">
                {section.title}
                {section.description && (
                  <span className="mt-1 block text-sm font-normal text-fg-muted">
                    {section.description}
                  </span>
                )}
              </legend>
            )}

            <div className="grid grid-cols-12 gap-x-4 gap-y-5">
              {fields
                .filter((f) => visibility[f.key] !== false)
                .map((field) => {
                  // The unknown-widget fallback. Never a crash, never a drop.
                  const Widget = WIDGETS[field.widget] ?? UnsupportedWidget;
                  const inputId = `${formId}-${field.key}`;
                  const errs = errorsFor(field.key);
                  return (
                    <div
                      key={field.key}
                      className="col-span-12"
                      // Tailwind's JIT cannot generate col-span-{dynamic} and
                      // inline styles cannot be responsive, so the span travels
                      // as a custom property and globals.css applies it above md.
                      style={{ ["--col-span" as string]: field.layout.col_span }}
                      data-col-span={field.layout.col_span}
                    >
                      <FieldShell
                        field={field}
                        inputId={inputId}
                        errors={errs}
                        registerRef={(el) => {
                          if (el) fieldRefs.current.set(field.key, el);
                          else fieldRefs.current.delete(field.key);
                        }}
                      >
                        {({ describedBy, labelId }) => (
                          <Widget
                            field={field}
                            value={values[field.key]}
                            errors={errs}
                            disabled={readOnly || field.disabled || submitting}
                            inputId={inputId}
                            labelId={labelId}
                            describedBy={describedBy}
                            onChange={(v) => setValue(field.key, v)}
                            onBlur={() => setTouched((t) => ({ ...t, [field.key]: true }))}
                          />
                        )}
                      </FieldShell>
                    </div>
                  );
                })}
            </div>
          </fieldset>
        );
      })}

      {!readOnly && (
        <div className="flex flex-wrap items-center gap-3 border-t border-line pt-6">
          {/* The submit button is NOT disabled on validation errors. A disabled
              submit gives a keyboard or screen-reader user no way to discover
              WHY they cannot proceed — they tab to it, it is inert, and nothing
              is announced. Submitting and surfacing the summary is the
              accessible pattern (WCAG 3.3.1). */}
          <Button type="submit" variant="primary" dot disabled={submitting}>
            {submitting ? "Saving…" : doc.submit_label}
          </Button>
          <p aria-live="polite" className="text-sm text-fg-muted">
            {attempted && blocking
              ? `${visibleErrorCount} problem${visibleErrorCount === 1 ? "" : "s"} to fix.`
              : ""}
          </p>
        </div>
      )}
    </form>
  );
}

function seed(doc: RenderDocument, initial?: FormValues): FormValues {
  const out: FormValues = {};
  for (const field of doc.fields) {
    out[field.key] = initial?.[field.key] ?? field.default_value ?? defaultFor(field);
  }
  return out;
}

function defaultFor(field: RenderField): unknown {
  if (field.widget === "checkbox" || field.widget === "switch") return false;
  if (field.widget === "address") return {};
  if (field.widget === "slider" || field.widget === "rating") {
    return field.constraints.minimum ?? 0;
  }
  return "";
}

function collectSummary(
  doc: RenderDocument,
  clientErrors: Record<string, FieldError[]>,
  serverErrors: Record<string, string[]>,
  visibility: Record<string, boolean>,
  attempted: boolean,
): Array<{ key: string; label: string; message: string }> {
  const labelOf = (key: string) => doc.fields.find((f) => f.key === key)?.label ?? key;
  const out: Array<{ key: string; label: string; message: string }> = [];

  for (const [key, messages] of Object.entries(serverErrors)) {
    if (messages[0]) out.push({ key, label: labelOf(key), message: messages[0] });
  }
  if (attempted) {
    for (const [key, errs] of Object.entries(clientErrors)) {
      if (visibility[key] === false) continue;
      if (key in serverErrors) continue;
      const first = errs.find((e) => e.severity === "error");
      if (first) out.push({ key, label: labelOf(key), message: first.message });
    }
  }
  return out;
}
