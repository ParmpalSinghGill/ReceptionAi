#!/usr/bin/env python3
"""CI gate: every container image reference must be pinned by @sha256 digest.

05-PLATFORM-SECURITY-OPS.md §E21.3.

A mutable tag is a supply-chain hole. `:latest` and even `:v1.2.3` can be
re-pushed, so the image that passed CI is not provably the image that runs.
Kyverno verifies signatures at admission (§E19.8), but admission control cannot
help if the manifest itself names a tag that moved after signing.

Placeholder digests are all-zeros deliberately. The zero digest is
syntactically valid and references nothing, so an un-substituted manifest hits
ImagePullBackOff — loud, immediate, and local to the bad deploy — rather than
silently resolving `:latest` to whatever happens to be current. It is rejected
everywhere except the base overlays where CD substitutes the real digest.

Run:  python3 tools/check_image_pinning.py
Exit: 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "infra", ROOT / ".github", ROOT / "docker-compose.yml"]

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}\b")

# `image: repo/name@sha256:…`, a Dockerfile `FROM repo@sha256:…`, or a
# `COPY --from=repo@sha256:…`. The last one matters: the uv binary arrives that
# way, and an unpinned COPY --from is exactly as mutable as an unpinned FROM.
IMAGE_RE = re.compile(
    r"^\s*(?:-\s*)?(?:image\s*:|FROM\s+|COPY\s+--from=)\s*"
    r"(?P<ref>[A-Za-z0-9][\w.\-/]*(?::[\w.\-]+)?(?:@sha256:[0-9a-fA-F]+)?)",
    re.MULTILINE,
)

PLACEHOLDER = "@sha256:" + "0" * 64

# The base manifests hold the all-zero placeholder; CD substitutes the real
# digest at deploy time. A RENDERED overlay never may — that is what ships.
PLACEHOLDER_ALLOWED = {
    "infra/k8s/base/api/deployment.yaml",
    "infra/k8s/base/worker-bookings/deployment.yaml",
    "infra/k8s/base/worker-ingestion/deployment.yaml",
    "infra/k8s/base/worker-notifications/deployment.yaml",
    "infra/k8s/base/worker-voice/deployment.yaml",
    "infra/k8s/base/media/deployment.yaml",
    "infra/k8s/base/web/deployment.yaml",
}

# Multi-stage internal aliases (`FROM deps AS build`) are not registry refs.
STAGE_ALIASES = {"deps", "build", "runtime", "base", "scratch", "final", "test"}

# GitHub Actions `uses:` pins are a separate control (they pin by commit SHA,
# not image digest) and are checked by _check_action_pinning below.
SCANNABLE_SUFFIXES = {".yaml", ".yml"}


def _is_scannable(path: Path) -> bool:
    if path.suffix in SCANNABLE_SUFFIXES:
        return True
    # Dockerfile, api.Dockerfile, Dockerfile.dev …
    return "dockerfile" in path.name.lower()


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for target in SCAN_DIRS:
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(p for p in target.rglob("*") if p.is_file() and _is_scannable(p))
    return sorted(set(files))


def _check_images(path: Path, text: str, rel: str) -> list[str]:
    problems: list[str] = []
    for m in IMAGE_RE.finditer(text):
        ref = m.group("ref")
        line_no = text[: m.start()].count("\n") + 1

        # `FROM deps AS build`, `FROM $BASE`, `image: ${VAR}` — not registry refs.
        if ref in STAGE_ALIASES or ref.startswith("$") or "${" in ref:
            continue

        if PLACEHOLDER in ref.lower():
            if rel not in PLACEHOLDER_ALLOWED:
                problems.append(
                    f"{rel}:{line_no}: placeholder digest outside a CD-substituted "
                    f"base manifest: {ref}"
                )
            continue

        if not DIGEST_RE.search(ref):
            problems.append(
                f"{rel}:{line_no}: image is not pinned by digest: {ref}\n"
                f"      fix: docker buildx imagetools inspect {ref} "
                f"--format '{{{{.Manifest.Digest}}}}'"
            )
    return problems


# GitHub Actions are pinned by commit SHA rather than image digest, and for the
# same reason: a tag like `v4` is a moving pointer, and a compromised action
# runs with the workflow's token.
USES_RE = re.compile(r"^\s*(?:-\s*)?uses\s*:\s*(?P<ref>\S+)", re.MULTILINE)
SHA40_RE = re.compile(r"@[0-9a-f]{40}$")


def _check_action_pinning(text: str, rel: str) -> list[str]:
    problems: list[str] = []
    for m in USES_RE.finditer(text):
        ref = m.group("ref").strip("'\"")
        line_no = text[: m.start()].count("\n") + 1
        # Local (`./.github/actions/x`) and docker:// refs are not tag-pinned.
        if ref.startswith((".", "docker://")):
            continue
        if not SHA40_RE.search(ref):
            problems.append(
                f"{rel}:{line_no}: action is not pinned to a full commit SHA: {ref}\n"
                f"      a tag is a moving pointer, and the action runs with this "
                f"workflow's token"
            )
    return problems


def main() -> int:
    problems: list[str] = []

    for path in _iter_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        problems.extend(_check_images(path, text, rel))
        if rel.startswith(".github/workflows/"):
            problems.extend(_check_action_pinning(text, rel))

    if problems:
        print("Unpinned or invalid image/action references:\n", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "\nEvery image under infra/ (and docker-compose.yml) must be pinned by\n"
            "@sha256: digest, and every GitHub Action by a 40-character commit SHA.\n"
            "A mutable tag can be re-pushed, so the image CI verified is not\n"
            "provably the image that runs (§E21.3).",
            file=sys.stderr,
        )
        return 1

    print(f"image pinning OK — {len(_iter_files())} files scanned, 0 violations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
