"""Voice Activity Projection as a Pipecat end-of-turn analyzer.

VAP (Ekstedt & Skantze, Interspeech 2022) is a stereo model: one channel per
speaker, and at every 20ms frame it predicts who will be speaking over the next
two seconds. At a pause in the caller's speech, a high probability that the
*other* channel (the agent) speaks next is a predicted turn shift — the caller
has finished. A high probability that the caller continues is a hold.

**Built on Pipecat's Smart Turn base class, deliberately.** Everything around
the prediction — buffering audio since speech start, asking at each VAD pause,
the maximum-silence fallback, running inference off the event loop — is the
same for any "audio in, finished-or-not out" model. Reusing it means VAP and
Smart Turn differ in exactly one method, which is what a fair comparison
between them needs.

**The agent channel is silence.** The caller's microphone is muted while the
agent speaks (see ``TurnPolicy.mute_while_bot_speaks``), so at the moment of a
caller pause the agent channel is silent in reality too. Feeding the agent's
real TTS audio would need it time-aligned to the caller's microphone stream,
which this pipeline does not have. Recorded as a known approximation.

PyTorch and the model are optional: nothing here imports ``torch`` until a VAP
call is actually built, and :func:`vap_unavailable_reason` says what is missing
instead of failing a call at connect time.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn, SmartTurnParams

__all__ = ["VapTurnAnalyzer", "load_vap_model", "vap_unavailable_reason"]

log = structlog.get_logger(__name__)

_STATE_DICT = "example/VAP_3mmz3t0u_50Hz_ad20s_134-epoch9-val_2.56.pt"
_SAMPLE_RATE = 16_000

_lock = threading.Lock()
_model: Any = None
_model_dir: Path | None = None


def vap_unavailable_reason(model_dir: str | Path) -> str | None:
    """Why VAP cannot run here, or ``None`` when it can."""
    for module in ("torch", "torchaudio", "einops"):
        if importlib.util.find_spec(module) is None:
            return (
                f"VAP needs the Python package '{module}'. Install: pip install "
                "torch torchaudio --index-url https://download.pytorch.org/whl/cpu "
                "&& pip install einops"
            )
    root = Path(model_dir).expanduser()
    if not (root / "vap" / "model.py").exists() or not (root / _STATE_DICT).exists():
        return f"VAP model not found in {root}. Run: python tools/setup_vap.py"
    return None


def load_vap_model(model_dir: str | Path, *, threads: int = 2) -> Any:
    """Load the pretrained VAP model once per process. Blocking: call off-loop.

    ``weights_only=True`` on the checkpoint: it is a plain state dict, and a
    pickle loader that may execute code has no business reading a file fetched
    from the internet.
    """
    global _model, _model_dir
    root = Path(model_dir).expanduser()
    with _lock:
        if _model is not None and _model_dir == root:
            return _model
        if reason := vap_unavailable_reason(root):
            raise RuntimeError(reason)
        import torch

        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from vap.model import VapConfig, VapGPT  # type: ignore[import-not-found]

        started = time.perf_counter()
        torch.set_num_threads(threads)
        conf = VapConfig()
        # The CPC encoder's weights are inside the state dict; asking the
        # constructor to fetch them separately would download a second copy.
        conf.load_pretrained = 0
        model = VapGPT(conf)
        state = torch.load(root / _STATE_DICT, map_location="cpu", weights_only=True)
        missing, unexpected = model.load_state_dict(state, strict=False)
        if missing:
            # Missing weights mean a randomly initialised network that would
            # still produce confident-looking numbers. Refuse it.
            raise RuntimeError(
                f"VAP state dict does not match the model code: {len(missing)} "
                f"missing tensors (first: {missing[0]})"
            )
        model.eval()
        _model, _model_dir = model, root
        log.info(
            "voice.vap.loaded",
            dir=str(root),
            unexpected_keys=len(unexpected),
            load_ms=round((time.perf_counter() - started) * 1000),
        )
        return model


def shift_probability(p_future: Any) -> float:
    """P(the agent speaks next) at the newest frame, from VAP's ``p_future``.

    ``p_future`` is (batch, frames, 2): per frame, the probability that each
    speaker is the next to speak, channel 0 being the caller. Tolerates a
    trailing scalar (P(caller)) in case the model code returns that shape.
    """
    last = p_future[0, -1]
    values = [float(v) for v in (last.reshape(-1).tolist())]
    if len(values) >= 2:
        return values[1]
    return 1.0 - values[0]


class VapTurnAnalyzer(BaseSmartTurn):
    """Smart Turn's pause handling with VAP's turn-shift prediction."""

    def __init__(
        self,
        *,
        model_dir: str | Path,
        threshold: float = 0.5,
        params: SmartTurnParams | None = None,
    ) -> None:
        super().__init__(params=params)
        self._threshold = threshold
        self._vap = load_vap_model(model_dir)

    def _predict_endpoint(self, audio_array: np.ndarray) -> dict[str, Any]:
        import torch

        audio = torch.from_numpy(np.ascontiguousarray(audio_array, dtype=np.float32))
        rate = self.sample_rate or _SAMPLE_RATE
        if rate != _SAMPLE_RATE:
            from torchaudio.functional import resample  # type: ignore[import-untyped]

            audio = resample(audio, rate, _SAMPLE_RATE)
        stereo = torch.stack([audio, torch.zeros_like(audio)]).unsqueeze(0)
        with torch.no_grad():
            out = self._vap.probs(stereo)
        p_shift = shift_probability(out["p_future"])
        return {"prediction": 1 if p_shift >= self._threshold else 0, "probability": p_shift}
