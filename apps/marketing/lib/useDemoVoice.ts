"use client";

import * as React from "react";
import { ApiError, createDemoSession } from "./api";

export const MEDIA_BASE =
  process.env.NEXT_PUBLIC_MEDIA_URL ?? "http://localhost:8100";

export interface VoiceLine {
  id: string;
  role: "caller" | "desk";
  text: string;
}

export type VoiceStatus =
  | "idle" // nothing started; the button says "Request a call"
  | "requesting-mic" // waiting on the browser permission prompt
  | "connecting" // minting a session and negotiating WebRTC
  | "live" // connected; audio flowing both ways
  | "ended" // the visitor hung up
  | "error"; // terminal failure, see `error`

export interface VoiceError {
  message: string;
  retryable: boolean;
}

/** Bars in the live waveform. Matches the hero card's visual density. */
const BAR_COUNT = 48;

/**
 * Drives the browser voice demo.
 *
 * ── Credential ─────────────────────────────────────────────────────────────
 * Voice reuses the demo widget's credential exactly: `POST /public/demo/sessions`
 * mints a single-use ticket, and that ticket is handed to the media worker in
 * the WebRTC offer, which redeems it. There is deliberately no second auth
 * path — the ticket already answers "was this browser granted this
 * conversation", which is the only question the media worker needs to ask.
 *
 * The chat widget spends its ticket on the SSE stream; voice spends its own on
 * the offer. Each call mints a fresh one, so the two never contend.
 *
 * ── Audio ──────────────────────────────────────────────────────────────────
 * Microphone permission is requested BEFORE any network call, so a visitor who
 * declines never creates a conversation row. The waveform is driven by a real
 * AnalyserNode over the outbound mic track — DESIGN.md §5 forbids animating
 * bars that do not correspond to audio, so when the mic is muted or silent the
 * bars genuinely fall flat.
 */
