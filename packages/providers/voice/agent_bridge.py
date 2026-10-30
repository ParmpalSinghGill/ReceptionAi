"""Bridge: our LangGraph runtime ↔ a Pipecat pipeline stage.

The composed pipeline is ``STT → this → TTS``. This processor is where "the
voice path must reach the same agent as chat" is actually enforced: it holds no
prompt, no tool table and no model client of its own. It receives a user
utterance and calls the same :class:`~packages.ai.runner.GraphRunner` that
``apps/api`` calls for web chat, against the same tenant manifest. A booking
made by voice therefore runs
``packages.ai.tools.builtin.booking.book_appointment`` — the identical code
path, writing the identical row.

Three behaviours below are not obvious and each fixes a real failure:

1. **Inference runs off the process-frame task.** Awaiting a multi-second graph
   run inside ``process_frame`` means Pipecat's barge-in waits forever for that
   task to exit, and the call goes dead silent.
2. **The reply is spoken as one utterance.** Default sentence aggregation splits
   a multi-sentence reply into separate TTS jobs; the gap between jobs unmutes
   the mic, speaker echo interrupts, and the caller hears half an answer.
3. **Utterances that arrive mid-turn are queued, not dropped.** A caller who
   says "oh and" then "make it Tuesday" must not lose the second half.

Two optional behaviours, both off unless the session turns them on:

* **Speculation.** On an *early* end-of-turn signal (VAD's pause, or Flux's
  EagerEndOfTurn) the agent starts on the words heard so far. When the turn is
  confirmed with the same words, that run's answer is used — the time between
  the early signal and the confirmation is saved. When the caller resumes, the
  run is cancelled and its rows are withdrawn from history. Side-effecting
  tools (booking, cancelling) wait for confirmation regardless; see
  :mod:`packages.ai.tools.gate`.
* **Metrics.** Each agent turn's timings go to a
  :class:`~packages.voice.call_metrics.CallMetrics` row.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from packages.ai.tools.gate import side_effects_held
from packages.providers.voice.turn_signals import UserTurnText, normalize_utterance
from packages.voice.call_metrics import CallMetrics, TurnRecord
from packages.voice.phone import (
    FRAGMENT_FLUSH_SECONDS,
    digits_in,
    is_complete_number,
    looks_like_phone_fragment,
    wants_phone_number,
)
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSSpeakFrame,
    UninterruptibleFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

__all__ = ["VoiceAgentProcessor", "last_user_text", "merge_utterances"]

log = structlog.get_logger(__name__)

#: A repeat of the same text inside this window is an artefact of the turn
#: machinery (two strategies opening one turn), not the caller saying it twice.
_DUPLICATE_WINDOW_S = 2.0

#: Spoken when the graph fails outright. The caller gets a sentence rather than
#: silence; the detail goes to the log, never to the speaker.
_FALLBACK_REPLY = "Sorry, I had trouble with that. Could you say it again?"

#: Silence between the preamble finishing and the reply starting. VOICE ONLY.
#:
#: Two utterances butted end to end read as one run-on sentence — "let me check
#: the diary Thursday at ten is free" — because TTS gives no sentence-final
#: pause between separate frames. Half a second is the beat a person leaves
#: after saying "one moment", short enough that nobody thinks the line dropped.
_PREAMBLE_GAP_S = 0.5

#: Ceiling on waiting for the preamble's audio to finish.
#:
#: The wait ends on BotStoppedSpeakingFrame, which the output transport emits.
#: If that frame never arrives — TTS error, transport hiccup — the turn must
#: still complete, so the wait is bounded and simply proceeds on timeout. Sized
#: for a long preamble read slowly, not for a typical one.
_PREAMBLE_MAX_WAIT_S = 12.0


def merge_utterances(left: str, right: str) -> str:
    """Join two utterances, dropping exact duplicates and containment.

    STT re-emits overlapping text often enough that naive concatenation
    produces "my number is my number is 07700".
    """
    a = (left or "").strip()
    b = (right or "").strip()
    if not a:
        return b
    if not b:
        return a
    if b == a or b in a:
        return a
    if a in b:
        return b
    return f"{a} {b}"


def last_user_text(context: Any) -> str:
    """Best-effort extract of the newest user utterance from an LLMContext.

    Defensive about shape because the context's message objects differ between
    aggregators (dicts here, objects there) and a hard assumption would turn a
    library upgrade into a mute call.
    """
    messages = getattr(context, "messages", None) or []
    for msg in reversed(list(messages)):
        if isinstance(msg, dict):
            role = msg.get("role") or msg.get("type")
            content = msg.get("content", "")
        else:
            role = getattr(msg, "role", None) or getattr(msg, "type", None)
            content = getattr(msg, "content", "")
        if str(role or "").lower() not in ("user", "human"):
            continue
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                else:
                    parts.append(str(getattr(part, "text", part)))
            return " ".join(parts).strip()
        return str(content).strip()
    return ""


# Guarded frame variants: an InterruptionFrame arriving while we are pushing a
# reply must not discard the reply we just committed to speaking. Marking these
# uninterruptible keeps text and audio in the queue together.
@dataclass
class _GuardedResponseStart(LLMFullResponseStartFrame, UninterruptibleFrame):
    pass


@dataclass
class _GuardedResponseEnd(LLMFullResponseEndFrame, UninterruptibleFrame):
    pass


@dataclass
class _GuardedTTSSpeak(TTSSpeakFrame, UninterruptibleFrame):
    pass




@dataclass(eq=False)
class _Speculation:
    """An agent run started on an early end-of-turn signal."""

    text: str
    norm: str
    started_at: float
    #: Released when the turn is confirmed; side-effecting tools wait on it.
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    #: Preamble produced before confirmation. Not spoken until confirmed — a
    #: caller who is still mid-sentence must not hear "let me check the diary".
    preamble: list[str] = field(default_factory=list)
    confirmed: bool = False
    record: TurnRecord | None = None
    task: asyncio.Task[str] | None = None


class VoiceAgentProcessor(FrameProcessor):
    """Runs one platform agent turn per caller utterance.

    Args:
        run_turn: Calls the platform's ``GraphRunner`` and returns the reply
            text. Injected rather than constructed so this class stays a
            translator between Pipecat and the runtime, and so a test can drive
            the whole pipeline without a model. Called with a ``speak_interim``
            keyword: a coroutine that speaks a tool-round preamble immediately.
            When metrics are on it also receives ``record``, a
            :class:`TurnRecord` to stamp timings into.
        retract_last_reply: Called when a generated reply is discarded unspoken
            because the caller kept talking. The runtime flags the persisted
            reply so the follow-up turn's model does not see it — otherwise it
            reads the continuation as an answer to a question the caller never
            heard. Optional: without it the reply is merely not spoken.
        on_transcript: Notified for each user utterance and assistant reply, so
            the shell can stream a transcript to the browser.
        shared_state: Turn-phase flags shared with the turn strategies. The
            soft-speech start strategy reads ``busy`` / ``bot_speaking`` from
            here to avoid opening a turn while the bot holds the floor.
        metrics: This call's metrics collector, or ``None``.
        speculation: ``None`` (off), ``"vad"`` (speculate at VAD's pause, using
            ``turn_text``) or ``"external"`` (an STT service with its own early
            signal calls :meth:`on_early_end_of_turn` / :meth:`on_turn_resumed`).
        turn_text: The running transcript of the turn in progress.
        withdraw_speculative: Takes a missed speculation's rows out of history.
    """

    # Class-level defaults so a processor assembled field-by-field (the unit
    # tests do) behaves as "metrics and speculation off".
    _call_metrics: CallMetrics | None = None
    _speculation_mode: str | None = None
    _turn_text: UserTurnText | None = None
    _withdraw_speculative: Callable[[str], Awaitable[None]] | None = None
    _speculation: _Speculation | None = None
    _abandoning: asyncio.Task[None] | None = None
    _spec_armed: bool = False
    _spec_attempts: int = 0
    _spec_first_start: float | None = None
    _current_record: TurnRecord | None = None
    _pending_commit_at: float | None = None
    _pending_merged: int = 0
    _phone_commit_at: float | None = None

    def __init__(
        self,
        *,
        run_turn: Callable[..., Awaitable[str]],
        retract_last_reply: Callable[[], Awaitable[None]] | None = None,
        on_transcript: Callable[[str, str], Awaitable[None]] | None = None,
        shared_state: dict[str, Any] | None = None,
        metrics: CallMetrics | None = None,
        speculation: str | None = None,
        turn_text: UserTurnText | None = None,
        withdraw_speculative: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._run_turn = run_turn
        self._retract_last_reply = retract_last_reply
        self._on_transcript = on_transcript
        self._state = shared_state if shared_state is not None else {}
        self._busy = False
        self._last_user_text = ""
        self._last_user_at = 0.0
        self._pending_user_text = ""
        # Set while a preamble is being spoken; the reply waits on it so the two
        # utterances do not overlap. None when no preamble is in flight.
        self._speaking: asyncio.Event | None = None
        # Digits held back while the caller is still reciting a number.
        self._phone_buffer = ""
        self._phone_flush_task: asyncio.Task[None] | None = None
        self._call_metrics = metrics
        self._speculation_mode = speculation
        self._turn_text = turn_text
        self._withdraw_speculative = withdraw_speculative

    # ── emitting a reply ──────────────────────────────────────────────────

    async def _push_spoken_reply(self, reply: str, direction: FrameDirection) -> None:
        """Send the reply to TTS as one utterance, and report it once.

        **The reply text reaches the browser through exactly one path** — the
        ``on_transcript`` callback, which the session shell owns. An earlier
        version also pushed an ``OutputTransportMessageUrgentFrame`` here, and
        every assistant line appeared twice in the panel: the frame and the
        callback are two delivery mechanisms for one message, and the browser
        cannot tell them apart. Emitting text from the processor is the same
        mistake as pushing an ``LLMTextFrame`` alongside it.

        Deliberately does not fabricate a BotStartedSpeakingFrame: if TTS is
        then interrupted before the matching stop frame, mute strategies keyed
        on those frames leave the mic muted for the rest of the call.
        """
        self._state["last_bot_text"] = reply

        start = _GuardedResponseStart()
        start.skip_tts = True
        end = _GuardedResponseEnd()
        end.skip_tts = True
        await self.push_frame(start, direction)
        await self.push_frame(end, direction)
        await self.push_frame(
            _GuardedTTSSpeak(text=reply, append_to_context=False), direction
        )

        if self._on_transcript is not None:
            await self._on_transcript("assistant", reply)

    # ── the turn loop ─────────────────────────────────────────────────────

    async def _speak_preamble(self, text: str, direction: FrameDirection) -> None:
        """Speak a tool-round preamble now, while the tool and reply are in flight.

        "Let me check what we have today" said at ~1.5s is the difference
        between a caller who waits and one who says "hello? are you there?"
        into the silence — which then arrives as a second turn.

        Arms the speech gate before pushing, so the reply that follows waits for
        this audio to finish instead of landing on top of it. See
        :meth:`_await_preamble_spoken`.
        """
        text = (text or "").strip()
        if not text:
            return
        log.info("voice.turn.preamble", chars=len(text))
        record = self._current_record
        if record is not None and record.t_preamble_push is None:
            record.t_preamble_push = time.monotonic()
            record.preamble_text = text
        self._speaking = asyncio.Event()
        await self._push_spoken_reply(text, direction)

    async def _await_preamble_spoken(self) -> None:
        """Block until the preamble's audio has finished, then leave a beat.

        VOICE ONLY — the chat path never calls this. On a call the preamble and
        the reply share one audio channel, so they are sequential whether or not
        the code says so; without this wait they were queued back to back and
        the caller heard one run-on sentence, with the browser transcript
        printing the reply while the preamble was still being read.

        ``push_frame`` queues, it does not speak: it returns as soon as the
        frame is handed downstream, long before TTS has produced audio. The only
        honest end-of-speech signal is ``BotStoppedSpeakingFrame`` from the
        output transport, which :meth:`process_frame` routes here.

        Bounded by ``_PREAMBLE_MAX_WAIT_S``: a dropped stop frame must not strand
        the turn, so a timeout proceeds rather than raising. An interruption
        clears the gate too — if the caller talks over the preamble, waiting out
        the rest of it would delay the answer they interrupted for.
        """
        gate = self._speaking
        if gate is None:
            return
        try:
            await asyncio.wait_for(gate.wait(), timeout=_PREAMBLE_MAX_WAIT_S)
        except TimeoutError:
            log.warning("voice.turn.preamble_wait_timeout")
        finally:
            # Cleared only once the wait is over. Clearing it up front opens a
            # race: a graph that returns faster than the audio plays would null
            # the gate before the stop frame arrives, `process_frame` would find
            # nothing to set, and the caller would sit through the full timeout.
            self._speaking = None
        await asyncio.sleep(_PREAMBLE_GAP_S)

    async def _invoke_run_turn(
        self,
        user_text: str,
        *,
        speak_interim: Callable[[str], Awaitable[None]],
        record: TurnRecord | None,
    ) -> str:
        if record is None:
            return await self._run_turn(user_text, speak_interim=speak_interim) or ""
        return (
            await self._run_turn(user_text, speak_interim=speak_interim, record=record)
            or ""
        )

    def _open_record(
        self,
        user_text: str,
        *,
        t_commit: float | None,
        merged: int,
        phone: bool,
        adopt: TurnRecord | None = None,
    ) -> TurnRecord | None:
        """Start this turn's metrics row, carrying the speculation counters."""
        if self._call_metrics is None:
            return None
        record = self._call_metrics.begin_turn(
            user_text,
            t_commit=t_commit,
            utterances_merged=merged,
            phone_number_held=phone,
            record=adopt,
        )
        record.spec_attempts = self._spec_attempts
        if record.t_spec_start is None:
            record.t_spec_start = self._spec_first_start
        if adopt is None and self._spec_attempts:
            record.spec_result = "miss"
        self._spec_attempts = 0
        self._spec_first_start = None
        return record

    async def _run_one_turn(
        self,
        user_text: str,
        direction: FrameDirection,
        *,
        speculation: _Speculation | None = None,
        t_commit: float | None = None,
        merged: int = 1,
        phone: bool = False,
    ) -> str:
        self._last_user_text = user_text
        self._last_user_at = time.monotonic()
        self._state["last_user_text"] = user_text
        self._state["busy"] = True

        async def speak_interim(text: str) -> None:
            await self._speak_preamble(text, direction)

        if speculation is not None and speculation.task is not None:
            # A hit: the confirmed words are the words the agent already started
            # on. Release held writes, speak any preamble now, take its answer.
            record = self._open_record(
                user_text,
                t_commit=t_commit,
                merged=merged,
                phone=phone,
                adopt=speculation.record,
            )
            if record is not None:
                record.spec_result = "hit"
                record.t_spec_start = speculation.started_at
            self._current_record = record
            speculation.confirmed = True
            speculation.gate.set()
            if self._call_metrics is not None:
                self._call_metrics.event("spec_hit", text=speculation.text)
            log.info(
                "voice.speculation.hit",
                saved_ms=round((time.monotonic() - speculation.started_at) * 1000),
            )
            if speculation.preamble and not speculation.task.done():
                await self._speak_preamble(" ".join(speculation.preamble), direction)
            try:
                reply = (await speculation.task).strip()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("voice.speculation.failed")
                reply = ""
        else:
            await self._settle_abandoned()
            record = self._open_record(
                user_text, t_commit=t_commit, merged=merged, phone=phone
            )
            self._current_record = record
            if record is not None:
                record.t_agent_start = time.monotonic()
            log.info("voice.turn.started", text_len=len(user_text))
            reply = (
                await self._invoke_run_turn(
                    user_text, speak_interim=speak_interim, record=record
                )
            ).strip()
        if not reply:
            reply = _FALLBACK_REPLY
            if self._current_record is not None:
                self._current_record.error = "empty_or_failed_turn"
        log.info("voice.turn.replied", reply_len=len(reply))
        # Nothing to wait for unless a preamble was actually spoken this turn.
        # When one was, the reply holds until its audio ends plus a beat.
        await self._await_preamble_spoken()
        return reply

    def _take_follow_up(self, current_text: str) -> str:
        """Claim the queued continuation, unless it is an echo of this turn."""
        pending = (self._pending_user_text or "").strip()
        self._pending_user_text = ""
        if not pending or pending == current_text:
            return ""
        if (
            pending == self._last_user_text
            and (time.monotonic() - self._last_user_at) < _DUPLICATE_WINDOW_S
        ):
            return ""
        return pending

    async def _drain_turns(
        self,
        first_text: str,
        direction: FrameDirection,
        speculation: _Speculation | None = None,
        *,
        t_commit: float | None = None,
        phone: bool = False,
    ) -> None:
        """Background turn loop. Must not run on Pipecat's process-frame task.

        **Only the last round's reply is spoken.** A continuation queued while
        a reply was being generated was spoken before that reply existed as
        audio, so the caller has never heard it — it answers a thought the
        conversation has already moved past. Speaking it and then answering the
        continuation reads the same offer out twice in a row ("I can get you in
        Friday at 10:30 — may I have your name?" … "Understood — I can get you
        in Friday at 10:30 — may I have your name?"), because the follow-up
        turn's model restates whatever the unheard reply established. Dropping
        the unheard reply and speaking only the follow-up's answer gives the
        caller one response to their complete thought.
        """
        try:
            next_text = first_text
            spec = speculation
            commit_at = t_commit
            merged = 1
            while next_text:
                try:
                    reply = await self._run_one_turn(
                        next_text,
                        direction,
                        speculation=spec,
                        t_commit=commit_at,
                        merged=merged,
                        phone=phone,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A failed turn must still say something: silence is
                    # indistinguishable from a dropped call. The exception text
                    # is logged, never spoken — it can carry PII.
                    log.exception("voice.turn.failed")
                    reply = _FALLBACK_REPLY
                spec = None
                phone = False
                record = self._current_record

                follow_commit = self._pending_commit_at
                follow_merged = max(1, self._pending_merged)
                if follow_up := self._take_follow_up(next_text):
                    log.info(
                        "voice.turn.reply_superseded",
                        reply_len=len(reply),
                        follow_up_len=len(follow_up),
                    )
                    if record is not None:
                        record.superseded = True
                        record.reply_text = reply
                    self._reset_pending_marks()
                    # Before the follow-up runs, not after: the follow-up's
                    # history is loaded at its start, and the unheard reply
                    # must already be out of it by then.
                    if self._retract_last_reply is not None:
                        try:
                            await self._retract_last_reply()
                        except Exception:
                            log.exception("voice.turn.retract_failed")
                    next_text = follow_up
                    commit_at, merged = follow_commit, follow_merged
                    continue

                if record is not None:
                    record.reply_text = reply
                    record.t_reply_push = time.monotonic()
                try:
                    await self._push_spoken_reply(reply, direction)
                except Exception:
                    log.exception("voice.turn.error_reply_failed")

                # An utterance that arrived while the reply frames were being
                # queued was NOT superseded — the reply is on its way to the
                # speaker — so it gets an ordinary turn of its own.
                follow_commit = self._pending_commit_at
                follow_merged = max(1, self._pending_merged)
                next_text = self._take_follow_up(next_text)
                self._reset_pending_marks()
                commit_at, merged = follow_commit, follow_merged
        finally:
            self._busy = False
            self._state["busy"] = False
            self._current_record = None

    def _reset_pending_marks(self) -> None:
        self._pending_commit_at = None
        self._pending_merged = 0

    def _queue_while_busy(self, user_text: str) -> None:
        """Append rather than overwrite, so a two-part utterance survives."""
        self._pending_user_text = merge_utterances(self._pending_user_text, user_text)
        self._pending_commit_at = time.monotonic()
        self._pending_merged += 1
        log.info("voice.turn.queued_while_busy", text_len=len(user_text))

    def _start_turn(
        self,
        user_text: str,
        direction: FrameDirection,
        *,
        speculation: _Speculation | None = None,
        t_commit: float | None = None,
        phone: bool = False,
    ) -> None:
        self._busy = True
        self._state["busy"] = True
        self.create_task(
            self._drain_turns(
                user_text, direction, speculation, t_commit=t_commit, phone=phone
            ),
            name="voice-agent-turn",
        )

    # ── speculation ───────────────────────────────────────────────────────

    def _may_speculate(self) -> bool:
        if self._speculation_mode is None or self._busy:
            return False
        # A caller reciting a number pauses between digit groups by design; the
        # phone reassembly owns those pauses, and a guess at each would only
        # be thrown away.
        return not wants_phone_number(str(self._state.get("last_bot_text") or ""))

    def _speculate(self, text: str) -> None:
        """Start (or restart) an agent run on ``text`` before confirmation."""
        text = (text or "").strip()
        if not text or not self._may_speculate():
            return
        norm = normalize_utterance(text)
        current = self._speculation
        if current is not None and current.norm == norm:
            return
        if current is not None:
            self._abandon(current, reason="text_changed")

        spec = _Speculation(text=text, norm=norm, started_at=time.monotonic())
        if self._call_metrics is not None:
            spec.record = TurnRecord(
                index=0, user_text=text, t_commit=spec.started_at, committed_at_utc=""
            )
            spec.record.t_agent_start = spec.started_at
            self._call_metrics.event("spec_start", text=text)
        self._spec_attempts += 1
        if self._spec_first_start is None:
            self._spec_first_start = spec.started_at

        async def interim(preamble: str) -> None:
            if spec.confirmed:
                # Confirmed while the run was still going: speak it like any
                # preamble. The direction is always downstream from here.
                await self._speak_preamble(preamble, FrameDirection.DOWNSTREAM)
            else:
                spec.preamble.append(preamble)

        # Only guesses abandoned BEFORE this one may be waited on. Waiting on
        # "whatever is being undone now" deadlocked: that could be this run's
        # own undo, which is waiting for this run to end.
        prior = self._abandoning

        async def run() -> str:
            # A previous guess must finish undoing its rows first, or this
            # run's history would still contain them. `asyncio.wait` neither
            # raises the prior task's error nor passes our cancellation into it.
            if prior is not None:
                await asyncio.wait([prior])
            with side_effects_held(spec.gate):
                return await self._invoke_run_turn(
                    text, speak_interim=interim, record=spec.record
                )

        spec.task = self.create_task(run(), name="voice-speculative-turn")
        self._speculation = spec
        log.info("voice.speculation.started", text_len=len(text))

    def _abandon(self, spec: _Speculation, *, reason: str) -> None:
        """Cancel a speculation and withdraw what it wrote, in the background."""
        if self._speculation is spec:
            self._speculation = None
        if self._call_metrics is not None:
            self._call_metrics.event("spec_cancel", reason=reason, text=spec.text)
        log.info("voice.speculation.abandoned", reason=reason)
        previous = self._abandoning

        async def undo() -> None:
            if previous is not None:
                await asyncio.wait([previous])
            task = spec.task
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                # Waits for the cancelled run to unwind, including the shielded
                # write of any partial reply — which is what gets withdrawn next.
                await asyncio.wait([task])
            if self._withdraw_speculative is not None:
                try:
                    await self._withdraw_speculative(spec.text)
                except Exception:
                    log.exception("voice.speculation.withdraw_failed")

        self._abandoning = self.create_task(undo(), name="voice-speculation-undo")

    async def _settle_abandoned(self) -> None:
        """Wait until every abandoned guess has withdrawn its rows."""
        task = self._abandoning
        if task is not None:
            await asyncio.wait([task])
            if self._abandoning is task:
                self._abandoning = None

    async def on_transcript_update(self) -> None:
        """The transcript tap saw new words. Re-speculate if a pause is open."""
        if self._speculation_mode == "vad" and self._spec_armed and self._turn_text:
            self._speculate(self._turn_text.text)

    async def on_early_end_of_turn(self, transcript: str) -> None:
        """An STT service's own early end-of-turn guess (Flux EagerEndOfTurn)."""
        if self._call_metrics is not None:
            self._call_metrics.event("early_eot", text=transcript)
        if self._speculation_mode == "external":
            self._speculate(transcript)

    async def on_turn_resumed(self) -> None:
        """The caller kept talking after an early end-of-turn guess."""
        if self._call_metrics is not None:
            self._call_metrics.event("turn_resumed")
        spec = self._speculation
        if spec is not None and not spec.confirmed:
            self._abandon(spec, reason="resumed")

    # ── phone-number reassembly ───────────────────────────────────────────
    #
    # See `packages.voice.phone` for why this exists. Text only: no VAD or mute
    # behaviour is touched, so barge-in is unaffected.

    def _cancel_phone_flush(self) -> None:
        task = self._phone_flush_task
        self._phone_flush_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _flush_phone_buffer(self, direction: FrameDirection) -> None:
        """Send the buffered digits once the caller stops adding to them."""
        try:
            await asyncio.sleep(FRAGMENT_FLUSH_SECONDS)
        except asyncio.CancelledError:
            return
        self._phone_flush_task = None
        text = (self._phone_buffer or "").strip()
        self._phone_buffer = ""
        if not text:
            return
        log.info("voice.phone.flushed", digits=len(digits_in(text)))
        if self._busy:
            self._queue_while_busy(text)
        else:
            self._start_turn(
                text, direction, t_commit=self._phone_commit_at, phone=True
            )

    def _buffer_phone_fragment(self, user_text: str, direction: FrameDirection) -> None:
        """Hold a partial number, flushing early once it is complete."""
        self._phone_buffer = merge_utterances(self._phone_buffer, user_text)
        self._phone_commit_at = time.monotonic()
        digits = digits_in(self._phone_buffer)

        if is_complete_number(digits):
            # Don't make someone who finished the number wait out the timer.
            self._cancel_phone_flush()
            text = self._phone_buffer.strip()
            self._phone_buffer = ""
            log.info("voice.phone.complete", digits=len(digits))
            if self._busy:
                self._queue_while_busy(text)
            else:
                self._start_turn(
                    text, direction, t_commit=self._phone_commit_at, phone=True
                )
            return

        self._cancel_phone_flush()
        self._phone_flush_task = self.create_task(
            self._flush_phone_buffer(direction), name="voice-phone-flush"
        )

    # ── frame entry point ─────────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # End of the preamble's audio, travelling UPSTREAM from the output
        # transport (this processor sits above TTS). Releases the reply waiting
        # in `_await_preamble_spoken`. An interruption releases it too: the
        # caller who talks over the preamble wants the answer sooner, not after
        # the rest of a sentence they already cut off.
        if isinstance(frame, BotStoppedSpeakingFrame | InterruptionFrame):
            gate = self._speaking
            if gate is not None:
                gate.set()

        # Speculation on VAD's pause. The stop is the early signal; speech
        # starting again cancels whatever was started on it.
        if self._speculation_mode == "vad":
            if isinstance(frame, VADUserStoppedSpeakingFrame):
                self._spec_armed = True
                if self._turn_text is not None:
                    self._speculate(self._turn_text.text)
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                self._spec_armed = False
                spec = self._speculation
                if spec is not None and not spec.confirmed:
                    self._abandon(spec, reason="resumed")

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        t_commit = time.monotonic()
        self._spec_armed = False
        if self._turn_text is not None:
            self._turn_text.reset()
        spec = self._speculation
        self._speculation = None

        user_text = last_user_text(frame.context)
        if not user_text:
            if spec is not None:
                self._abandon(spec, reason="empty_commit")
            # Never block waiting for STT to catch up: that open cancel window
            # dropped later turns entirely.
            await self.push_frame(frame, direction)
            return

        now = time.monotonic()
        if (
            user_text == self._last_user_text
            and (now - self._last_user_at) < _DUPLICATE_WINDOW_S
        ):
            if spec is not None:
                self._abandon(spec, reason="duplicate_commit")
            log.debug("voice.turn.duplicate_skipped")
            return

        if self._on_transcript is not None:
            await self._on_transcript("user", user_text)

        # A number read out with pauses arrives as several utterances. Hold the
        # pieces together instead of answering the first one — but only while
        # the agent actually asked for a number, so ordinary speech is never
        # delayed.
        if wants_phone_number(str(self._state.get("last_bot_text") or "")):
            if looks_like_phone_fragment(user_text):
                if spec is not None:
                    self._abandon(spec, reason="phone_fragment")
                if self._busy:
                    self._queue_while_busy(user_text)
                else:
                    self._buffer_phone_fragment(user_text, direction)
                return
            if self._phone_buffer:
                # Real speech after some digits: send them together, once.
                self._cancel_phone_flush()
                user_text = merge_utterances(self._phone_buffer, user_text)
                self._phone_buffer = ""

        if self._busy:
            if spec is not None:
                self._abandon(spec, reason="busy")
            self._queue_while_busy(user_text)
            return

        if spec is not None and spec.norm != normalize_utterance(user_text):
            # The confirmed words are not the words the agent started on: the
            # caller said more after the early signal, or STT revised it.
            self._abandon(spec, reason="text_mismatch")
            spec = None

        # Return immediately so barge-in can cancel and recreate the
        # process-frame task while the graph keeps running in the background.
        self._start_turn(user_text, direction, speculation=spec, t_commit=t_commit)
