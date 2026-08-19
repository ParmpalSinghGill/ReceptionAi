"use client";

import * as React from "react";
import {
  Button,
  ConversationCard,
  TranscriptRow,
  Waveform,
  cn,
} from "@receptionai/ui";
import { useDemoVoice } from "../lib/useDemoVoice";

/**
 * The live voice demo — a real call in the browser.
 *
 * Styled as the same ConversationCard the hero and the chat panel use, so the
 * three read as one product in three states rather than three widgets.
 *
 * Accessibility, per DESIGN.md §7:
 *  - Every control is a real `<button>`, reachable and operable by keyboard.
 *  - The transcript is a polite `aria-live` log, so new lines are announced
 *    without interrupting what the caller is saying.
 *  - Status is carried in text, never in colour alone.
 *  - The waveform is `aria-hidden` decoration; the transcript carries meaning.
 */
export function CallPanel({ onClose }: { onClose: () => void }) {
  const {
    status,
    error,
    lines,
    amplitudes,
    muted,
    start,
    hangup,
    toggleMute,
    microphones,
    microphoneId,
    setMicrophoneId,
  } = useDemoVoice();

  const logRef = React.useRef<HTMLDivElement>(null);
  const primaryRef = React.useRef<HTMLButtonElement>(null);

  // The visitor asked for a call, so the call control should be ready to press.
  React.useEffect(() => {
    primaryRef.current?.focus();
  }, []);

  React.useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const live = status === "live";
  const connecting = status === "connecting" || status === "requesting-mic";

  const statusText =
    status === "requesting-mic"
      ? "Waiting for microphone"
      : status === "connecting"
        ? "Connecting"
        : status === "live"
          ? muted
            ? "Live · muted"
            : "Live · listening"
          : status === "ended"
            ? "Call ended"
            : status === "error"
              ? "Not connected"
              : "Ready to call";

  return (
    <ConversationCard
      status={live ? "Live call · demo" : "Voice demo"}
      live={live}
      waveform={
        <div className="flex items-center justify-between gap-4">
          {/*
            Real amplitude only. When the mic is muted or the line is idle the
            bars fall flat, because a moving waveform with no audio behind it
            is a lie the design forbids (DESIGN.md §5).
          */}
          <Waveform
            height={28}
            bars={48}
            amplitudes={live && !muted ? amplitudes : undefined}
          />
          <span className="shrink-0 font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)] text-fg-subtle">
            Voice channel
          </span>
        </div>
      }
      footer={
        <div className="border-t border-line p-4">
          {/*
            The microphone picker sits with the call controls rather than behind
            a settings icon, because the moment it is needed is BEFORE the first
            call: a browser default pointing at the wrong device produces a call
            that connects, shows a flat waveform, and never transcribes a word.
            Nothing in that failure points at the microphone, so the control has
            to be visible before anyone knows to look for it.

            Disabled mid-call — switching the input device means renegotiating
            the WebRTC track, and swapping audio under a live conversation is
            worse than asking the caller to hang up and start again.
          */}
          {microphones.length > 0 ? (
            <div className="mb-3">
              <label
                htmlFor="mic-select"
                className="mb-1.5 block font-sans text-label font-semibold uppercase tracking-[var(--text-label-tracking)] text-fg-subtle"
              >
                Microphone
              </label>
              <select
                id="mic-select"
                value={microphoneId ?? ""}
                disabled={live}
                onChange={(e) => setMicrophoneId(e.target.value)}
                className="w-full rounded-input border border-line bg-bg-inset px-3 py-2 font-sans text-body-sm text-fg outline-none transition-colors focus-visible:border-border-strong focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg-raised disabled:cursor-not-allowed disabled:opacity-60"
              >
                {microphones.map((device, index) => (
                  <option key={device.deviceId} value={device.deviceId}>
                    {/*
                      Labels are empty until permission has been granted once —
                      a browser privacy rule, not a bug. A numbered fallback
                      beats a list of blank options.
                    */}
                    {device.label || `Microphone ${index + 1}`}
                  </option>
                ))}
              </select>
              {live ? (
                <p className="mt-1.5 font-sans text-caption text-fg-subtle">
                  Hang up to switch microphone.
                </p>
              ) : null}
            </div>
          ) : null}
          {error ? (
            <div
              role="alert"
              className="mb-3 flex items-start gap-2 rounded-input border border-danger bg-danger-dim px-3 py-2"
            >
              <span aria-hidden="true" className="mt-px font-semibold text-danger-fg">
                ✕
              </span>
              <p className="text-body-sm text-danger-fg">{error.message}</p>
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            {live ? (
              <>
                <Button
                  ref={primaryRef}
                  size="md"
                  variant="outline"
                  dot={false}
                  onClick={hangup}
                >
                  End call
                </Button>
                <Button
                  size="md"
                  variant="ghost"
                  dot={false}
                  onClick={toggleMute}
                  aria-pressed={muted}
                >
                  {muted ? "Unmute" : "Mute"}
                </Button>
              </>
            ) : (
              <Button
                ref={primaryRef}
                size="md"
                onClick={() => void start()}
                disabled={connecting}
              >
                {connecting
                  ? "Connecting…"
                  : status === "ended" || status === "error"
                    ? "Call again"
                    : "Start the call"}
              </Button>
            )}
          </div>

          <div className="mt-2 flex items-center justify-between gap-3">
            {/* Status as text, not colour alone. */}
            <p
              className="font-sans text-label uppercase tracking-[var(--text-label-tracking)] text-fg-subtle"
              // The status itself is announced: a caller who cannot see the
              // dot still needs to know the line went live.
              aria-live="polite"
            >
              {statusText}
            </p>
            <button
              type="button"
              onClick={() => {
                hangup();
                onClose();
              }}
              className="rounded-pill font-sans text-label uppercase tracking-[var(--text-label-tracking)] text-fg-subtle underline underline-offset-4 transition-colors duration-micro hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
            >
              Close
            </button>
          </div>
        </div>
      }
    >
      <div
        ref={logRef}
        aria-live="polite"
        aria-atomic="false"
        aria-label="Call transcript"
        role="log"
        tabIndex={0}
        className={cn(
          "flex max-h-72 min-h-40 flex-col gap-4 overflow-y-auto pr-1",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
        )}
      >
        {lines.map((line) => (
          <TranscriptRow
            key={line.id}
            speaker={line.role === "caller" ? "You" : "Desk"}
            tone={line.role}
          >
            {line.text}
          </TranscriptRow>
        ))}

        {lines.length === 0 ? (
          <p className="mt-auto text-body-sm text-fg-subtle">
            {status === "idle"
              ? "Press “Start the call”, allow your microphone, and just talk — try “I chipped a tooth, can anyone see me today?”"
              : status === "requesting-mic"
                ? "Allow microphone access in your browser to continue."
                : connecting
                  ? "Opening the line…"
                  : "Say hello when you’re ready."}
          </p>
        ) : null}
      </div>
    </ConversationCard>
  );
}
