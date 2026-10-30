"""Benchmark the voice end-of-turn detectors without making calls by hand.

Each run streams caller audio in real time through the real voice pipeline:
Silero VAD, the chosen detector, Deepgram speech-to-text and text-to-speech.
Only the agent is a stand-in, a fixed ~0.4s reply, so the numbers measure
turn-taking and audio rather than the LLM. Every agent turn becomes one row of
the same ``turns.csv`` that live calls write, so `voice_metrics_report.py`
reads both.

Caller audio comes from one of:
  * ``--audio DIR``: your own recordings (.wav). Each file is one caller turn.
    Files play in name order with ``--gap`` seconds of silence between them.
  * ``--sentences FILE``: one sentence per line, spoken by Deepgram TTS.
  * neither: three built-in booking sentences.

Examples:
  python tools/voice_turn_bench.py
  python tools/voice_turn_bench.py --detectors smart_turn,flux --spec on --repeat 3
  python tools/voice_turn_bench.py --audio ~/recordings/caller1 --out var/voice_metrics/bench
  python tools/voice_turn_bench.py --report

Needs DEEPGRAM_API_KEY in .env. Each configuration takes about as long as the
audio itself, because audio is fed in real time.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SR = 16_000
CHUNK_BYTES = SR * 2 // 50  # 20 ms of 16-bit mono
DEFAULT_SENTENCES = [
    "Hi, I would like to book a teeth cleaning for Thursday morning.",
    "My name is Priya Sharma.",
    "Actually, can we make it, um, Friday after lunch instead?",
]
DETECTORS = ("silence", "smart_turn", "vap", "flux")


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _tts(text: str, key: str, cache: Path) -> bytes:
    """Deepgram TTS as raw 16 kHz mono PCM, cached by sentence."""
    import httpx

    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha256(text.encode()).hexdigest()[:16] + ".pcm")
    if not path.exists():
        r = httpx.post(
            "https://api.deepgram.com/v1/speak",
            params={
                "model": "aura-2-thalia-en",
                "encoding": "linear16",
                "sample_rate": SR,
                "container": "none",
            },
            headers={"Authorization": f"Token {key}"},
            json={"text": text},
            timeout=30,
        )
        r.raise_for_status()
        path.write_bytes(r.content)
    return path.read_bytes()


def _read_wav(path: Path) -> bytes:
    """Any 16-bit PCM wav as 16 kHz mono (channels averaged, linear resample)."""
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: only 16-bit PCM wav is supported")
        rate, channels = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = data.reshape(-1, channels).mean(axis=1)
    if rate != SR:
        n = int(len(audio) * SR / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    return audio.astype(np.int16).tobytes()


def _utterances(args: argparse.Namespace, key: str) -> list[bytes]:
    if args.audio:
        files = sorted(Path(args.audio).expanduser().glob("*.wav"))
        if not files:
            raise SystemExit(f"no .wav files in {args.audio}")
        return [_read_wav(f) for f in files]
    lines = DEFAULT_SENTENCES
    if args.sentences:
        lines = [s.strip() for s in Path(args.sentences).read_text().splitlines() if s.strip()]
    return [_tts(s, key, ROOT / "var" / "voice_bench_cache") for s in lines]


async def _run_one(
    detector: str,
    speculative: bool,
    utterances: list[bytes],
    args: argparse.Namespace,
    key: str,
) -> list[str]:
    from packages.providers.voice.metrics_observer import MetricsClock, VoiceMetricsObserver
    from packages.providers.voice.pipecat_composed import (
        build_composed_pipeline,
        detector_turn_setup,
        detector_unavailable_reason,
    )
    from packages.voice.call_metrics import CallMeta, CallMetrics
    from packages.voice.engine.config import TenantVoiceConfig
    from packages.voice.turn import TurnDetectionConfig, TurnPolicy
    from pipecat.frames.frames import EndFrame, InputAudioRawFrame, StartFrame
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport, TransportParams
    from pipecat.workers.runner import WorkerRunner

    td = TurnDetectionConfig.parse(detector, speculative=speculative)
    if (reason := detector_unavailable_reason(td)) is not None:
        print(f"  skipped {td.label}: {reason}")
        return []
    if td.detector.value == "vap":
        from packages.providers.voice.vap_turn import load_vap_model

        await asyncio.to_thread(load_vap_model, Path(td.vap_model_dir).expanduser())  # noqa: ASYNC240 - one stat at setup

    cfg = TenantVoiceConfig(turn_detection=td)
    setup = detector_turn_setup(cfg, TurnPolicy.from_stop_secs(cfg.stop_secs), shared_state={})
    meta = CallMeta(
        call_id=str(uuid.uuid4()),
        chat_id=f"bench-{uuid.uuid4().hex[:8]}",
        tenant_id="bench",
        config_label=td.label,
        detector=str(td.detector),
        speculative=speculative,
        stop_secs=cfg.stop_secs,
        vad_stop_secs=setup.vad.stop_secs,
        max_silence_secs=td.max_silence_secs,
        vap_threshold=td.vap_threshold if detector == "vap" else None,
        flux_model=td.flux_model if detector == "flux" else None,
        stt_model=cfg.stt_model,
        tts_voice=cfg.tts_voice,
        llm_provider="bench-stub",
        llm_model="fixed-reply",
    )
    metrics = CallMetrics(meta, directory=Path(args.out))
    heard: list[str] = []

    async def run_turn(text: str, *, speak_interim=None, record=None) -> str:  # type: ignore[no-untyped-def]
        heard.append(text)
        await asyncio.sleep(0.25)  # stand-in for time to first token
        if record is not None:
            record.t_first_token = record.t_first_reply_token = time.monotonic()
        await asyncio.sleep(0.15)
        if record is not None:
            record.t_generation_end = time.monotonic()
            record.agent_key, record.outcome = "bench", "ok"
        return "Sure, let me check that for you."

    async def withdraw(text: str) -> None:
        heard.append(f"(withdrawn) {text}")

    script: list[bytes | float] = [1.0]
    for u in utterances:
        script += [u, args.gap]

    class _In(BaseInputTransport):
        async def start(self, frame: StartFrame) -> None:
            await super().start(frame)
            await self.set_transport_ready(frame)
            self.create_task(self._feed())

        async def _feed(self) -> None:
            for item in script:
                data = item if isinstance(item, bytes) else b"\0" * (int(SR * item) * 2)
                for off in range(0, len(data), CHUNK_BYTES):
                    chunk = data[off : off + CHUNK_BYTES].ljust(CHUNK_BYTES, b"\0")
                    await self.push_audio_frame(
                        InputAudioRawFrame(audio=chunk, sample_rate=SR, num_channels=1)
                    )
                    await asyncio.sleep(0.02)

    class _Out(BaseOutputTransport):
        async def start(self, frame: StartFrame) -> None:
            await super().start(frame)
            await self.set_transport_ready(frame)

        async def write_audio_frame(self, frame) -> bool:  # type: ignore[no-untyped-def]
            await asyncio.sleep(len(frame.audio) / (frame.sample_rate * 2 * frame.num_channels))
            return True

    class _Transport(BaseTransport):
        def __init__(self) -> None:
            super().__init__()
            p = TransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=SR,
                audio_out_enabled=True,
                audio_out_sample_rate=24_000,
            )
            self._in, self._out = _In(p), _Out(p)

        def input(self):  # type: ignore[no-untyped-def]
            return self._in

        def output(self):  # type: ignore[no-untyped-def]
            return self._out

    pipeline = build_composed_pipeline(
        _Transport(),
        cfg=cfg,
        run_turn=run_turn,
        api_key=key,
        metrics=metrics,
        withdraw_speculative=withdraw,
    )
    clock = MetricsClock()
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(),
        clock=clock,
        observers=[VoiceMetricsObserver(metrics, clock)],
        cancel_on_idle_timeout=False,
    )
    total = sum(len(s) / (SR * 2) if isinstance(s, bytes) else s for s in script)

    async def stop() -> None:
        await asyncio.sleep(total + 2.0)
        await worker.queue_frame(EndFrame())

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    try:
        await asyncio.gather(runner.run(), stop())
    finally:
        metrics.close()
    return heard


async def _main(args: argparse.Namespace) -> None:
    key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not key:
        raise SystemExit("DEEPGRAM_API_KEY is not set (.env)")
    detectors = [d.strip() for d in args.detectors.split(",") if d.strip()]
    for d in detectors:
        if d not in DETECTORS:
            raise SystemExit(f"unknown detector {d!r}; choose from {', '.join(DETECTORS)}")
    specs = {"off": [False], "on": [True], "both": [False, True]}[args.spec]
    utterances = _utterances(args, key)
    print(f"{len(utterances)} caller turns per run; writing to {args.out}/turns.csv")

    for rep in range(1, args.repeat + 1):
        for d in detectors:
            for s in specs:
                label = d + ("+spec" if s else "")
                print(f"[run {rep}/{args.repeat}] {label} ...", flush=True)
                try:
                    heard = await _run_one(d, s, utterances, args, key)
                except Exception as exc:
                    print(f"  FAILED: {exc!r}")
                    continue
                for h in heard:
                    print(f"  agent got: {h}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--detectors",
        default="silence,smart_turn,flux",
        help="comma list from: " + ", ".join(DETECTORS),
    )
    parser.add_argument(
        "--spec",
        choices=("off", "on", "both"),
        default="both",
        help="speculative start off, on, or each config both ways",
    )
    parser.add_argument("--repeat", type=int, default=1, help="runs per configuration")
    parser.add_argument("--audio", help="folder of .wav caller turns (your recordings)")
    parser.add_argument("--sentences", help="text file, one caller sentence per line")
    parser.add_argument(
        "--gap",
        type=float,
        default=5.0,
        help="seconds of silence after each caller turn (room for the reply)",
    )
    parser.add_argument(
        "--out",
        default="var/voice_metrics/bench",
        help="where turns.csv and events/ go (kept apart from live calls)",
    )
    parser.add_argument("--report", action="store_true", help="print the summary table after")
    args = parser.parse_args()

    _load_env()
    import logging

    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logging.getLogger().setLevel(logging.WARNING)

    asyncio.run(_main(args))
    if args.report:
        import subprocess

        subprocess.run(  # noqa: S603 - our own script, fixed argv
            [
                sys.executable,
                str(ROOT / "tools" / "voice_metrics_report.py"),
                str(Path(args.out) / "turns.csv"),
            ],
            check=False,
        )


if __name__ == "__main__":
    main()
