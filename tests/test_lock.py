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
from unittest.mock import patch

import pytest

from repo_pulse.lock import Lock, _write_all

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
# Atomicity — the lock file is created in a single syscall
# ---------------------------------------------------------------------------


def test_lock_directory_has_no_extraneous_files_after_acquire(
    tmp_path: Path,
) -> None:
    """The atomic-create path (``O_CREAT | O_EXCL``) must not leave any
    staging / tmp file behind. The lock file itself is the only
    artifact of a successful acquire.
    """
    lock_path = tmp_path / "atomic_create.lock"
    lock = Lock(lock_path)
    assert lock.acquire() is True
    try:
        siblings = sorted(p.name for p in lock_path.parent.iterdir())
        assert siblings == [lock_path.name], (
            f"unexpected siblings in lock dir: {siblings}"
        )
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# _write_all — short-write loop on the staging file
# ---------------------------------------------------------------------------


# ``os.open`` defaults to text mode on Windows (which would silently
# translate ``\n`` to ``\r\n`` in the bytes we hand it). The lock
# layer's content is always ASCII decimal + ``\n``, so we need the
# file open in binary mode for the test assertions to be byte-equal
# to the input. ``os.O_BINARY`` exists on Windows only.
_O_BINARY = getattr(os, "O_BINARY", 0)


def _open_writable_binary(path: Path) -> int:
    """Open ``path`` for writing in binary mode, cross-platform.

    On POSIX, ``O_BINARY`` is 0 and the flags work as-is. On
    Windows, ``os.open`` defaults to text mode unless ``O_BINARY``
    is set; without it, a ``b"foo\nbar"`` payload comes back as
    ``b"foo\r\nbar"`` and the byte-equality assertion in
    ``test_write_all_*`` fires even though the helper is correct.
    """
    return os.open(path, os.O_CREAT | os.O_WRONLY | _O_BINARY, 0o644)


def test_write_all_writes_every_byte_on_a_normal_call(tmp_path: Path) -> None:
    """Smoke: a real ``os.write`` on a regular file returns the full
    length, so ``_write_all`` calls ``os.write`` exactly once and the
    file ends up with the full payload. This is the
    everyday-case regression net for the helper.
    """
    payload = b"12345\n"
    path = tmp_path / "normal_write.bin"
    fd = _open_writable_binary(path)
    try:
        _write_all(fd, payload)
    finally:
        os.close(fd)
    assert path.read_bytes() == payload


def test_write_all_loops_on_short_writes(tmp_path: Path) -> None:
    """The contract that the rest of ``acquire()`` depends on: when
    ``os.write`` reports a short write, the helper loops until every
    byte is on disk. Without the loop, a short write would leave a
    truncated tmp file, ``os.replace`` would atomically publish the
    truncation, and the next reader would treat the corrupt content
    as "no holder" and steal the lock — the same
    mutual-exclusion regression the ``tmp + rename`` scheme exists
    to prevent.

    The mock hands the file back one byte at a time and reports
    "1 written" each time, so the helper has to call ``os.write``
    exactly ``len(payload)`` times. If the loop ever bails early,
    the final assertion on the file fails (the mock wrote fewer
    bytes than the payload length).

    ``real_write`` is captured *before* the patch so the mock can
    call through to the genuine syscall to push the actual byte
    onto the file — otherwise the file would stay empty regardless
    of how well the loop worked, and the test would not actually
    pin the contract.
    """
    payload = b"12345\n"
    path = tmp_path / "short_writes.bin"
    fd = _open_writable_binary(path)

    real_write = os.write
    calls: list[bytes] = []

    def one_byte_at_a_time(_fd: int, data: bytes) -> int:
        # Write the first byte of the requested data to the real
        # file via the captured (un-patched) ``os.write``, then
        # report "1 byte written" so the loop has to come back.
        calls.append(bytes(data))
        return real_write(_fd, data[:1])

    try:
        with patch("repo_pulse.lock.os.write", side_effect=one_byte_at_a_time):
            _write_all(fd, payload)
    finally:
        os.close(fd)

    # The loop called os.write once per byte of the payload. Each
    # call returned 1; the loop kept going until the data view
    # was empty. The first call had the full payload, the last
    # had one byte.
    assert len(calls) == len(payload)
    assert calls[0] == payload
    assert calls[-1] == payload[-1:]
    # And the file got the full payload, byte by byte.
    assert path.read_bytes() == payload


def test_write_all_raises_when_os_write_returns_zero(tmp_path: Path) -> None:
    """A zero-byte return from ``os.write`` on a regular file is a
    filesystem-level failure (full disk, broken pipe surrogate,
    whatever). ``_write_all`` must surface it as ``OSError`` so the
    surrounding ``try/except`` in ``acquire()`` cleans up the
    staging file and returns ``False`` — not silently publish a
    zero-byte lock via the subsequent ``os.replace``.
    """
    path = tmp_path / "zero_write.bin"
    fd = _open_writable_binary(path)
    try:
        with patch("repo_pulse.lock.os.write", return_value=0):
            with pytest.raises(OSError, match="returned 0"):
                _write_all(fd, b"12345\n")
    finally:
        os.close(fd)
    # The file was opened but never written to; the helper raised
    # before completing any write.
    assert path.read_bytes() == b""


def test_acquire_keeps_full_pid_even_if_os_write_short_writes(
    tmp_path: Path,
) -> None:
    """End-to-end pin: with ``os.write`` mocked to return short
    lengths, ``Lock.acquire()`` still produces a lock file whose
    content is the *complete* PID. This is the regression that
    motivated the ``_write_all`` helper — without the loop, the
    ``os.replace`` would atomically publish whatever ``os.write``
    managed to push, and the next process would see a truncated /
    empty file and treat the lock as up for grabs.
    """
    lock_path = tmp_path / "short_write.lock"
    lock = Lock(lock_path)
    real_write = os.write

    def short_writes(fd: int, data: bytes) -> int:
        # Two bytes per call, regardless of how much we were asked
        # for. ``_write_all`` has to keep calling.
        return real_write(fd, data[:2])

    # Capture the file content inside the ``with patch`` block —
    # *before* ``release()`` unlinks the file in the test's
    # ``finally``. Reading after release would FileNotFoundError on
    # a normal-acquire path too.
    with patch("repo_pulse.lock.os.write", side_effect=short_writes):
        try:
            assert lock.acquire() is True
            captured = lock_path.read_bytes()
        finally:
            lock.release()

    assert captured.strip() == str(os.getpid()).encode("utf-8")
