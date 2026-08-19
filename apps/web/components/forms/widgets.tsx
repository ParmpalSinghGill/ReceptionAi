"use client";

import { Check, ChevronsUpDown, Star, TriangleAlert, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import type { FieldError, RenderField, RenderFieldOption } from "@/lib/forms/types";
import { cn } from "@/lib/utils";

export interface WidgetProps {
  field: RenderField;
  value: unknown;
  errors: FieldError[];
  disabled: boolean;
  describedBy: string;
  labelId: string;
  inputId: string;
  onChange: (value: unknown) => void;
  onBlur: () => void;
}

const hasError = (errors: FieldError[]) => errors.some((e) => e.severity === "error");
const asString = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));

// --- Text ------------------------------------------------------------------

export function TextWidget({
  field, value, errors, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  return (
    <Input
      id={inputId}
      type={field.input_type === "custom" ? "text" : field.input_type}
      value={asString(value)}
      placeholder={field.placeholder}
      disabled={disabled}
      autoComplete={field.autocomplete}
      inputMode={field.input_type === "tel" ? "tel" : field.input_type === "number" ? "decimal" : undefined}
      minLength={field.constraints.min_length}
      maxLength={field.constraints.max_length}
      min={field.constraints.minimum ?? field.constraints.min_date}
      max={field.constraints.maximum ?? field.constraints.max_date}
      step={field.constraints.step}
      aria-describedby={describedBy || undefined}
      aria-invalid={hasError(errors) || undefined}
      onChange={(e) =>
        onChange(field.input_type === "number" ? e.target.valueAsNumber : e.target.value)
      }
      onBlur={onBlur}
    />
  );
}

export function TextareaWidget({
  field, value, errors, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  return (
    <Textarea
      id={inputId}
      value={asString(value)}
      placeholder={field.placeholder}
      disabled={disabled}
      maxLength={field.constraints.max_length}
      aria-describedby={describedBy || undefined}
      aria-invalid={hasError(errors) || undefined}
      onChange={(e) => onChange(e.target.value)}
      onBlur={onBlur}
    />
  );
}

// --- Choice ----------------------------------------------------------------

export function RadioWidget({
  field, value, errors, disabled, inputId, describedBy, labelId, onChange, onBlur,
}: WidgetProps) {
  return (
    <RadioGroup
      id={inputId}
      value={asString(value)}
      disabled={disabled}
      aria-describedby={describedBy || undefined}
      aria-invalid={hasError(errors) || undefined}
      // The GROUP is labelled by the field label; each option labels itself.
      aria-labelledby={labelId}
      onValueChange={(v) => {
        onChange(v);
        onBlur();
      }}
    >
      {(field.options ?? []).map((o) => {
        const id = `${inputId}-${o.value}`;
        return (
          <div key={o.value} className="flex items-start gap-3">
            <RadioGroupItem value={o.value} id={id} className="mt-0.5" />
            <label htmlFor={id} className="cursor-pointer text-sm">
              <span className="block text-fg">{o.label}</span>
              {o.description && (
                <span className="block text-xs text-fg-subtle">{o.description}</span>
              )}
            </label>
          </div>
        );
      })}
    </RadioGroup>
  );
}

export function CheckboxWidget({
  field, value, errors, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  return (
    <div className="flex items-start gap-3">
      <input
        id={inputId}
        type="checkbox"
        checked={value === true}
        disabled={disabled}
        aria-describedby={describedBy || undefined}
        aria-invalid={hasError(errors) || undefined}
        onChange={(e) => onChange(e.target.checked)}
        onBlur={onBlur}
        className="mt-0.5 size-5 shrink-0 cursor-pointer rounded-[4px] border border-strong bg-bg-inset accent-[var(--accent)]"
      />
      {field.placeholder && (
        <label htmlFor={inputId} className="cursor-pointer text-sm text-fg-muted">
          {field.placeholder}
        </label>
      )}
    </div>
  );
}

export function SwitchWidget({
  field, value, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  return (
    <div className="flex items-center gap-3">
      <Switch
        id={inputId}
        checked={value === true}
        disabled={disabled}
        aria-describedby={describedBy || undefined}
        onCheckedChange={(v) => {
          onChange(v);
          onBlur();
        }}
      />
      {/* The state is stated in words, never carried by the switch position
          alone (1.4.1 / 1.3.1). */}
      <span className="text-sm text-fg-muted">
        {value === true ? "Yes" : "No"}
        {field.placeholder ? ` — ${field.placeholder}` : ""}
      </span>
    </div>
  );
}

export function ComboboxWidget({
  field, value, errors, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  const [open, setOpen] = useState(false);
  const options: RenderFieldOption[] = field.options ?? [];
  const multiple = Array.isArray(value);
  const selected = multiple ? (value as string[]) : value ? [String(value)] : [];
  const labelOf = (v: string) => options.find((o) => o.value === v)?.label ?? v;

  const toggle = (optionValue: string) => {
    if (multiple) {
      const next = selected.includes(optionValue)
        ? selected.filter((v) => v !== optionValue)
        : [...selected, optionValue];
      onChange(next);
    } else {
      onChange(optionValue === value ? "" : optionValue);
      setOpen(false);
    }
    onBlur();
  };

  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) onBlur();
      }}
    >
      <PopoverTrigger asChild>
        <Button
          id={inputId}
          type="button"
          variant="secondary"
          role="combobox"
          aria-expanded={open}
          aria-describedby={describedBy || undefined}
          aria-invalid={hasError(errors) || undefined}
          disabled={disabled}
          className={cn(
            "h-10 w-full justify-between rounded-input px-3 font-normal",
            !selected.length && "text-fg-subtle",
            hasError(errors) && "border-danger",
          )}
        >
          <span className="truncate">
            {selected.length === 0
              ? (field.placeholder ?? "Select…")
              : selected.length === 1
                ? labelOf(selected[0]!)
                : `${selected.length} selected`}
          </span>
          <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" aria-hidden="true" />
        </Button>
      </PopoverTrigger>

      <PopoverContent align="start" className="w-[var(--radix-popover-trigger-width)] p-1">
        <ul role="listbox" aria-multiselectable={multiple || undefined} className="max-h-64 overflow-y-auto">
          {options.length === 0 && (
            <li className="px-3 py-2 text-sm text-fg-subtle">No options.</li>
          )}
          {options.map((o) => {
            const isSelected = selected.includes(o.value);
            return (
              <li key={o.value}>
                <button
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => toggle(o.value)}
                  className="flex w-full items-start gap-2 rounded-[6px] px-2 py-2 text-left text-sm hover:bg-bg-inset"
                >
                  <Check
                    className={cn("mt-0.5 size-4 shrink-0 text-accent", !isSelected && "opacity-0")}
                    aria-hidden="true"
                  />
                  <span className="flex flex-col">
                    <span className="text-fg">{o.label}</span>
                    {o.description && (
                      <span className="text-xs text-fg-subtle">{o.description}</span>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

// --- Date / time -----------------------------------------------------------

/**
 * Native date and time inputs rather than a custom calendar popover.
 *
 * A hand-built date picker is one of the most commonly inaccessible widgets on
 * the web, and the native control is keyboard-operable, localised, and
 * understood by every screen reader and mobile OS for free. `min`/`max` come
 * from the server as ABSOLUTE dates (02 §7.2), so the browser enforces the
 * same boundary the server will.
 */
export function DatePickerWidget(props: WidgetProps) {
  return <TextWidget {...props} field={{ ...props.field, input_type: "date" }} />;
}

export function TimePickerWidget(props: WidgetProps) {
  return <TextWidget {...props} field={{ ...props.field, input_type: "time" }} />;
}

// --- Range / rating --------------------------------------------------------

export function SliderWidget({
  field, value, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  const min = field.constraints.minimum ?? 0;
  const max = field.constraints.maximum ?? 10;
  const current = typeof value === "number" ? value : min;

  return (
    <div className="flex items-center gap-4">
      <Slider
        id={inputId}
        min={min}
        max={max}
        step={field.constraints.step ?? 1}
        value={[current]}
        disabled={disabled}
        aria-describedby={describedBy || undefined}
        // The numeric value is announced, not just the position.
        aria-valuetext={String(current)}
        onValueChange={([v]) => onChange(v)}
        onValueCommit={onBlur}
        className="flex-1"
      />
      <output htmlFor={inputId} className="tabular w-10 text-right text-sm text-fg">
        {current}
      </output>
    </div>
  );
}

export function RatingWidget({
  field, value, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  const max = field.constraints.maximum ?? 5;
  const current = typeof value === "number" ? value : 0;

  // A radio group under the hood, so arrow keys work and the state is real.
  return (
    <div
      id={inputId}
      role="radiogroup"
      aria-describedby={describedBy || undefined}
      className="flex items-center gap-1"
    >
      {Array.from({ length: max }, (_, i) => i + 1).map((n) => (
        <button
          key={n}
          type="button"
          role="radio"
          aria-checked={current === n}
          aria-label={`${n} out of ${max}`}
          disabled={disabled}
          onClick={() => {
            onChange(n);
            onBlur();
          }}
          className="flex size-9 items-center justify-center rounded-pill hover:bg-bg-inset"
        >
          <Star
            className={cn("size-5", n <= current ? "fill-accent text-accent" : "text-fg-subtle")}
            aria-hidden="true"
          />
        </button>
      ))}
      <span className="ml-2 text-sm text-fg-muted">
        {current > 0 ? `${current} of ${max}` : "Not rated"}
      </span>
    </div>
  );
}

// --- File ------------------------------------------------------------------

export function FileDropWidget({
  field, value, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const file = value instanceof File ? value : null;

  /**
   * A drop-only target excludes every keyboard user from a primary task
   * (06 §14.4). The real <input type="file"> is present and focusable; the
   * drop zone is a convenience layered on top of it, not a replacement.
   */
  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        const f = e.dataTransfer.files?.[0];
        if (f) {
          onChange(f);
          onBlur();
        }
      }}
      className={cn(
        "rounded-input border border-dashed p-4 transition-colors duration-micro",
        dragging ? "border-accent bg-accent-dim/30" : "border-strong bg-bg-inset",
      )}
    >
      <div className="flex flex-wrap items-center gap-3">
        <Upload className="size-5 shrink-0 text-fg-subtle" aria-hidden="true" />
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          disabled={disabled}
          accept={field.constraints.accept?.join(",")}
          aria-describedby={describedBy || undefined}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onChange(f);
            onBlur();
          }}
          className="min-w-0 flex-1 text-sm text-fg-muted file:mr-3 file:rounded-pill file:border-0 file:bg-accent file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-accent-fg"
        />
      </div>
      <p className="mt-2 text-xs text-fg-subtle">
        {file ? `Selected: ${file.name}` : "Drag a file here, or use the button above."}
        {field.constraints.accept?.length ? ` Accepts ${field.constraints.accept.join(", ")}.` : ""}
      </p>
    </div>
  );
}

// --- Signature -------------------------------------------------------------

export function SignatureWidget({
  value, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  /**
   * A canvas signature cannot be produced with a keyboard, so a TYPED name is
   * offered as an equal alternative rather than a fallback. Legally and
   * practically a typed name is a signature; a canvas that only accepts a
   * mouse is simply an exclusion.
   */
  return (
    <div className="space-y-2">
      <Input
        id={inputId}
        type="text"
        value={asString(value)}
        placeholder="Type your full name to sign"
        disabled={disabled}
        autoComplete="name"
        aria-describedby={describedBy || undefined}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
        className="font-display text-lg"
      />
      <p className="text-xs text-fg-subtle">
        Typing your name here counts as your signature.
      </p>
    </div>
  );
}

// --- Address ---------------------------------------------------------------

export function AddressWidget({
  value, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  const v = (value ?? {}) as Record<string, string>;
  const set = (part: string, next: string) => onChange({ ...v, [part]: next });

  const parts: Array<{ key: string; label: string; autocomplete: string; span: string }> = [
    { key: "line1", label: "Address line 1", autocomplete: "address-line1", span: "sm:col-span-2" },
    { key: "line2", label: "Address line 2", autocomplete: "address-line2", span: "sm:col-span-2" },
    { key: "city", label: "Town or city", autocomplete: "address-level2", span: "" },
    { key: "postcode", label: "Postcode", autocomplete: "postal-code", span: "" },
  ];

  return (
    <fieldset className="grid gap-3 sm:grid-cols-2" aria-describedby={describedBy || undefined}>
      {parts.map((p, i) => (
        <div key={p.key} className={cn("space-y-1.5", p.span)}>
          <label htmlFor={`${inputId}-${p.key}`} className="text-xs text-fg-subtle">
            {p.label}
          </label>
          <Input
            id={i === 0 ? inputId : `${inputId}-${p.key}`}
            type="text"
            value={v[p.key] ?? ""}
            disabled={disabled}
            autoComplete={p.autocomplete}
            onChange={(e) => set(p.key, e.target.value)}
            onBlur={onBlur}
          />
        </div>
      ))}
    </fieldset>
  );
}

// --- The degradation path --------------------------------------------------

/**
 * Rendered when `field.widget` is not in WIDGETS.
 *
 * This happens exactly once in the system's life cycle: a manifest schema
 * MINOR adds a widget, the backend ships, and a portal build from before that
 * change renders a form containing it. The two apps deploy independently, so
 * "rare" is not "never".
 *
 * The three WRONG answers, and why:
 *
 *   1. Throw. One unrecognised widget white-screens an entire intake form and
 *      the tenant cannot take a booking. The blast radius is wildly out of
 *      proportion to the cause.
 *   2. Render nothing. The field silently vanishes. If it is required, the
 *      server rejects the submit with an error pointing at a field that is not
 *      on screen — an unsubmittable form with no visible cause, which is the
 *      single worst outcome available.
 *   3. Render a generic input and say nothing. The user types free text into
 *      what should have been a constrained control and the server rejects it
 *      with a format error they cannot act on.
 *
 * What we do instead: render a text control (so the field is fillable and its
 * value reaches the server), label it HONESTLY, and report it. `input_type`
 * still arrives from the server even for an unknown widget, so the control is
 * usually close to right — an unknown widget over `input_type: "date"` still
 * gets a native date picker.
 */
export function UnsupportedWidget({
  field, value, errors, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  const reported = useRef(false);
  useEffect(() => {
    if (reported.current) return;
    reported.current = true;
    if (process.env.NODE_ENV !== "production") {
      console.warn(
        `[DynamicForm] Unknown widget "${field.widget}" for field "${field.key}". ` +
          `Falling back to a text control. This portal build predates the ` +
          `manifest schema that produced this form.`,
      );
    }
    // Reported ONCE per mount, not per render: a form with three unknown
    // widgets re-rendering on every keystroke would generate thousands of
    // events. No value and no label are sent — the label may be
    // tenant-authored, PII-adjacent text.
  }, [field.widget, field.key]);

  const Control = field.input_type === "text" && !field.constraints.max_length ? Textarea : Input;

  return (
    <div className="space-y-1.5">
      <Control
        id={inputId}
        type={field.input_type === "custom" ? "text" : field.input_type}
        value={asString(value)}
        placeholder={field.placeholder}
        disabled={disabled}
        aria-describedby={describedBy || undefined}
        aria-invalid={hasError(errors) || undefined}
        onChange={(e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
          onChange(e.target.value)
        }
        onBlur={onBlur}
      />
      <p className="flex items-start gap-1.5 text-xs text-fg-muted">
        <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        <span>
          This field normally uses a control this version of the portal does not have yet. You can
          still enter a value; refresh later for the full experience.
        </span>
      </p>
    </div>
  );
}