export function useDemoVoice() {
  const [status, setStatus] = React.useState<VoiceStatus>("idle");
  const [error, setError] = React.useState<VoiceError | null>(null);
  const [lines, setLines] = React.useState<VoiceLine[]>([]);
  const [amplitudes, setAmplitudes] = React.useState<number[]>([]);
  const [muted, setMuted] = React.useState(false);
  // Available microphones, and which one to use. The browser's default is
  // frequently the wrong one — a laptop's built-in array when a headset is
  // plugged in, or a virtual device from a conferencing app that captures
  // silence. Without a picker the caller has no way to recover: the call
  // connects, the waveform sits flat, and nothing says why.
  const [microphones, setMicrophones] = React.useState<MediaDeviceInfo[]>([]);
  const [microphoneId, setMicrophoneId] = React.useState<string | null>(null);

  const pcRef = React.useRef<RTCPeerConnection | null>(null);
  const streamRef = React.useRef<MediaStream | null>(null);
  const audioRef = React.useRef<HTMLAudioElement | null>(null);
  const audioCtxRef = React.useRef<AudioContext | null>(null);
  const rafRef = React.useRef<number | null>(null);
  // Guards against a double-click starting two calls, which would mint two
  // conversations and leave one orphaned.
  const startingRef = React.useRef(false);

  /** List the microphones the browser will admit to having.
   *
   * Labels are empty until permission has been granted at least once — that is
   * a deliberate browser privacy rule, not a bug. Before consent the picker
   * shows "Microphone 1/2/3"; after the first call it shows real names. Call
   * this again after `getUserMedia` succeeds to upgrade the labels.
   */
  const refreshMicrophones = React.useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const inputs = devices.filter((d) => d.kind === "audioinput");
      setMicrophones(inputs);
      // Only default when nothing is chosen, so re-enumerating mid-call (a
      // headset being unplugged, say) does not silently switch the device
      // under a caller who picked one.
      setMicrophoneId((current) => {
        if (current && inputs.some((d) => d.deviceId === current)) return current;
        return inputs[0]?.deviceId ?? null;
      });
    } catch {
      // Enumeration failing is not worth an error banner: the call can still
      // start on the browser default, which is what happened before there was
      // a picker at all.
    }
  }, []);

  React.useEffect(() => {
    void refreshMicrophones();
    const md = navigator.mediaDevices;
    if (!md?.addEventListener) return;
    // Plugging in a headset mid-session should populate the list.
    md.addEventListener("devicechange", refreshMicrophones);
    return () => md.removeEventListener("devicechange", refreshMicrophones);
  }, [refreshMicrophones]);

  const pushLine = React.useCallback((role: "caller" | "desk", text: string) => {
    const clean = (text ?? "").trim();
    if (!clean) return;
    setLines((prev) => [
      ...prev,
      { id: `${Date.now()}-${prev.length}`, role, text: clean },
    ]);
  }, []);

  /** Tear down every resource. Safe to call twice — hangup and unmount race. */
  const cleanup = React.useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    // Stopping the tracks is what turns the browser's recording indicator off.
    // Leaving it on after a call reads as "this site is still listening".
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    void audioCtxRef.current?.close().catch(() => undefined);
    audioCtxRef.current = null;
    pcRef.current?.close();
    pcRef.current = null;
    if (audioRef.current) {
      audioRef.current.srcObject = null;
      audioRef.current = null;
    }
    setAmplitudes([]);
  }, []);

  React.useEffect(() => cleanup, [cleanup]);

  /** Sample the mic into the waveform. Real amplitude only (DESIGN.md §5). */
  const startMeter = React.useCallback((stream: MediaStream) => {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!Ctor) return;
    const ctx = new Ctor();
    audioCtxRef.current = ctx;
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.75;
    ctx.createMediaStreamSource(stream).connect(analyser);

    const bins = new Uint8Array(analyser.frequencyBinCount);
    const perBar = Math.floor(bins.length / BAR_COUNT) || 1;

    const tick = () => {
      analyser.getByteFrequencyData(bins);
      const next: number[] = [];
      for (let i = 0; i < BAR_COUNT; i++) {
        let sum = 0;
        for (let j = 0; j < perBar; j++) sum += bins[i * perBar + j] ?? 0;
        // /255 normalises the byte range; the exponent lifts quiet speech into
        // visible territory without letting room tone register as sound.
        next.push(Math.min(1, (sum / perBar / 255) ** 0.7));
      }
      setAmplitudes(next);
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  }, []);

  const hangup = React.useCallback(() => {
    cleanup();
    setStatus("ended");
  }, [cleanup]);

  const toggleMute = React.useCallback(() => {
    const track = streamRef.current?.getAudioTracks()[0];
    if (!track) return;
    track.enabled = !track.enabled;
    setMuted(!track.enabled);
  }, []);

  const start = React.useCallback(async () => {
    if (startingRef.current) return;
    startingRef.current = true;
    setError(null);
    setLines([]);

    try {
      // 1. Microphone first: a refusal must not create a conversation row.
      setStatus("requesting-mic");
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            // `exact` rather than a plain deviceId: a soft constraint lets the
            // browser silently substitute a different microphone, which is the
            // exact confusion the picker exists to end. If the chosen device is
            // gone we want an error naming it, not audio from somewhere else.
            ...(microphoneId ? { deviceId: { exact: microphoneId } } : {}),
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
      } catch (err) {
        setStatus("error");
        // These fail for genuinely different reasons and the fix differs each
        // time. One message for all three sends someone to their browser
        // settings when the real problem is another app holding the device.
        const name = err instanceof DOMException ? err.name : "";
        let message =
          "ReceptionAi needs your microphone to hold a call. Allow access in " +
          "your browser, then try again.";
        if (name === "NotFoundError" || name === "OverconstrainedError") {
          message =
            "That microphone is no longer available. Pick a different one and " +
            "try again.";
        } else if (name === "NotReadableError" || name === "AbortError") {
          message =
            "Your microphone is in use by another app. Close it — or choose a " +
            "different microphone — and try again.";
        }
        setError({ message, retryable: true });
        // The device may have vanished; refresh so the picker reflects reality.
        void refreshMicrophones();
        return;
      }
      streamRef.current = stream;
      setMuted(false);
      // Labels are only readable after consent, so this is the point at which
      // "Microphone 1" becomes "Jabra Evolve 20".
      void refreshMicrophones();

      // 2. Mint the demo session. Same endpoint and same ticket the chat
      //    widget uses; `voice` only changes how the row is labelled.
      setStatus("connecting");
      const session = await createDemoSession("voice");

      // 3. Negotiate WebRTC with the media worker.
      const pc = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
      });
      pcRef.current = pc;

      stream.getTracks().forEach((track) => pc.addTrack(track, stream));
      // The bot's voice. Created in JS rather than rendered so it cannot be
      // unmounted mid-call by a re-render.
      pc.addTransceiver("audio", { direction: "recvonly" });

      const el = new Audio();
      el.autoplay = true;
      audioRef.current = el;
      pc.ontrack = (event) => {
        el.srcObject = event.streams[0];
        void el.play().catch(() => undefined);
      };

      // Transcript arrives on the data channel.
      //
      // WE create the channel, so `pc.ondatachannel` never fires — that event
      // is for channels the *remote* peer opens, and the media worker only
      // ever answers. Attaching the handler to the channel we created is the
      // difference between a live transcript and a silently empty panel: the
      // server sends the messages either way and nothing errors.
      const channel = pc.createDataChannel("rtvi-ai");
      const handleFrame = (raw: string) => {
        try {
          const frame = JSON.parse(raw);
          const data = frame?.data;
          if (data?.type === "user_transcript") pushLine("caller", data.text);
          else if (data?.type === "assistant_transcript")
            pushLine("desk", data.text);
        } catch {
          // A malformed frame must never end a call.
        }
      };
      channel.onmessage = (msg) => handleFrame(msg.data as string);
      // Belt and braces: if the worker ever opens its own channel instead,
      // read that too rather than going quiet.
      pc.ondatachannel = (event) => {
        event.channel.onmessage = (msg) => handleFrame(msg.data as string);
      };

      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "connected") setStatus("live");
        else if (
          pc.connectionState === "failed" ||
          pc.connectionState === "disconnected"
        ) {
          setStatus("error");
          setError({
            message: "The call dropped. Check your connection and try again.",
            retryable: true,
          });
        }
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      // Wait for ICE gathering: the media worker answers a complete offer
      // rather than trickling candidates, which keeps signaling to one POST.
      await new Promise<void>((resolve) => {
        if (pc.iceGatheringState === "complete") return resolve();
        const check = () => {
          if (pc.iceGatheringState === "complete") {
            pc.removeEventListener("icegatheringstatechange", check);
            resolve();
          }
        };
        pc.addEventListener("icegatheringstatechange", check);
        // Some networks never reach "complete"; proceed with what we have.
        setTimeout(resolve, 2500);
      });

      const res = await fetch(`${MEDIA_BASE}/voice/offer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sdp: pc.localDescription?.sdp,
          type: pc.localDescription?.type,
          ticket: session.ticket,
          conversation_id: session.conversation_id,
        }),
      });
      if (!res.ok) {
        throw new ApiError(
          res.status === 503
            ? "All demo lines are busy right now. Try again in a moment."
            : "Could not start the call.",
          res.status,
        );
      }
      const answer = await res.json();
      await pc.setRemoteDescription(answer);

      startMeter(stream);
      if (session.greeting) pushLine("desk", session.greeting);
    } catch (cause) {
      cleanup();
      setStatus("error");
      setError({
        message:
          cause instanceof ApiError
            ? cause.message
            : "Could not reach the voice service.",
        retryable: true,
      });
    } finally {
      startingRef.current = false;
    }
  }, [cleanup, pushLine, startMeter]);

  return {
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
  };
}
