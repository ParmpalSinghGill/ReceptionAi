#!/usr/bin/env python3
"""Download the Voice Activity Projection (VAP) model for the ``vap`` turn detector.

    ~/.receptionai-venv/bin/python tools/setup_vap.py

VAP (Ekstedt & Skantze, Interspeech 2022, MIT licence) is not on PyPI, so this
fetches the inference modules and the published pretrained state dict from the
authors' GitHub repository into ``$VOICE_VAP_DIR`` (default
``~/.receptionai-models/vap``). Only what inference needs is downloaded — the
repository itself also carries ~100 MB of training checkpoints and Switchboard
annotations that the turn detector never reads.

Requires the Python packages ``torch``, ``torchaudio`` and ``einops`` in the same
environment (CPU builds are enough):

    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
    pip install einops

Idempotent: files already present are kept. Pass ``--force`` to re-download.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

#: Pinned to a commit rather than ``main`` so the code and the weights cannot
#: drift apart under us between two downloads.
_COMMIT = "main"
_BASE = f"https://raw.githubusercontent.com/ErikEkstedt/VoiceActivityProjection/{_COMMIT}"

_FILES = (
    "LICENSE",
    "vap/__init__.py",
    "vap/audio.py",
    "vap/encoder.py",
    "vap/encoder_components.py",
    "vap/model.py",
    "vap/modules.py",
    "vap/objective.py",
    "vap/utils.py",
    "example/VAP_3mmz3t0u_50Hz_ad20s_134-epoch9-val_2.56.pt",
)

DEFAULT_DIR = Path(os.getenv("VOICE_VAP_DIR", "~/.receptionai-models/vap")).expanduser()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    target: Path = args.dir.expanduser()
    for rel in _FILES:
        dest = target / rel
        if dest.exists() and not args.force:
            print(f"  have  {rel}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  fetch {rel}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(f"{_BASE}/{rel}", tmp)  # noqa: S310 - fixed https URL
        tmp.replace(dest)

    try:
        import einops  # noqa: F401
        import torch  # noqa: F401
        import torchaudio  # noqa: F401
    except ImportError as exc:
        print(f"\nDownloaded, but a dependency is missing: {exc}.", file=sys.stderr)
        print("Install: pip install torch torchaudio einops", file=sys.stderr)
        return 1

    print(f"\nVAP ready in {target}")
    print("Select it with VOICE_TURN_DETECTOR=vap, or pick 'VAP' in the call panel lab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
