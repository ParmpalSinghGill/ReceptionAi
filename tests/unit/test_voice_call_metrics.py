"""The per-turn metrics row is derived correctly from a call's event timeline.

Built from a synthetic timeline with known times, so every expected number is
arithmetic on the test's own inputs. The anchor is the caller's last word: VAD
stop minus its stop window.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pytest
from packages.voice.call_metrics import COLUMNS, CallMeta, CallMetrics


def _meta() -> CallMeta:
    return CallMeta(
        call_id="call-1",
        chat_id="chat-1",
        tenant_id="tenant-1",
        config_label="smart_turn+spec",
        detector="smart_turn",
        speculative=True,
        stop_secs=0.5,
        vad_stop_secs=0.2,
    )


def _rows(directory: Path) -> list[dict[str, str]]:
    with (directory / "turns.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_latencies_are_measured_from_the_last_word(tmp_path: Path) -> None:
    t0 = time.monotonic() - 60.0
    m = CallMetrics(_meta(), directory=tmp_path)
    m._t_start = (
        t0  # the call began when the timeline did  # a timeline that has already happened
    )
    m.event("vad_start", t=t0 + 1.0)
    m.event("vad_stop", t=t0 + 3.2, stop_secs=0.2)  # last word at t0+3.0
    m.event("turn_analysis", t=t0 + 3.25, probability=0.91, inference_ms=12.0)
    m.event("transcript_final", t=t0 + 3.3, text="book thursday")
    m.event("user_turn_stop", t=t0 + 3.35)

    turn = m.begin_turn("book thursday", t_commit=t0 + 3.36)
    turn.t_agent_start = t0 + 3.37
    turn.t_first_token = t0 + 4.0
    turn.t_first_reply_token = t0 + 4.2
    turn.t_generation_end = t0 + 5.0
    turn.t_reply_push = t0 + 5.01
    turn.reply_text = "Thursday at ten is free."
    turn.tool_names = ["booking.check_availability"]
    m.event("bot_start", t=t0 + 5.3)
    m.event("bot_stop", t=t0 + 7.3)
    m.close()

    (row,) = _rows(tmp_path)
    assert tuple(row) == COLUMNS
    assert row["chat_id"] == "chat-1" and row["turn_index"] == "1"
    assert row["speech_end_source"] == "vad"
    assert row["user_speech_ms"] == "2000"
    assert row["endpoint_ms"] == "350"
    assert row["stt_final_ms"] == "300"
    assert row["first_token_ms"] == "1000"
    assert row["first_reply_token_ms"] == "1200"
    assert row["generation_end_ms"] == "2000"
    assert row["first_audio_ms"] == "2300"
    assert row["reply_audio_ms"] == "2300"
    assert row["reply_audio_end_ms"] == "4300"
    assert row["tts_ttfb_ms"] == "290"
    assert row["detector_probability"] == "0.91"
    assert row["tool_calls"] == "1"
    assert row["user_resumed_before_reply"] == "0"


def test_turns_are_indexed_in_order_and_resumes_are_flagged(tmp_path: Path) -> None:
    t0 = time.monotonic() - 60.0
    m = CallMetrics(_meta(), directory=tmp_path)
    m._t_start = (
        t0  # the call began when the timeline did  # a timeline that has already happened
    )
    m.event("vad_stop", t=t0 + 1.2, stop_secs=0.2)
    first = m.begin_turn("it can be a general checkup", t_commit=t0 + 1.3)
    # The caller carries on before hearing anything: an early end of turn.
    m.event("vad_start", t=t0 + 1.8)
    m.event("vad_stop", t=t0 + 2.6, stop_secs=0.2)
    first.superseded = True
    m.begin_turn("no emergency", t_commit=t0 + 2.7)
    m.close()

    rows = _rows(tmp_path)
    assert [r["turn_index"] for r in rows] == ["1", "2"]
    assert rows[0]["user_resumed_before_reply"] == "1"
    assert rows[0]["resume_gap_ms"] == "500"
    assert rows[0]["superseded"] == "1"
    assert rows[1]["endpoint_ms"] == "300"


def test_the_event_log_is_written_per_call(tmp_path: Path) -> None:
    m = CallMetrics(_meta(), directory=tmp_path)
    m.event("vad_start")
    m.close()
    lines = (tmp_path / "events" / "call-1.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["kind"] == "meta"
    assert any(json.loads(line)["kind"] == "vad_start" for line in lines[1:])


def test_a_speculative_hit_records_the_saving(tmp_path: Path) -> None:
    from packages.voice.call_metrics import TurnRecord

    t0 = time.monotonic() - 60.0
    m = CallMetrics(_meta(), directory=tmp_path)
    m._t_start = (
        t0  # the call began when the timeline did  # a timeline that has already happened
    )
    m.event("vad_stop", t=t0 + 1.2, stop_secs=0.2)
    spec = TurnRecord(index=0, user_text="x", t_commit=t0 + 1.25, committed_at_utc="")
    spec.t_agent_start = t0 + 1.25
    turn = m.begin_turn("x", t_commit=t0 + 2.0, record=spec)
    turn.spec_result = "hit"
    turn.t_spec_start = t0 + 1.25
    m.close()
    (row,) = _rows(tmp_path)
    assert row["spec_result"] == "hit"
    assert row["spec_saved_ms"] == "750"
    assert row["agent_start_ms"] == "250"


def test_a_broadcast_frame_is_one_event(tmp_path: Path) -> None:
    """VAD/turn/bot frames travel both ways as two frames; log them once."""
    pytest.importorskip("pipecat")
    import asyncio

    from packages.providers.voice.metrics_observer import (
        MetricsClock,
        VoiceMetricsObserver,
    )
    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection

    m = CallMetrics(_meta(), directory=tmp_path)
    observer = VoiceMetricsObserver(m, MetricsClock())  # type: ignore[no-untyped-call]
    down, up = VADUserStartedSpeakingFrame(), VADUserStartedSpeakingFrame()
    down.broadcast_sibling_id, up.broadcast_sibling_id = up.id, down.id

    async def push_both() -> None:
        pushes = ((down, FrameDirection.DOWNSTREAM), (up, FrameDirection.UPSTREAM))
        for frame, direction in pushes:
            await observer.on_push_frame(
                FramePushed(
                    source=None,  # type: ignore[arg-type]
                    destination=None,  # type: ignore[arg-type]
                    frame=frame,
                    direction=direction,
                    timestamp=0,
                )
            )

    asyncio.run(push_both())
    assert sum(1 for e in m._events if e[1] == "vad_start") == 1
