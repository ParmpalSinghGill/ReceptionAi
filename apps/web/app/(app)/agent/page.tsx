"use client";

import { Lock, Play, RotateCcw, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Badge } from "@/components/ui/badge";
import { usePermissions } from "@/lib/auth/permissions";
import { useAiConfig, useModels, useSaveAiConfig } from "@/lib/query/hooks";
import type { AiConfig, PromptLayer } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export default function AgentConfigPage() {
  const config = useAiConfig();
  const models = useModels();
  const save = useSaveAiConfig();
  const { can } = usePermissions();
  const editable = can("agents:update");

  const [draft, setDraft] = useState<AiConfig | null>(null);
  useEffect(() => {
    if (config.data && !draft) setDraft(config.data);
  }, [config.data, draft]);

  if (!draft) {
    return (
      <PageShell>
        <div className="h-8 w-64 animate-pulse rounded-input bg-bg-inset" />
        <div className="h-96 animate-pulse rounded-card bg-bg-inset" />
      </PageShell>
    );
  }

  const model = models.data?.find((m) => m.id === draft.model_id);
  const tenantLayer = draft.layers.find((l) => l.editable);
  const dirty = JSON.stringify(draft) !== JSON.stringify(config.data);

  const setTenantContent = (content: string) =>
    setDraft({
      ...draft,
      layers: draft.layers.map((l) => (l.editable ? { ...l, content } : l)),
    });

  return (
    <PageShell>
      <PageHeader
        title="AI configuration"
        description="Which model answers your phone, and what it has been told."
        actions={
          <>
            {dirty && (
              <Button
                variant="ghost"
                onClick={() => config.data && setDraft(config.data)}
                disabled={save.isPending}
              >
                <RotateCcw aria-hidden="true" />
                Discard changes
              </Button>
            )}
            <Button
              variant="primary"
              dot
              disabled={!editable || !dirty || save.isPending}
              onClick={() => save.mutate(draft)}
            >
              {save.isPending ? "Saving…" : "Save configuration"}
            </Button>
          </>
        }
      />

      {!editable && (
        <p className="rounded-input border border-line bg-bg-inset p-3 text-sm text-fg-muted">
          You can see this configuration but not change it. Editing needs the admin or owner role.
        </p>
      )}

      {/* `min-w-0` on the grid: its columns default to an `auto` minimum, so
          the <pre> prompt layers and long unbroken model strings inside would
          widen the track past the viewport instead of scrolling in place. */}
      <div className="grid min-w-0 gap-4 lg:grid-cols-3">
        <Card className="min-w-0 lg:col-span-1">
          <CardHeader>
            <CardTitle>Model</CardTitle>
            <CardDescription>
              Faster models answer sooner; deeper ones handle awkward calls better.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="model">Primary model</Label>
              <Select
                value={draft.model_id}
                disabled={!editable}
                onValueChange={(v) => setDraft({ ...draft, model_id: v })}
              >
                <SelectTrigger id="model">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {["anthropic", "openai", "google"].map((provider) => {
                    const list = (models.data ?? []).filter((m) => m.provider === provider);
                    if (!list.length) return null;
                    // SelectGroup, not a bare <div>: Radix requires the group
                    // context for SelectLabel, and the grouping is what makes
                    // the label an accessible group name rather than a
                    // decorative row inside the listbox.
                    return (
                      <SelectGroup key={provider}>
                        <SelectLabel>{provider}</SelectLabel>
                        {list.map((m) => (
                          <SelectItem key={m.id} value={m.id}>
                            {m.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    );
                  })}
                </SelectContent>
              </Select>
              {model && (
                <p className="text-xs text-fg-subtle">
                  {model.latency_class} · £{model.input_cost_per_mtok} in / £
                  {model.output_cost_per_mtok} out per million tokens ·{" "}
                  {(model.context_window / 1000).toFixed(0)}k context
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="fallback">Fallback model</Label>
              <Select
                value={draft.fallback_model_id ?? "none"}
                disabled={!editable}
                onValueChange={(v) =>
                  setDraft({ ...draft, fallback_model_id: v === "none" ? null : v })
                }
              >
                <SelectTrigger id="fallback">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">None — fail the call</SelectItem>
                  {(models.data ?? []).map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-fg-subtle">
                Used if the primary model is unavailable mid-call.
              </p>
            </div>

            <div className="space-y-2">
              <div className="flex items-baseline justify-between">
                <Label htmlFor="temperature">Temperature</Label>
                <output htmlFor="temperature" className="tabular text-sm text-fg">
                  {draft.temperature.toFixed(2)}
                </output>
              </div>
              <Slider
                id="temperature"
                min={0}
                max={1}
                step={0.05}
                value={[draft.temperature]}
                disabled={!editable}
                aria-valuetext={`${draft.temperature.toFixed(2)} — ${
                  draft.temperature <= 0.3 ? "consistent" : draft.temperature <= 0.7 ? "balanced" : "varied"
                }`}
                onValueChange={([v]) => setDraft({ ...draft, temperature: v ?? 0 })}
              />
              <p className="text-xs text-fg-subtle">
                Low means the assistant says the same thing to the same question. For a front desk
                that is usually what you want.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="max-tokens">Longest reply</Label>
              <Input
                id="max-tokens"
                type="number"
                min={128}
                max={4096}
                step={128}
                value={draft.max_output_tokens}
                disabled={!editable}
                onChange={(e) =>
                  setDraft({ ...draft, max_output_tokens: Number(e.target.value) })
                }
              />
              <p className="text-xs text-fg-subtle">In tokens. 1024 is roughly 700 words.</p>
            </div>

            <div className="space-y-2">
              <Label>Tools the assistant may use</Label>
              <ul className="flex flex-wrap gap-1.5">
                {draft.tools_enabled.map((t) => (
                  <li key={t}>
                    <Badge tone="neutral" className="font-mono">
                      {t}
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>
          </CardContent>
        </Card>

        <div className="min-w-0 space-y-4 lg:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>Instructions</CardTitle>
              <CardDescription>
                Three layers, applied in order. You can only edit the last one — but you can read
                all of them, because instructions you cannot see are instructions you cannot
                debug.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {draft.layers.map((layer) => (
                <PromptLayerBlock
                  key={layer.key}
                  layer={layer}
                  editable={editable && layer.editable}
                  onChange={setTenantContent}
                />
              ))}

              <p className="tabular text-xs text-fg-subtle">
                About{" "}
                {draft.layers.reduce((n, l) => n + l.token_estimate, 0).toLocaleString("en-GB")}{" "}
                tokens are sent before the caller says anything.
              </p>
            </CardContent>
          </Card>

          <TestPanel
            temperature={draft.temperature}
            modelLabel={model?.label ?? draft.model_id}
            tenantInstructions={tenantLayer?.content ?? ""}
          />
        </div>
      </div>
    </PageShell>
  );
}

function PromptLayerBlock({
  layer,
  editable,
  onChange,
}: {
  layer: PromptLayer;
  editable: boolean;
  onChange: (content: string) => void;
}) {
  const id = `layer-${layer.key}`;
  return (
    <div
      className={cn(
        "rounded-card border p-4",
        layer.editable ? "border-accent/40 bg-bg-raised" : "border-line bg-bg-inset",
      )}
    >
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {!layer.editable && (
            <Lock className="size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" />
          )}
          <Label htmlFor={id} className="text-sm">
            {layer.label}
          </Label>
          {/* Read-only is stated in words, not just implied by a greyer box. */}
          {!layer.editable && <Badge tone="neutral">Read only</Badge>}
        </div>
        <span className="tabular text-xs text-fg-subtle">
          ~{layer.token_estimate.toLocaleString("en-GB")} tokens
        </span>
      </div>

      <p className="mb-3 text-xs text-fg-muted">{layer.description}</p>

      {editable ? (
        <Textarea
          id={id}
          value={layer.content}
          rows={12}
          onChange={(e) => onChange(e.target.value)}
          className="font-mono text-xs leading-relaxed"
        />
      ) : (
        // A disabled <textarea> would be unfocusable and unreadable by
        // keyboard; a <pre> with tabIndex is scrollable and selectable, which
        // is what "read only" should actually mean.
        <pre
          id={id}
          tabIndex={0}
          className="scroll-x max-h-64 overflow-y-auto whitespace-pre-wrap rounded-input bg-bg p-3 font-mono text-xs leading-relaxed text-fg-muted"
        >
          {layer.content}
        </pre>
      )}

      <p className="mt-2 font-mono text-xs text-fg-subtle">{layer.source}</p>
    </div>
  );
}

/**
 * The test-conversation panel.
 *
 * Deliberately labelled as a simulation rather than dressed up as a live call:
 * it runs against the DRAFT configuration, which is not what production is
 * using until Save is pressed, and implying otherwise would be the single most
 * dangerous thing this screen could do.
 */
function TestPanel({
  temperature,
  modelLabel,
  tenantInstructions,
}: {
  temperature: number;
  modelLabel: string;
  tenantInstructions: string;
}) {
  const [messages, setMessages] = useState<Array<{ role: "caller" | "desk"; text: string }>>([
    { role: "desk", text: "Thanks for calling Northgate Dental — how can I help?" },
  ]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const logRef = useRef<HTMLDivElement>(null);

  const send = () => {
    const text = input.trim();
    if (!text || streaming) return;
    setMessages((m) => [...m, { role: "caller", text }]);
    setInput("");
    setStreaming(true);
    setStreamText("");

    // A canned reply, streamed a token at a time so the streaming UI and its
    // announcement behaviour are exercised. Real streaming arrives via the SSE
    // endpoint in PHASE1-API-CONTRACT.md.
    const reply = replyFor(text, tenantInstructions);
    const words = reply.split(" ");
    let i = 0;
    const tick = setInterval(() => {
      i += 1;
      setStreamText(words.slice(0, i).join(" "));
      if (i >= words.length) {
        clearInterval(tick);
        setStreaming(false);
        setStreamText("");
        setMessages((m) => [...m, { role: "desk", text: reply }]);
      }
    }, 45);
  };

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [messages, streamText]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Try it</CardTitle>
        <CardDescription>
          A simulated caller, against the settings above as you have them now — not against what
          is live. Save to make these settings answer real calls.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div
          ref={logRef}
          className="max-h-80 space-y-3 overflow-y-auto rounded-card border border-line bg-bg-inset p-4"
        >
          {messages.map((m, i) => (
            <div key={i} className="grid grid-cols-[3.5rem_minmax(0,1fr)] gap-3">
              <span
                className={cn(
                  "text-label uppercase",
                  m.role === "desk" ? "text-accent" : "text-fg-subtle",
                )}
              >
                {m.role === "desk" ? "Desk" : "Caller"}
              </span>
              <p className="text-sm text-fg">{m.text}</p>
            </div>
          ))}

          {streaming && (
            <div className="grid grid-cols-[3.5rem_minmax(0,1fr)] gap-3">
              <span className="text-label uppercase text-accent">Desk</span>
              <div>
                {/* Visual: updates every token, hidden from assistive tech so
                    it cannot generate its own announcements. */}
                <p aria-hidden="true" className="text-sm text-fg">
                  {streamText}
                  <span className="ml-0.5 inline-block h-4 w-0.5 animate-caret-blink bg-current align-middle motion-reduce:animate-none" />
                </p>
                {/* Status is a SEPARATE live region from the text, so
                    "responding" is never interleaved into a sentence. */}
                <div className="sr-only" role="status" aria-live="polite">
                  Assistant is responding
                </div>
              </div>
            </div>
          )}
        </div>

        {/* The completed reply is announced once, in full, rather than
            token-by-token — a 40-word answer announced per token becomes
            several hundred spoken fragments and the listener hears nothing. */}
        <div className="sr-only" aria-live="polite" aria-atomic="false">
          {!streaming && messages.at(-1)?.role === "desk" ? messages.at(-1)?.text : ""}
        </div>

        <div className="flex gap-2">
          <Input
            value={input}
            placeholder="Say something a caller might say…"
            aria-label="Test message"
            disabled={streaming}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          <Button variant="primary" onClick={send} disabled={streaming || !input.trim()}>
            <Send aria-hidden="true" />
            <span className="sr-only sm:not-sr-only">Send</span>
          </Button>
        </div>

        <p className="tabular text-xs text-fg-subtle">
          {modelLabel} · temperature {temperature.toFixed(2)}
        </p>

        <div className="flex flex-wrap gap-2">
          {[
            "I've got terrible toothache, can I be seen today?",
            "What time do you close on a Saturday?",
            "How much is a check-up?",
          ].map((s) => (
            /* `Button` is whitespace-nowrap by default, which is right for a
               two-word label and wrong for a whole sentence: at 375px these
               pushed the page wider than the viewport. Wrapping keeps the
               suggestion readable rather than truncating it. */
            <Button
              key={s}
              variant="secondary"
              size="sm"
              disabled={streaming}
              onClick={() => setInput(s)}
              className="h-auto max-w-full whitespace-normal py-1.5 text-left"
            >
              <Play aria-hidden="true" className="shrink-0" />
              {s}
            </Button>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function replyFor(text: string, tenantInstructions: string): string {
  const t = text.toLowerCase();
  const mentionsEmergency = tenantInstructions.toLowerCase().includes("emergency");

  if (/pain|ache|hurt|emergency|swollen|broken/.test(t)) {
    return mentionsEmergency
      ? "I'm sorry to hear that. We hold emergency slots each morning — I can offer you 08:20 tomorrow with Dr Osei, or 16:40 in the afternoon. Which suits you better?"
      : "I'm sorry to hear that. Let me check what appointments are available and get you seen as soon as we can.";
  }
  if (/close|open|hours|saturday|sunday/.test(t)) {
    return "We're open 08:30 to 17:30 Monday to Thursday, 08:30 to 15:00 on Friday, and 09:00 to 13:00 on Saturday. We're closed on Sundays.";
  }
  if (/cost|price|how much|charge|fee/.test(t)) {
    return "A private check-up is £68, and the NHS band 1 charge applies if you're an NHS patient with us. Would you like me to book you in?";
  }
  return "Of course — let me help with that. Could I take your name and date of birth so I can find you on the system?";
}
