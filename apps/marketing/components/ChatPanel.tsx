"use client";

import * as React from "react";
import {
  Button,
  ConversationCard,
  Input,
  TranscriptRow,
  Waveform,
  cn,
} from "@receptionai/ui";
import { useDemoChat } from "../lib/useDemoChat";

/**
 * The live chat demo, styled like the hero card so the two read as the same
 * product in two channels.
 */
export function ChatPanel({ onClose }: { onClose: () => void }) {
  const { messages, status, error, activity, start, send, retry } =
    useDemoChat();
  const [draft, setDraft] = React.useState("");
  const inputRef = React.useRef<HTMLInputElement>(null);
  const logRef = React.useRef<HTMLDivElement>(null);

  // Open the session as soon as the panel mounts, so the first message has a
  // conversation to land in.
  React.useEffect(() => {
    void start();
  }, [start]);

  // Move focus to the composer when the panel opens — the user asked for the
  // chat, so the chat should be ready to type into.
  React.useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Keep the newest turn in view as tokens arrive. `block: "nearest"` avoids
  // yanking the whole page around when the panel is mid-viewport.
  React.useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const busy = status === "sending" || status === "streaming";
  const connecting = status === "connecting";
  // Only the greeting present means the visitor has not spoken yet.
  const showEmptyState =
    messages.filter((m) => m.role === "caller").length === 0 && !error;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const text = draft.trim();
    if (!text || busy || connecting) return;
    setDraft("");
    void send(text);
  }

  return (
    <ConversationCard
      status="Live chat · demo"
      live={status !== "error"}
      elapsed={undefined}
      waveform={
        // Flat, minimum-height bars: this is a TEXT channel, so there is no
        // audio and the waveform must not imply any (DESIGN.md §5).
        <div className="flex items-center justify-between gap-4">
          <Waveform height={24} bars={40} />
          <span className="shrink-0 font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)] text-fg-subtle">
            Text channel
          </span>
        </div>
      }
      footer={
        <div className="border-t border-line p-4">
          {error ? (
            <div
              // assertive: a failure the visitor must act on should interrupt.
              role="alert"
              className="mb-3 flex items-start gap-2 rounded-input border border-danger bg-danger-dim px-3 py-2"
            >
              <span aria-hidden="true" className="mt-px font-semibold text-danger-fg">
                ✕
              </span>
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <p className="text-body-sm text-danger-fg">{error.message}</p>
                {error.retryable ? (
                  <div>
                    <Button size="sm" variant="outline" dot={false} onClick={() => void retry()}>
                      Try again
                    </Button>
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          <form onSubmit={onSubmit} className="flex items-center gap-2">
            <label htmlFor="chat-input" className="sr-only">
              Your message to the front desk
            </label>
            <Input
              id="chat-input"
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={
                connecting ? "Connecting…" : "Ask about an appointment…"
              }
              disabled={connecting || Boolean(error)}
              autoComplete="off"
            />
            <Button
              type="submit"
              size="md"
              dot={false}
              disabled={!draft.trim() || busy || connecting || Boolean(error)}
            >
              Send
            </Button>
          </form>

          <div className="mt-2 flex items-center justify-between gap-3">
            {/* Status in text, not colour alone (DESIGN.md §7). */}
            <p className="font-sans text-label uppercase tracking-[var(--text-label-tracking)] text-fg-subtle">
              {connecting
                ? "Connecting"
                : status === "sending"
                  ? "Sending"
                  : status === "streaming"
                    ? "Replying"
                    : status === "error"
                      ? "Disconnected"
                      : "Connected"}
            </p>
            <button
              type="button"
              onClick={onClose}
              className="rounded-pill font-sans text-label uppercase tracking-[var(--text-label-tracking)] text-fg-subtle underline underline-offset-4 transition-colors duration-micro hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
            >
              Close chat
            </button>
          </div>
        </div>
      }
    >
      <div
        ref={logRef}
        // The streaming transcript is a polite live region: new tokens are
        // announced without interrupting, per DESIGN.md §7.
        aria-live="polite"
        aria-atomic="false"
        aria-busy={busy}
        aria-label="Chat transcript"
        role="log"
        tabIndex={0}
        className={cn(
          "flex max-h-72 min-h-40 flex-col gap-4 overflow-y-auto pr-1",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
        )}
      >
        {messages.map((m) => (
          <TranscriptRow
            key={m.id}
            speaker={m.role === "caller" ? "You" : "Desk"}
            tone={m.role === "caller" ? "caller" : "desk"}
            streaming={m.streaming}
          >
            {m.text}
          </TranscriptRow>
        ))}

        {/*
          What the desk is doing, while it does it.

          A booking turn is several provider round trips — measured at ~5s — and
          before this the whole wait was blank: you typed, nothing moved, then a
          finished reply appeared at once. The backend already emitted a
          manifest-authored label ("Checking the diary") partway through; the
          widget discarded it.

          `aria-live="polite"` so a screen-reader user hears the same progress a
          sighted one sees, and `aria-hidden` on the dots because "· · ·" read
          aloud is noise.
        */}
        {activity ? (
          <p
            aria-live="polite"
            className="flex items-center gap-2 font-sans text-body-sm text-fg-subtle"
          >
            <span aria-hidden="true" className="flex gap-1">
              <span className="size-1.5 animate-pulse rounded-full bg-accent [animation-delay:0ms]" />
              <span className="size-1.5 animate-pulse rounded-full bg-accent [animation-delay:150ms]" />
              <span className="size-1.5 animate-pulse rounded-full bg-accent [animation-delay:300ms]" />
            </span>
            {activity}…
          </p>
        ) : null}

        {showEmptyState ? (
          <p className="mt-auto text-body-sm text-fg-subtle">
            Try &ldquo;Do you take Delta Dental?&rdquo; or &ldquo;I need a
            cleaning next week.&rdquo;
          </p>
        ) : null}
      </div>
    </ConversationCard>
  );
}
