"""Tests for wipro_live.safe_out_dir — where a capture is allowed to write.

Two concerns that must stay separate, because conflating them caused a real regression:

  * `per_run=True` is a POLICY for interactive entry points: every launch gets its own
    run_<timestamp> directory. It belongs to main(), not to Capture.
  * the guard is a SAFETY property of Capture: `_writer` opens its CSVs with mode "w", so
    writing into a directory that already holds a capture silently destroys it (a 244 s
    capture was lost this way -- docs/CIRCLE-PICTURE-AND-DATA-HAZARD.md).

The first version applied the per-run redirect unconditionally inside Capture, which
silently moved every programmatic caller's files into a subdirectory it never asked for
and broke four unrelated tests. `test_clean_dir_is_untouched_without_per_run` pins that
down.

    py -3.11 -m pytest tests/test_safe_out_dir.py -q
    py -3.11 tests/test_safe_out_dir.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import wipro_live as live  # noqa: E402


def _write(directory: pathlib.Path, name: str, rows: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(rows, encoding="utf-8")


def test_clean_dir_is_untouched_without_per_run() -> None:
    """A programmatic caller gets exactly the directory it asked for.

    This is the regression guard: a clean directory must NOT be redirected unless the
    caller opted into per-run directories.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "out"
        assert live.safe_out_dir(target) == target


def test_per_run_always_gets_its_own_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "out"
        chosen = live.safe_out_dir(target, per_run=True)
        assert chosen != target
        assert chosen.parent == target
        assert chosen.name.startswith("run_")


def test_existing_capture_is_not_overwritten() -> None:
    """The guard fires regardless of per_run, so no caller can silently lose data."""
    for per_run in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "out"
            _write(target, "range.csv", "a,b\n1,2\n")
            chosen = live.safe_out_dir(target, per_run=per_run)
            assert chosen != target, f"per_run={per_run}"
            assert chosen.parent == target
            assert (target / "range.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"


def test_zero_byte_file_does_not_trigger_the_guard() -> None:
    """An aborted run leaves empty files; those must not block reuse of the directory."""
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "out"
        _write(target, "range.csv", "")
        assert live.safe_out_dir(target) == target


def test_force_allows_overwrite() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "out"
        _write(target, "range.csv", "a,b\n1,2\n")
        assert live.safe_out_dir(target, force=True) == target
        # force also overrides per_run
        assert live.safe_out_dir(target, force=True, per_run=True) == target


def test_repeated_runs_do_not_collide() -> None:
    """Two runs within the same second must still get distinct directories."""
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "out"
        first = live.safe_out_dir(target, per_run=True)
        first.mkdir(parents=True)
        second = live.safe_out_dir(target, per_run=True)
        assert first != second


def test_capture_files_constant_is_intact() -> None:
    assert live.CAPTURE_FILES == ("range.csv", "ftm.csv", "csi.csv")


def test_capture_defaults_to_no_per_run() -> None:
    """Capture must keep a programmatic caller's path; only the entry points opt in."""
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "out"
        cap = live.Capture("COMx", target, ["id"])
        assert cap.out_dir == target


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok    {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:                                # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {exc!r}")
    print("PASS" if not failed else f"{failed} FAILURE(S)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
