#!/usr/bin/env python3
"""Record capture commands, board identity, Git state and candidate firmware hashes.

Image fingerprints describe local application files. They do not verify which
image is running on the connected board.

Run directly with python src/capture_provenance.py to inspect available metadata.
"""
from __future__ import annotations

import datetime
import hashlib
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
BASELINE = HERE.parent
FIRMWARE_BIN = BASELINE / "firmware" / "bin"

# Diagnostic strings used to fingerprint candidate firmware images.
MARKERS = (b"FTM initiation failed", b"num_entries=", b"wipro_iters")


def _sha256(p: pathlib.Path) -> str | None:
    try:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _git(args: list[str]) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=BASELINE, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=20)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def image_fingerprints() -> list[dict]:
    out = []
    if not FIRMWARE_BIN.is_dir():
        return out
    for p in sorted(FIRMWARE_BIN.rglob("*.bin")):
        try:
            head = p.read_bytes()[:1]
        except OSError:
            continue
        if not head or head != b"\xe9":        # ESP application image magic.
            continue
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        out.append({
            "path": str(p.relative_to(BASELINE)).replace("\\", "/"),
            "bytes": len(blob),
            "sha256": _sha256(p),
            "markers_present": [m.decode() for m in MARKERS if m in blob],
        })
    return out


def snapshot(commands: list[str] | None = None, board_mac: str | None = None,
             note: str | None = None) -> dict:
    return {
        "written_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "board_mac": board_mac,
        "note": note,
        "host_argv": sys.argv,
        "console_commands_sent": commands or [],
        "git": {
            "head": _git(["rev-parse", "HEAD"]),
            "firmware_dirty": [ln for ln in (_git(["status", "--porcelain", "--", "firmware/"])
                                             or "").splitlines() if ln.strip()],
            "firmware_src_clean_note": (
                "Native ranging source is pinned to upstream revision "
                "288311fd40cfdf629780ebc3604bc52671feec69. "
                "Review local changes against that reference. See docs/FIRMWARE.md."),
        },
        "firmware_images": image_fingerprints(),
    }


def write(out_dir: pathlib.Path, commands: list[str] | None = None,
          board_mac: str | None = None, note: str | None = None) -> pathlib.Path | None:
    """Write capture metadata without interrupting acquisition on collection errors."""
    try:
        import json
        out_dir = pathlib.Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "PROVENANCE.json"
        p.write_text(json.dumps(snapshot(commands, board_mac, note), indent=2,
                                ensure_ascii=False), encoding="utf-8")
        return p
    except Exception:                                        # noqa: BLE001
        return None


def main() -> int:
    import json
    print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
