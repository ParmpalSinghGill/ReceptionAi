"use client";

import { Check, Loader2, TriangleAlert, Wrench } from "lucide-react";

import type { ToolCall, Turn } from "@/lib/api/types";
import { formatLatency } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The transcript viewer, styled as DESIGN.md §5's conversation card: rows of
 * a `LABEL` (11px, --fg-subtle, uppercase) beside the message text, on
 * --bg-raised with a 12px radius.
 *
 * The label reads CALLER / DESK rather than "user"/"assistant" — the same
 * words the marketing page uses, because a receptionist reading this should
 * recognise the thing they saw on the website.
 *
 * Keyboard operation (DESIGN.md §7): the transcript is a semantic list, and
 * each turn's tool calls are a real <details> so they are reachable and
 * toggleable with the keyboard rather than being a div with an onClick.
 */
export function TranscriptViewer({ turns }: { turns: Turn[] }) {
  return (
    <ol className="divide-y divide-[var(--border)]">
      {turns.map((turn) => (
        <li key={turn.id} className="py-4 first:pt-0 last:pb-0">
          {/* `minmax(0,1fr)` rather than `1fr`: a grid track's default minimum
              is `auto`, so a long unbroken tool-call payload in the <pre>
              below would widen the column past the viewport instead of
              scrolling inside its own box (1.4.10 Reflow). */}
          <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-3 sm:grid-cols-[5.5rem_minmax(0,1fr)] sm:gap-4">
            <div className="pt-0.5">
              <span
                className={cn(
                  "text-label uppercase",
                  turn.role === "desk" ? "text-accent" : "text-fg-subtle",
                )}
              >
                {turn.role === "desk" ? "Desk" : turn.role === "caller" ? "Caller" : "System"}
              </span>
              {/* Latency belongs to the DESK's turn — it is the time from the
                  caller finishing to the first token back. Putting it on the
                  caller's row would attribute the wait to the wrong party. */}
              {turn.latency_ms !== null && (
                <span className="tabular mt-1 block text-xs text-fg-subtle">
                  {formatLatency(turn.latency_ms)}
                </span>
              )}
            </div>

            <div className="min-w-0 space-y-2">
              <p className="text-fg">{turn.content}</p>

              {turn.tool_calls.length > 0 && <ToolCalls calls={turn.tool_calls} />}

              {turn.usage && (
                <p className="tabular text-xs text-fg-subtle">
                  {turn.usage.prompt_tokens.toLocaleString("en-GB")} in ·{" "}
                  {turn.usage.completion_tokens.toLocaleString("en-GB")} out
                  {turn.finish_reason && turn.finish_reason !== "stop" && (
                    <span className="ml-2">· finished: {turn.finish_reason}</span>
                  )}
                </p>
              )}
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}

const TOOL_ICON = {
  running: Loader2,
  succeeded: Check,
  failed: TriangleAlert,
} as const;

const TOOL_TONE = {
  running: "text-info-fg",
  succeeded: "text-ok-fg",
  failed: "text-danger-fg",
} as const;

function ToolCalls({ calls }: { calls: ToolCall[] }) {
  return (
    <ul className="space-y-1.5">
      {calls.map((call) => {
        const Icon = TOOL_ICON[call.status];
        return (
          <li key={call.id}>
            <details className="group rounded-input border border-line bg-bg-inset">
              {/* `flex-wrap` plus a truncating label: at 375px the tool key,
                  the human label, the duration and the status glyph do not fit
                  on one line, and forcing them to made the row overflow the
                  card. Wrapping keeps every part readable instead. */}
              <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 text-xs">
                <Wrench className="size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" />
                <span className="font-mono text-fg">{call.tool_key}</span>
                <span className="min-w-0 truncate text-fg-subtle">{call.label}</span>
                <span className="ml-auto flex shrink-0 items-center gap-1.5">
                  {call.duration_ms !== null && (
                    <span className="tabular text-fg-subtle">
                      {formatLatency(call.duration_ms)}
                    </span>
                  )}
                  {/* Icon AND the status word — never colour alone. */}
                  <Icon
                    className={cn(
                      "size-3.5",
                      TOOL_TONE[call.status],
                      call.status === "running" && "animate-spin motion-reduce:animate-none",
                    )}
                    aria-hidden="true"
                  />
                  <span className={cn("sr-only", TOOL_TONE[call.status])}>{call.status}</span>
                </span>
              </summary>

              <div className="space-y-2 border-t border-line px-3 py-2">
                {call.arguments_preview && (
                  <div>
                    <p className="text-label uppercase text-fg-subtle">Arguments</p>
                    <pre className="scroll-x mt-1 font-mono text-xs text-fg-muted">
                      {call.arguments_preview}
                    </pre>
                  </div>
                )}
                {call.result_preview && (
                  <div>
                    <p className="text-label uppercase text-fg-subtle">Result</p>
                    <pre className="scroll-x mt-1 font-mono text-xs text-fg-muted">
                      {call.result_preview}
                    </pre>
                  </div>
                )}
              </div>
            </details>
          </li>
        );
      })}
    </ul>
  );
}
