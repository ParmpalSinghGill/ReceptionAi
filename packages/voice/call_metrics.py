"""Per-turn latency and turn-taking metrics for voice calls, written as a table.

One row per agent turn ("message"), keyed by ``call_id`` + ``chat_id`` +
``turn_index``, appended to ``turns.csv`` — load it with
``pandas.read_csv(...)`` and every column is ready to group by detector. Each
call's raw, timestamped event log goes to ``events/<call_id>.jsonl`` beside it,
so any number not precomputed here can still be derived later.

**The anchor is the caller's last word.** Every ``*_ms`` latency column is
measured from ``speech_end``: the moment Silero VAD last heard speech before the
turn was committed (its stop event minus its stop window). Silero runs under all
four detectors precisely so this anchor is the same instrument for each of them
— a comparison where each method timed itself would compare clocks, not methods.

**Rows are finalised late, on purpose.** Pipeline events reach this module
through Pipecat's observer queue, slightly after they happen; and some facts
about a turn (when its audio finished, whether the caller spoke again before
hearing it) only exist after the turn is over. So a row is assembled when the
*next* turn begins, or when the call ends, from the event log — never at the
moment the agent starts, when half its columns would still be unknown.

Vendor-free: nothing here imports Pipecat. The adapter that feeds frames in
lives in :mod:`packages.providers.voice.metrics_observer`.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

__all__ = ["COLUMNS", "CallMeta", "CallMetrics", "TurnRecord", "default_metrics_dir"]

log = structlog.get_logger(__name__)

#: Column order of ``turns.csv``. Append-only: adding a column rotates the old
#: file aside rather than writing rows under the wrong header.
COLUMNS: tuple[str, ...] = (
    # identity
    "call_id", "chat_id", "turn_index", "tenant_id", "committed_at_utc",
    # configuration under test
    "config_label", "detector", "speculative", "stop_secs", "vad_stop_secs",
    "max_silence_secs", "vap_threshold", "flux_model", "stt_model", "tts_voice",
    "llm_provider", "llm_model",
    # what was said
    "user_text", "user_words", "reply_text", "reply_chars", "preamble_text",
    "utterances_merged", "phone_number_held",
    # caller speech, measured by VAD
    "user_speech_ms", "speech_end_source",
    # latencies from the caller's last word (ms)
    "stt_final_ms", "endpoint_ms", "agent_start_ms", "first_token_ms",
    "first_reply_token_ms", "generation_end_ms", "preamble_audio_ms",
    "first_audio_ms", "reply_audio_ms", "reply_audio_end_ms",
    # durations (ms)
    "generation_ms", "graph_latency_ms", "tts_ttfb_ms", "reply_audio_duration_ms",
    # model work
    "prompt_tokens", "completion_tokens", "cached_tokens", "tool_calls",
    "tool_names", "agent_key", "finish_reason", "outcome",
    # detector internals
    "detector_probability", "detector_inference_ms", "detector_calls",
    # speculation
    "spec_attempts", "spec_result", "spec_start_ms", "spec_saved_ms",
    # turn-taking quality
    "superseded", "user_resumed_before_reply", "resume_gap_ms",
    "barge_in", "error",
)  # fmt: skip


def default_metrics_dir() -> Path:
    """``$VOICE_METRICS_DIR``, else ``var/voice_metrics`` under the working dir."""
    return Path(os.getenv("VOICE_METRICS_DIR", "var/voice_metrics")).expanduser()


@dataclass(frozen=True, slots=True)
class CallMeta:
    """What is fixed for the whole call and repeated on every row."""

    call_id: str
    chat_id: str
    tenant_id: str
    config_label: str
    detector: str
    speculative: bool
    stop_secs: float
    vad_stop_secs: float
    max_silence_secs: float | None = None
    vap_threshold: float | None = None
    flux_model: str | None = None
    stt_model: str | None = None
    tts_voice: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None


@dataclass(slots=True)
class TurnRecord:
    """One agent turn as it happens. Times are ``time.monotonic()`` seconds."""

    index: int
    user_text: str
    t_commit: float
    committed_at_utc: str
    utterances_merged: int = 1
    phone_number_held: bool = False
    t_agent_start: float | None = None
    t_first_token: float | None = None
    t_first_reply_token: float | None = None
    t_generation_end: float | None = None
    t_preamble_push: float | None = None
    t_reply_push: float | None = None
    preamble_text: str = ""
    reply_text: str = ""
    superseded: bool = False
    error: str = ""
    graph_latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    tool_names: list[str] = field(default_factory=list)
    agent_key: str | None = None
    finish_reason: str | None = None
    outcome: str | None = None
    spec_attempts: int = 0
    spec_result: str = "none"
    t_spec_start: float | None = None
    finalized: bool = False


class CallMetrics:
    """Collects one call's events and writes its rows.

    Thread-safe for the two writers it has: the pipeline observer task and the
    agent bridge, both on the event loop, plus file writes guarded by a lock so
    two calls ending together do not interleave lines in the shared CSV.
    """

    _file_lock = threading.Lock()

    def __init__(self, meta: CallMeta, *, directory: Path | None = None) -> None:
        self.meta = meta
        self._dir = directory or default_metrics_dir()
        self._events: list[tuple[float, str, dict[str, Any]]] = []
        self._turns: list[TurnRecord] = []
        self._t_start = time.monotonic()
        self._closed = False
        self.event("call_start", t=self._t_start, **{"config": meta.config_label})

    # ── inputs ────────────────────────────────────────────────────────────

    def event(self, kind: str, *, t: float | None = None, **data: Any) -> None:
        """Record a timestamped event. ``t`` defaults to now."""
        if self._closed:
            return
        self._events.append((time.monotonic() if t is None else t, kind, data))

    def begin_turn(
        self,
        user_text: str,
        *,
        t_commit: float | None = None,
        utterances_merged: int = 1,
        phone_number_held: bool = False,
        record: TurnRecord | None = None,
    ) -> TurnRecord:
        """Open a row for an agent turn, finalising every earlier one.

        ``record`` adopts a row that already has timings in it — a speculative
        run that started before this commit and turned out to be right.
        """
        t = time.monotonic() if t_commit is None else t_commit
        for prev in self._turns:
            if not prev.finalized:
                self._finalize(prev, until=t)
        turn = record or TurnRecord(
            index=0, user_text=user_text, t_commit=t, committed_at_utc=""
        )
        turn.index = len(self._turns) + 1
        turn.user_text = user_text
        turn.t_commit = t
        turn.committed_at_utc = datetime.now(tz=UTC).isoformat(timespec="milliseconds")
        turn.utterances_merged = utterances_merged
        turn.phone_number_held = phone_number_held
        self._turns.append(turn)
        self.event("commit", t=t, index=turn.index, text=user_text)
        return turn

    def close(self) -> None:
        """Finalise outstanding rows and write the event log. Idempotent."""
        if self._closed:
            return
        now = time.monotonic()
        self.event("call_end", t=now)
        for turn in self._turns:
            if not turn.finalized:
                self._finalize(turn, until=now)
        self._closed = True
        self._write_events()

    # ── derivation ────────────────────────────────────────────────────────

    def _window(self, kind: str, lo: float, hi: float) -> list[tuple[float, dict[str, Any]]]:
        return [(t, d) for (t, k, d) in self._events if k == kind and lo < t <= hi]

    def _finalize(self, turn: TurnRecord, *, until: float) -> None:
        """Build the row for ``turn`` from the event log, then write it."""
        turn.finalized = True
        prev_commit = self._prev_commit(turn)

        # Caller speech: the VAD events between the previous commit and this one.
        starts = self._window("vad_start", prev_commit, turn.t_commit)
        stops = self._window("vad_stop", prev_commit, turn.t_commit + 0.05)
        speech_end: float | None = None
        source = ""
        if stops:
            t_stop, data = stops[-1]
            speech_end = t_stop - float(data.get("stop_secs") or 0.0)
            source = "vad"
        finals = self._window("transcript_final", prev_commit, turn.t_commit + 0.05)
        if speech_end is None and finals:
            speech_end, source = finals[-1][0], "transcript"
        if speech_end is None:
            speech_end, source = turn.t_commit, "commit"
        speech_start = starts[0][0] if starts else None

        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - speech_end) * 1000)

        detected = self._window("user_turn_stop", prev_commit, turn.t_commit + 0.05)
        t_detect = detected[-1][0] if detected else turn.t_commit

        detector = self._window("turn_analysis", prev_commit, turn.t_commit + 0.05)
        prob = detector[-1][1].get("probability") if detector else None
        infer = detector[-1][1].get("inference_ms") if detector else None

        # Bot audio after this commit and before the next turn.
        bot_starts = self._window("bot_start", turn.t_commit, until)
        first_audio = bot_starts[0][0] if bot_starts else None
        preamble_audio = None
        reply_audio = None
        if turn.t_preamble_push is not None:
            preamble_audio = next((t for t, _ in bot_starts if t >= turn.t_preamble_push), None)
        if turn.t_reply_push is not None:
            reply_audio = next((t for t, _ in bot_starts if t >= turn.t_reply_push), None)
        reply_end = None
        if reply_audio is not None:
            reply_end = next((t for t, _ in self._window("bot_stop", reply_audio, until)), None)

        # Did the caller start talking again before hearing anything back? The
        # signature of a detector that ended the turn too early.
        heard_at = first_audio if first_audio is not None else until
        resumed = self._window("vad_start", turn.t_commit, heard_at)
        resume_gap = round((resumed[0][0] - turn.t_commit) * 1000) if resumed else None
        barge = bool(
            reply_audio is not None
            and self._window("interruption", reply_audio, reply_end if reply_end else until)
        )

        spec_saved = None
        if turn.spec_result == "hit" and turn.t_spec_start is not None:
            spec_saved = round((turn.t_commit - turn.t_spec_start) * 1000)

        tts_ttfb_events = self._window("tts_ttfb", turn.t_commit, until)
        row: dict[str, Any] = {
            "call_id": self.meta.call_id,
            "chat_id": self.meta.chat_id,
            "turn_index": turn.index,
            "tenant_id": self.meta.tenant_id,
            "committed_at_utc": turn.committed_at_utc,
            "config_label": self.meta.config_label,
            "detector": self.meta.detector,
            "speculative": self.meta.speculative,
            "stop_secs": self.meta.stop_secs,
            "vad_stop_secs": self.meta.vad_stop_secs,
            "max_silence_secs": self.meta.max_silence_secs,
            "vap_threshold": self.meta.vap_threshold,
            "flux_model": self.meta.flux_model,
            "stt_model": self.meta.stt_model,
            "tts_voice": self.meta.tts_voice,
            "llm_provider": self.meta.llm_provider,
            "llm_model": self.meta.llm_model,
            "user_text": turn.user_text,
            "user_words": len(turn.user_text.split()),
            "reply_text": turn.reply_text,
            "reply_chars": len(turn.reply_text),
            "preamble_text": turn.preamble_text,
            "utterances_merged": turn.utterances_merged,
            "phone_number_held": turn.phone_number_held,
            "user_speech_ms": (
                round((speech_end - speech_start) * 1000)
                if speech_start is not None and speech_end >= speech_start
                else None
            ),
            "speech_end_source": source,
            "stt_final_ms": ms(finals[-1][0]) if finals else None,
            "endpoint_ms": ms(t_detect),
            "agent_start_ms": ms(turn.t_agent_start),
            "first_token_ms": ms(turn.t_first_token),
            "first_reply_token_ms": ms(turn.t_first_reply_token),
            "generation_end_ms": ms(turn.t_generation_end),
            "preamble_audio_ms": ms(preamble_audio),
            "first_audio_ms": ms(first_audio),
            "reply_audio_ms": ms(reply_audio),
            "reply_audio_end_ms": ms(reply_end),
            "generation_ms": (
                round((turn.t_generation_end - turn.t_agent_start) * 1000)
                if turn.t_generation_end and turn.t_agent_start
                else None
            ),
            "graph_latency_ms": turn.graph_latency_ms,
            "tts_ttfb_ms": (
                round((reply_audio - turn.t_reply_push) * 1000)
                if reply_audio is not None and turn.t_reply_push is not None
                else (tts_ttfb_events[0][1].get("ms") if tts_ttfb_events else None)
            ),
            "reply_audio_duration_ms": (
                round((reply_end - reply_audio) * 1000)
                if reply_end is not None and reply_audio is not None
                else None
            ),
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
            "cached_tokens": turn.cached_tokens,
            "tool_calls": len(turn.tool_names),
            "tool_names": "|".join(turn.tool_names),
            "agent_key": turn.agent_key,
            "finish_reason": turn.finish_reason,
            "outcome": turn.outcome,
            "detector_probability": prob,
            "detector_inference_ms": infer,
            "detector_calls": len(detector),
            "spec_attempts": turn.spec_attempts,
            "spec_result": turn.spec_result,
            "spec_start_ms": ms(turn.t_spec_start),
            "spec_saved_ms": spec_saved,
            "superseded": turn.superseded,
            "user_resumed_before_reply": bool(resumed),
            "resume_gap_ms": resume_gap,
            "barge_in": barge,
            "error": turn.error,
        }
        self._write_row(row)

    def _prev_commit(self, turn: TurnRecord) -> float:
        i = turn.index - 2
        return self._turns[i].t_commit if i >= 0 else self._t_start

    # ── output ────────────────────────────────────────────────────────────

    @property
    def csv_path(self) -> Path:
        return self._dir / "turns.csv"

    def _write_row(self, row: dict[str, Any]) -> None:
        try:
            with self._file_lock:
                self._dir.mkdir(parents=True, exist_ok=True)
                path = self.csv_path
                if path.exists():
                    with path.open(newline="", encoding="utf-8") as fh:
                        header = next(csv.reader(fh), None)
                    if header is not None and tuple(header) != COLUMNS:
                        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
                        path.rename(path.with_name(f"turns.{stamp}.csv"))
                new = not path.exists()
                with path.open("a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=COLUMNS)
                    if new:
                        writer.writeheader()
                    writer.writerow({k: _cell(row.get(k)) for k in COLUMNS})
        except Exception:
            # Metrics must never cost a call. Losing a row is logged, loudly.
            log.exception("voice.metrics.row_write_failed", call_id=self.meta.call_id)

    def _write_events(self) -> None:
        try:
            with self._file_lock:
                events_dir = self._dir / "events"
                events_dir.mkdir(parents=True, exist_ok=True)
                path = events_dir / f"{self.meta.call_id}.jsonl"
                with path.open("w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"kind": "meta", **_meta_dict(self.meta)}) + "\n")
                    for t, kind, data in self._events:
                        rel = round((t - self._t_start) * 1000, 1)
                        fh.write(
                            json.dumps({"t_ms": rel, "kind": kind, **data}, default=str) + "\n"
                        )
        except Exception:
            log.exception("voice.metrics.events_write_failed", call_id=self.meta.call_id)


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return round(value, 4)
    return value


def _meta_dict(meta: CallMeta) -> dict[str, Any]:
    return {k: getattr(meta, k) for k in meta.__dataclass_fields__}
