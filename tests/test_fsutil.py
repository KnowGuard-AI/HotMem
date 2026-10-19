"""Tests for the shared fsutil crash-safety primitives (#101, commit 2).

Covers:
    - atomic_publish moves a staging directory into place.
    - A pre-existing final artifact is replaced cleanly.
    - The backup-restore branch: a failed publish leaves the previous
      artifact intact and the staging directory untouched.
    - fsync_dir runs on a real directory (smoke).
"""

from __future__ import annotations

from pathlib import Path

from hotmem.fsutil import atomic_publish, fsync_dir


def _make_dir(path: Path, marker: str) -> Path:
    path.mkdir(parents=True)
    (path / marker).write_text(marker)
    return path


def test_atomic_publish_moves_staging_into_place(tmp_path: Path):
    staging = _make_dir(tmp_path / "staging", "payload.txt")
    final = tmp_path / "final"

    atomic_publish(staging, final)

    assert (final / "payload.txt").read_text() == "payload.txt"
    assert not staging.exists()


def test_atomic_publish_replaces_previous_final(tmp_path: Path):
    _make_dir(tmp_path / "final", "old.txt")
    staging = _make_dir(tmp_path / "staging", "new.txt")

    atomic_publish(staging, tmp_path / "final")

    final = tmp_path / "final"
    assert (final / "new.txt").read_text() == "new.txt"
    assert not (final / "old.txt").exists()
    # No backup leftovers remain.
    assert [p.name for p in tmp_path.iterdir()] == ["final"]


def test_atomic_publish_restores_previous_final_on_failure(tmp_path: Path, monkeypatch):
    final = _make_dir(tmp_path / "final", "old.txt")
    staging = _make_dir(tmp_path / "staging", "new.txt")

    real_replace = __import__("os").replace
    calls: list[str] = []

    def flaky_replace(src, dst):
        calls.append(str(dst))
        # First call moves the previous final aside; second (staging -> final)
        # fails so the restore branch must run.
        if len(calls) == 2:
            raise OSError("simulated publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr("hotmem.fsutil.os.replace", flaky_replace)

    try:
        atomic_publish(staging, final)
    except OSError:
        pass
    else:
        raise AssertionError("expected the simulated publish failure")

    # The previous artifact is intact at its original path.
    assert (final / "old.txt").read_text() == "old.txt"
    # Staging survives untouched — nothing was destroyed.
    assert (staging / "new.txt").read_text() == "new.txt"


def test_fsync_dir_smoke(tmp_path: Path):
    fsync_dir(tmp_path)
    assert tmp_path.is_dir()


def test_fsync_dir_windows_noop(tmp_path: Path, monkeypatch):
    """On Windows (os.name == 'nt'), os.open on a directory raises PermissionError;
    fsync_dir must no-op cleanly so atomic package/delta/handoff publish succeeds (#118)."""
    monkeypatch.setattr("hotmem.fsutil.os.name", "nt")

    def forbid_dir_open(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("hotmem.fsutil.os.open", forbid_dir_open)
    fsync_dir(tmp_path)


def test_confined_relpath_rejects_windows_drive_and_backslash_escapes(tmp_path: Path):
    """Manifest path confinement rejects Windows drive-relative, drive-absolute,
    and leading-backslash paths on all platforms (#118)."""
    from hotmem.interchange.paths import confined_relpath

    for unsafe in (
        r"\outside.txt",
        r"\\server\share\secret.txt",
        "C:outside.txt",
        r"C:\outside.txt",
        "C:/outside.txt",
        r"sub\..\..\outside.txt",
        "attachments\\..\\secret",
    ):
        assert not confined_relpath(tmp_path, unsafe), f"expected {unsafe!r} to be rejected"
