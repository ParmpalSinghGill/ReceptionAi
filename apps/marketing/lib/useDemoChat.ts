"use client";

import * as React from "react";
import {
  ApiError,
  createDemoSession,
  sendMessage,
  streamUrl,
  uuid,
  type SessionEvent,
  type StreamErrorEvent,
  type TokenEvent,
  type TurnCompleteEvent,
} from "./api";

export interface ChatMessage {
  id: string;
  role: "caller" | "desk";
  text: string;
  /** True while tokens are still arriving for this message. */
  streaming?: boolean;
}

export type ChatStatus =
  | "idle" // no session yet — the empty state
  | "connecting" // minting a session / opening the stream
  | "ready" // connected, awaiting input
  | "sending" // user turn posted, waiting for first token
  | "streaming" // tokens arriving
  | "error"; // terminal failure, see `error`

export interface ChatError {
  message: string;
  /** Whether a retry is worth offering. */
  retryable: boolean;
}

/** Max automatic re-mint attempts before surfacing a terminal error. */
const MAX_RECONNECTS = 3;

/**
 * Drives the public demo chat.
 *
 * ── Credential lifecycle ───────────────────────────────────────────────────
 * There are TWO credentials, and conflating them is what broke the widget:
 *
 *   ticket   single-use, ~20s TTL. Opening the SSE stream REDEEMS it. It is
 *            spent within milliseconds of the panel mounting, so it is useless
 *            for anything the user does afterwards.
 *   session  minted BY the stream and delivered as its first `session` event.
 *            30-minute TTL, renewed on use, scoped to one conversation. This
 *            is what authenticates message POSTs and reconnects.
 *
 * The widget opens the stream immediately and posts a message only when the
 * user finishes typing — seconds later. Posting with the ticket therefore
 * always 401'd, and the demo could never hold a conversation. Every backend
 * test posted BEFORE opening the stream (curl's natural order, the reverse of
 * a browser's), which is how the suite missed it.
 *
 * ── Reconnect ──────────────────────────────────────────────────────────────
 * `EventSource` reconnects on its own and reuses the ORIGINAL URL, which still
 * carries the now-spent ticket, so its built-in retry can only ever 401. We
 * take reconnection away from it entirely: on `onerror`, `close()` at once to
 * cancel the browser's retry, then reopen ourselves. Which credential we
 * reopen with depends on how far we got:
 *
 *   have a session  → reconnect with it directly. No re-mint round-trip.
 *   no session yet  → the stream died before the `session` event arrived, so
 *                     the ticket is gone with nothing to show for it. Re-mint.
 *
 * Backoff and an attempt ceiling keep a genuinely-down backend from turning
 * into a mint loop.
 */
