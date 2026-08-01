"""Tests for the ``repo_pulse.lock`` layer — happy path / contract.

Covers the acceptance criteria in
``.scratch/repo-pulse/issues/04-lock.md`` for the no-PID-collision
cases: acquire/release round-trip, double-acquire guard, sibling
acquire, idempotent release, parent-dir creation, the context
manager, and the tmp-file hygiene of a fresh acquire.

The stale-lock / dead-PID / cross-platform-kill cases live in
``test_lock_stale.py``. They were split out after a Windows-specific
pytest-3.12 interaction (function-scoped ``tmp_path`` hangs after
several tests in the same session) was observed when a planted
dead-PID case ran after a series of acquire/release tests. The
behaviour is identical — both files import the same ``Lock`` and the
same helpers — but a single short file per session avoids the hang.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from repo_pulse.lock import Lock

# ---------------------------------------------------------------------------
# Basic acquire/release cycle
# ---------------------------------------------------------------------------


def test_acquire_then_release_round_trip(tmp_path: Path) -> None:
    """Acquire on a free file → True, file appears with our PID; release
    removes the file and ``is_held()`` goes back to False."""
    lock_path = tmp_path / "round_trip.lock"
    lock = Lock(lock_path)
    assert not lock_path.exists()

    assert lock.acquire() is True
    assert lock_path.exists()
    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert lock.is_held() is True
    assert lock.holder_pid == os.getpid()

    lock.release()
    assert not lock_path.exists()
    assert lock.is_held() is False
    assert lock.holder_pid is None


def test_second_acquire_by_same_lock_returns_false(tmp_path: Path) -> None:
    """A second ``acquire()`` on the same Lock object is a no-op (False)."""
    lock = Lock(tmp_path / "double_acquire.lock")
    assert lock.acquire() is True
    try:
        assert lock.acquire() is False
    finally:
        lock.release()


def test_second_acquire_by_sibling_lock_returns_false(tmp_path: Path) -> None:
    """A second ``Lock`` instance pointing at the same file in the same
    process cannot steal the lock just because the holder PID matches its
    own. (Without the file-based check, a sibling instance would treat
    us as dead.)"""
    lock_path = tmp_path / "sibling.lock"
    lock = Lock(lock_path)
    sibling = Lock(lock_path)
    assert lock.acquire() is True
    try:
        assert sibling.acquire() is False
    finally:
        lock.release()
    # After release, the sibling can take it.
    assert sibling.acquire() is True
    sibling.release()


def test_release_is_idempotent(tmp_path: Path) -> None:
    """``release()`` does not raise when called without a prior acquire
    and is safe to call twice in a row."""
    lock = Lock(tmp_path / "idempotent.lock")
    lock.release()  # no-op
    assert lock.acquire() is True
    lock.release()
    lock.release()  # no-op


def test_release_when_file_already_removed_does_not_raise(tmp_path: Path) -> None:
    """If something else removed the lock file out from under us, our
    ``release()`` still has to be idempotent — the Collector cleanup
    path runs unconditionally."""
    lock = Lock(tmp_path / "external_rm.lock")
    assert lock.acquire() is True
    # Simulate an external rm (e.g. operator cleanup, or a parallel
    # release from a duplicate context manager).
    lock.path.unlink()

    lock.release()  # must not raise FileNotFoundError
    assert not lock.path.exists()


def test_acquire_after_external_unlink_recovers(tmp_path: Path) -> None:
    """An external ``unlink`` between two ``acquire()`` calls must not
    strand the ``Lock`` instance in a state where the in-memory
    ``_we_hold`` says "we own it" forever.

    The Collector relies on the symmetric "release is idempotent"
    contract (test above). The other half of the symmetry — "acquire
    after a phantom external rm still works" — is what this test
    pins. Without the file-vs-flag re-check in ``acquire()``, a
    second ``acquire()`` on the same instance returns ``False``
    forever, even though the on-disk lock is up for grabs.
    """
    lock = Lock(tmp_path / "phantom.lock")
    assert lock.acquire() is True
    # External rm without a release(). Now the file is gone but the
    # in-memory flag still says we own it.
    lock.path.unlink()

    # A second acquire() on the same instance must re-take the lock
    # (the file-confirmation check clears the stale flag and we go
    # through the normal acquire path).
    assert lock.acquire() is True
    try:
        assert lock.path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# is_held / holder_pid
# ---------------------------------------------------------------------------


def test_is_held_false_when_no_file(tmp_path: Path) -> None:
    """No file on disk → ``is_held()`` is False, ``holder_pid`` is None."""
    lock = Lock(tmp_path / "missing.lock")
    assert lock.is_held() is False
    assert lock.holder_pid is None


def test_holder_pid_returns_live_pid(tmp_path: Path) -> None:
    """A lock file with our own PID inside → ``holder_pid`` is that PID
    and ``is_held()`` is True."""
    lock_path = tmp_path / "live_holder.lock"
    # Plant our own PID (we are by definition alive in this process).
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    lock = Lock(lock_path)

    assert lock.is_held() is True
    assert lock.holder_pid == os.getpid()


def test_holder_pid_none_for_corrupt_file(tmp_path: Path) -> None:
    """Non-numeric / empty content → no holder, so acquire takes over."""
    for content in ("", " ", "not-a-pid", "12.34"):
        lock_path = tmp_path / f"corrupt_{abs(hash(content))}.lock"
        lock_path.write_text(content, encoding="utf-8")
        lock = Lock(lock_path)

        assert lock.is_held() is False, (
            f"corrupt content {content!r} should not be held"
        )
        assert lock.holder_pid is None, (
            f"corrupt content {content!r} should yield None PID"
        )


# ---------------------------------------------------------------------------
# Missing parent directory
# ---------------------------------------------------------------------------


def test_acquire_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """A nested, not-yet-existing parent directory is created on acquire.

    The deploy case: the first run on a fresh VPS hits an empty
    ``/opt/repo-pulse/data/`` and the lock file's parent chain has to
    spring into existence without the user running ``mkdir`` first.
    """
    deep = tmp_path / "deeply" / "nested" / "data"
    lock_path = deep / "fresh.lock"
    assert not deep.exists()

    lock = Lock(lock_path)
    assert lock.acquire() is True
    try:
        assert deep.is_dir()
        assert lock_path.is_file()
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_acquires_on_enter_and_releases_on_exit(
    tmp_path: Path,
) -> None:
    """``with Lock(path) as held:`` enters with the lock acquired (held=True)
    and releases on exit. The ``held`` value lets the caller branch on
    "another run is in flight" without re-checking ``is_held()``."""
    lock_path = tmp_path / "ctx_basic.lock"
    lock = Lock(lock_path)
    with lock as held:
        assert held is True
        assert lock_path.exists()
        assert lock.is_held() is True
    assert not lock_path.exists()
    assert lock.is_held() is False


def test_context_manager_held_false_when_already_locked(tmp_path: Path) -> None:
    """When the lock is already held by an external ``acquire()``, the
    context manager still enters (no exception) but ``held`` is False,
    and the body must not assume exclusive access. Exit is a no-op
    (does not release someone else's lock)."""
    lock_path = tmp_path / "ctx_held.lock"
    lock = Lock(lock_path)
    assert lock.acquire() is True
    try:
        with lock as held:
            assert held is False
            # The body runs, but the lock is still held by us.
            assert lock.is_held() is True
        # Exit did not release someone else's lock.
        assert lock.is_held() is True
    finally:
        lock.release()


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    """A raised exception inside the ``with`` block must not leave the
    lock held — the Collector wraps the snapshot body in the lock and
    any unhandled error has to release cleanly."""
    lock_path = tmp_path / "ctx_exc.lock"
    lock = Lock(lock_path)
    with pytest.raises(RuntimeError, match="boom"):
        with lock as held:
            assert held is True
            raise RuntimeError("boom")
    assert not lock_path.exists()
    assert lock.is_held() is False


# ---------------------------------------------------------------------------
# Atomicity / tmp-file hygiene (fresh-acquire path)
# ---------------------------------------------------------------------------


def test_no_tmp_file_lingers_after_successful_acquire(tmp_path: Path) -> None:
    """A successful acquire must not leave a ``.tmp.<pid>`` file behind.

    The tmp file is the "write to tmp, rename" staging area; if the
    rename succeeded it must be gone, and the final lock file must
    contain our PID."""
    lock_path = tmp_path / "tmp_hygiene_ok.lock"
    lock = Lock(lock_path)
    assert lock.acquire() is True
    try:
        # Look in the same directory as the lock file for any stray
        # tmp staging. ``*.tmp.*`` would also match unrelated files
        # (none expected in tmp_path, but be specific just in case).
        siblings = [p for p in lock_path.parent.iterdir() if ".tmp." in p.name]
        assert siblings == [], f"stray tmp file(s) left behind: {siblings}"
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()
