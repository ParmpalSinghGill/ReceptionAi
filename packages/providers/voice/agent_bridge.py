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
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from packages.voice.phone import (
    FRAGMENT_FLUSH_SECONDS,
    digits_in,
    is_complete_number,
    looks_like_phone_fragment,
    wants_phone_number,
)
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSSpeakFrame,
    UninterruptibleFrame,
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


class VoiceAgentProcessor(FrameProcessor):
    """Runs one platform agent turn per caller utterance.

    Args:
        run_turn: Calls the platform's ``GraphRunner`` and returns the reply
            text. Injected rather than constructed so this class stays a
            translator between Pipecat and the runtime, and so a test can drive
            the whole pipeline without a model.
        on_transcript: Notified for each user utterance and assistant reply, so
            the shell can stream a transcript to the browser.
        shared_state: Turn-phase flags shared with the turn strategies. The
            soft-speech start strategy reads ``busy`` / ``bot_speaking`` from
            here to avoid opening a turn while the bot holds the floor.
    """

    def __init__(
        self,
        *,
        run_turn: Callable[[str], Awaitable[str]],
        on_transcript: Callable[[str, str], Awaitable[None]] | None = None,
        shared_state: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._run_turn = run_turn
        self._on_transcript = on_transcript
        self._state = shared_state if shared_state is not None else {}
        self._busy = False
        self._last_user_text = ""
        self._last_user_at = 0.0
        self._pending_user_text = ""
        # Digits held back while the caller is still reciting a number.
        self._phone_buffer = ""
        self._phone_flush_task: asyncio.Task[None] | None = None

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

    async def _run_one_turn(self, user_text: str, direction: FrameDirection) -> None:
        self._last_user_text = user_text
        self._last_user_at = time.monotonic()
        self._state["last_user_text"] = user_text
        self._state["busy"] = True

        log.info("voice.turn.started", text_len=len(user_text))
        reply = (await self._run_turn(user_text) or "").strip()
        if not reply:
            reply = _FALLBACK_REPLY
        log.info("voice.turn.replied", reply_len=len(reply))
        await self._push_spoken_reply(reply, direction)

    async def _drain_turns(self, first_text: str, direction: FrameDirection) -> None:
        """Background turn loop. Must not run on Pipecat's process-frame task."""
        try:
            next_text = first_text
            while next_text:
                try:
                    await self._run_one_turn(next_text, direction)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A failed turn must still say something: silence is
                    # indistinguishable from a dropped call. The exception text
                    # is logged, never spoken — it can carry PII.
                    log.exception("voice.turn.failed")
                    try:
                        await self._push_spoken_reply(_FALLBACK_REPLY, direction)
                    except Exception:
                        log.exception("voice.turn.error_reply_failed")

                pending = (self._pending_user_text or "").strip()
                self._pending_user_text = ""
                if not pending or pending == next_text:
                    break
                if (
                    pending == self._last_user_text
                    and (time.monotonic() - self._last_user_at) < _DUPLICATE_WINDOW_S
                ):
                    break
                next_text = pending
        finally:
            self._busy = False
            self._state["busy"] = False

    def _queue_while_busy(self, user_text: str) -> None:
        """Append rather than overwrite, so a two-part utterance survives."""
        self._pending_user_text = merge_utterances(self._pending_user_text, user_text)
        log.info("voice.turn.queued_while_busy", text_len=len(user_text))

    def _start_turn(self, user_text: str, direction: FrameDirection) -> None:
        self._busy = True
        self._state["busy"] = True
        self.create_task(self._drain_turns(user_text, direction), name="voice-agent-turn")

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
            self._start_turn(text, direction)

    def _buffer_phone_fragment(self, user_text: str, direction: FrameDirection) -> None:
        """Hold a partial number, flushing early once it is complete."""
        self._phone_buffer = merge_utterances(self._phone_buffer, user_text)
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
                self._start_turn(text, direction)
            return

        self._cancel_phone_flush()
        self._phone_flush_task = self.create_task(
            self._flush_phone_buffer(direction), name="voice-phone-flush"
        )

    # ── frame entry point ─────────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        user_text = last_user_text(frame.context)
        if not user_text:
            # Never block waiting for STT to catch up: that open cancel window
            # dropped later turns entirely.
            await self.push_frame(frame, direction)
            return

        now = time.monotonic()
        if (
            user_text == self._last_user_text
            and (now - self._last_user_at) < _DUPLICATE_WINDOW_S
        ):
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
            self._queue_while_busy(user_text)
            return

        # Return immediately so barge-in can cancel and recreate the
        # process-frame task while the graph keeps running in the background.
        self._start_turn(user_text, direction)