export function useDemoChat() {
  const [messages, setMessages] = React.useState<ChatMessage[]>([]);
  const [status, setStatus] = React.useState<ChatStatus>("idle");
  const [error, setError] = React.useState<ChatError | null>(null);
  const [greeting, setGreeting] = React.useState<string | null>(null);

  const sourceRef = React.useRef<EventSource | null>(null);
  // True from the moment a session POST is issued until it settles, so two
  // overlapping `start()` calls cannot both mint (see `start`).
  const startingRef = React.useRef(false);
  // Set when a stream is ended deliberately (a server-sent `error` event), so
  // the transport-level handler does not treat it as a dropped connection.
  const terminatedRef = React.useRef(false);
  const conversationRef = React.useRef<string | null>(null);
  // The conversation session, once the stream has handed it over. Null until
  // the first `session` event arrives — `send()` waits for it, and the
  // reconnect path falls back to re-minting while it is still null.
  const sessionRef = React.useRef<string | null>(null);
  const reconnectsRef = React.useRef(0);
  const retryTimerRef = React.useRef<number | null>(null);
  // Guards against a reconnect being scheduled after the panel unmounts.
  const aliveRef = React.useRef(true);
  // The turn currently streaming, so deltas append to the right message.
  const activeTurnRef = React.useRef<string | null>(null);

  const teardown = React.useCallback(() => {
    sourceRef.current?.close();
    sourceRef.current = null;
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  React.useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      teardown();
    };
  }, [teardown]);

  /** Append a token delta to the in-flight desk message. */
  const appendDelta = React.useCallback((turnId: string, delta: string) => {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === "desk" && last.id === turnId) {
        return [...prev.slice(0, -1), { ...last, text: last.text + delta }];
      }
      // First token of a new turn: open a fresh desk message.
      return [...prev, { id: turnId, role: "desk", text: delta, streaming: true }];
    });
  }, []);

  const finishTurn = React.useCallback((turnId: string) => {
    activeTurnRef.current = null;
    setMessages((prev) =>
      prev.map((m) => (m.id === turnId ? { ...m, streaming: false } : m)),
    );
  }, []);

  /**
   * Drop a desk turn that was cut off mid-stream.
   *
   * When the transport dies partway through a reply, the resumed stream sends
   * the answer again under a NEW turn_id — so keeping the fragment would leave
   * a truncated "I can get you" stranded above the complete sentence. The
   * half-turn is discarded at the moment of the drop; the user sees the reply
   * restart rather than two contradictory copies.
   */
  const discardPartialTurn = React.useCallback(() => {
    const turnId = activeTurnRef.current;
    if (!turnId) return;
    activeTurnRef.current = null;
    setMessages((prev) =>
      prev.filter((m) => !(m.id === turnId && m.role === "desk" && m.streaming)),
    );
  }, []);

  /**
   * POST a turn with the conversation session.
   * Assumes the session is present; callers gate on that.
   */
  const postTurn = React.useCallback(async (content: string) => {
    const conversationId = conversationRef.current;
    const session = sessionRef.current;
    if (!conversationId || !session) return;

    try {
      await sendMessage(conversationId, content, uuid(), session);
    } catch (e) {
      if (!aliveRef.current) return;
      setStatus("error");
      setError({
        message:
          e instanceof ApiError && e.status === 0
            ? "Message not sent — the connection dropped."
            : e instanceof ApiError && e.status === 401
              ? // The session expired (30 minutes idle) or was revoked. Not a
                // transport problem, so the reconnect loop cannot fix it; the
                // user needs a fresh conversation.
                "This demo session expired. Start a new one to keep chatting."
              : e instanceof ApiError
                ? e.message
                : "Message could not be sent.",
        retryable: true,
      });
    }
  }, []);

  /**
   * A message typed before the `session` event arrived.
   *
   * The stream opens in milliseconds, so this window is small — but it is real
   * on a slow connection, and silently dropping the user's first message would
   * be the worst possible outcome. Held in a ref rather than state because the
   * `session` listener is registered once and must see the current value.
   */
  const pendingRef = React.useRef<string | null>(null);

  const flushPending = React.useCallback(async () => {
    const queued = pendingRef.current;
    if (!queued || !sessionRef.current) return;
    pendingRef.current = null;
    await postTurn(queued);
  }, [postTurn]);

  /**
   * Open a stream for `conversationId`, authenticating with either a
   * freshly-minted ticket or an existing conversation session.
   *
   * Declared as a ref-held function so the error handler can re-enter it
   * without a stale-closure dance.
   */
  const openStream = React.useRef<
    (
      conversationId: string,
      credential: { ticket: string } | { session: string },
    ) => void
  >(() => {});

  openStream.current = (conversationId, credential) => {
    if (!aliveRef.current) return;
    teardown();
    // A fresh stream starts un-terminated, so a later drop on THIS connection
    // is still eligible for the reconnect path.
    terminatedRef.current = false;

    const es = new EventSource(streamUrl(conversationId, credential));
    sourceRef.current = es;

    es.addEventListener("open", () => {
      // A successful open means the credential worked; reset the budget so a
      // later, unrelated drop gets its own full set of retries.
      reconnectsRef.current = 0;
      setStatus((s) => (s === "connecting" ? "ready" : s));
    });

    /**
     * The stream's first frame: the ticket has been exchanged for a
     * conversation session. Until this lands there is no credential that can
     * authenticate a POST, which is why `send()` waits for it.
     */
    es.addEventListener("session", (ev) => {
      const data = JSON.parse(
        (ev as MessageEvent<string>).data,
      ) as SessionEvent;
      sessionRef.current = data.session;
      // The server is authoritative about which conversation this session
      // belongs to; follow it rather than trusting the id we opened with.
      if (data.conversation_id) conversationRef.current = data.conversation_id;
      // A queued message was waiting for exactly this.
      void flushPending();
    });

    es.addEventListener("token", (ev) => {
      const data = JSON.parse((ev as MessageEvent<string>).data) as TokenEvent;
      activeTurnRef.current = data.turn_id;
      setStatus("streaming");
      appendDelta(data.turn_id, data.delta);
    });

    es.addEventListener("turn_complete", (ev) => {
      const data = JSON.parse(
        (ev as MessageEvent<string>).data,
      ) as TurnCompleteEvent;
      finishTurn(data.turn_id);
      setStatus("ready");
    });

    es.addEventListener("error", (ev) => {
      // A server-SENT `error` event (as opposed to a transport failure) has a
      // data payload. This is the provider-error path from the contract.
      const raw = (ev as MessageEvent<string>).data;
      if (!raw) return; // transport error — handled by onerror below
      let parsed: StreamErrorEvent | null = null;
      try {
        parsed = JSON.parse(raw) as StreamErrorEvent;
      } catch {
        /* fall through to the generic message */
      }
      // A server-sent error is TERMINAL for this stream: the backend told us
      // what went wrong, so reconnecting would just be told again. Marking the
      // stream as deliberately ended stops `onerror` — which fires as a
      // consequence of the teardown below — from starting the re-mint loop and
      // minting a fresh session per failure.
      terminatedRef.current = true;
      teardown();
      if (activeTurnRef.current) finishTurn(activeTurnRef.current);
      setStatus("error");
      setError({
        message:
          parsed?.message ??
          "The assistant hit a problem generating that reply.",
        retryable: parsed?.retryable ?? true,
      });
    });

    /**
     * Transport-level failure: the connection dropped, or the server rejected
     * it (401 because the ticket was already spent). `EventSource` does not
     * expose the status code, so both cases funnel here — and both are handled
     * the same way, which is exactly why the "401 = auth failed" reading is a
     * bug. The right move is always: close, re-mint, reopen.
     */
    es.onerror = () => {
      // A CLOSED source means the browser already gave up; an OPEN one means
      // this fired for a recoverable blip. Either way we take over.
      teardown();
      if (!aliveRef.current) return;
      // Suppressed when the stream was ended deliberately by a server-sent
      // `error` event: that failure is already surfaced with its own message
      // and a Try again button, and reconnecting would loop.
      if (terminatedRef.current) return;
      // The resumed stream replays the reply under a new turn id, so the
      // fragment received before the drop must not linger.
      discardPartialTurn();

      if (reconnectsRef.current >= MAX_RECONNECTS) {
        setStatus("error");
        setError({
          message:
            "Lost the connection to the demo service and could not restore it.",
          retryable: true,
        });
        return;
      }

      const attempt = reconnectsRef.current++;
      // 400ms, 800ms, 1600ms — brief, since reconnecting is usually a single
      // round-trip and only a genuinely-down backend needs the longer waits.
      const delay = 400 * 2 ** attempt;
      setStatus("connecting");

      retryTimerRef.current = window.setTimeout(async () => {
        if (!aliveRef.current) return;

        // Preferred path: we already hold a conversation session, which is
        // renewed on use and re-authenticates the stream directly. The spent
        // ticket is irrelevant — replaying it would 401 forever.
        const session = sessionRef.current;
        const conversationId = conversationRef.current;
        if (session && conversationId) {
          openStream.current(conversationId, { session });
          return;
        }

        try {
          // Fallback: the stream died before the `session` event arrived, so
          // the ticket was redeemed and we have nothing to show for it. The
          // only way back is a brand-new session.
          const minted = await createDemoSession();
          // A ticket is scoped to one conversation, so follow the id the new
          // session came back with rather than pairing it with the old one.
          conversationRef.current = minted.conversation_id;
          sessionRef.current = null;
          openStream.current(minted.conversation_id, { ticket: minted.ticket });
        } catch (e) {
          if (!aliveRef.current) return;
          setStatus("error");
          // Distinguish the three failures a re-mint can hit. They look
          // identical to the code and completely different to a person:
          // 0   — no response at all (server down, DNS, CORS-blocked)
          // 429 — the service is healthy and deliberately throttling us
          // else — reached it, and it refused
          let message = "Could not restore the demo session.";
          if (e instanceof ApiError && e.status === 0) {
            message =
              "Can't reach the demo service. Check your connection and try again.";
          } else if (e instanceof ApiError && e.status === 429) {
            message =
              "Too many demo sessions from this address just now. " +
              "Give it a minute and try again.";
          }
          setError({ message, retryable: true });
        }
      }, delay);
    };
  };

  /** Start a session and open the stream. Safe to call repeatedly. */
  const start = React.useCallback(async () => {
    // `sourceRef` alone is not enough: it is only set once the session POST
    // resolves, so React StrictMode's double-invoked mount effect fires two
    // overlapping starts and mints two sessions (burning a ticket and leaving
    // an orphan conversation). `startingRef` closes that window synchronously.
    if (sourceRef.current || startingRef.current) return;
    startingRef.current = true;
    setStatus("connecting");
    setError(null);
    reconnectsRef.current = 0;
    try {
      const minted = await createDemoSession();
      if (!aliveRef.current) return;
      conversationRef.current = minted.conversation_id;
      // A new conversation carries no session until its stream hands one over.
      sessionRef.current = null;
      pendingRef.current = null;
      setGreeting(minted.greeting);
      setMessages([{ id: "greeting", role: "desk", text: minted.greeting }]);
      openStream.current(minted.conversation_id, { ticket: minted.ticket });
    } catch (e) {
      if (!aliveRef.current) return;
      setStatus("error");
      setError({
        message:
          e instanceof ApiError && e.status === 0
            ? "Can't reach the demo service. It may not be running yet."
            : e instanceof ApiError && e.status === 429
              ? "The demo is busy right now. Give it a moment and try again."
              : "Could not start the demo session.",
        retryable: true,
      });
    } finally {
      startingRef.current = false;
    }
  }, []);

  /** Post a user turn. */
  const send = React.useCallback(
    async (text: string) => {
      const content = text.trim();
      const conversationId = conversationRef.current;
      if (!content || !conversationId) return;

      setMessages((prev) => [
        ...prev,
        { id: uuid(), role: "caller", text: content },
      ]);
      setStatus("sending");
      setError(null);

      // Without a session there is no credential that can authenticate the
      // POST — the ticket was spent opening the stream. Queue it; the
      // `session` handler flushes as soon as the credential lands.
      if (!sessionRef.current) {
        pendingRef.current = content;
        return;
      }

      await postTurn(content);
    },
    [postTurn],
  );

  /** Retry after a terminal error: fresh session, fresh stream. */
  const retry = React.useCallback(async () => {
    teardown();
    reconnectsRef.current = 0;
    // Clear the deliberate-termination flag, or the retried stream would treat
    // its own first drop as terminal and never reconnect.
    terminatedRef.current = false;
    startingRef.current = false;
    conversationRef.current = null;
    // Drop the old credential and any queued turn: `start()` below opens a
    // brand-new conversation, and carrying either across would post into the
    // previous one.
    sessionRef.current = null;
    pendingRef.current = null;
    setError(null);
    setMessages([]);
    setStatus("idle");
    await start();
  }, [start, teardown]);

  return { messages, status, error, greeting, start, send, retry };
}
