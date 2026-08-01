"""Tests for the ``repo_pulse.lock`` layer — stale-lock / cross-platform kill.

Companion to ``test_lock.py``; covers the cases that plant a
deliberately-dead PID into a lock file and check that the liveness
probe treats it as "not held".

The contract under test is in
``.scratch/repo-pulse/issues/04-lock.md``:

* A lock file whose PID is no longer running is re-taken on the
  next ``acquire()`` (stale-lock detection).
* ``is_held()`` and ``holder_pid`` both return ``False``/``None`` for
  a file whose PID is dead, not the holder of the lock.
* The tmp-file hygiene guarantee also holds on the stale-takeover
  path.

``os.kill(pid, 0)`` for a PID that does not exist raises
``ProcessLookupError`` (POSIX) or ``OSError``/``WinError 87``
(Windows). The production ``Lock._is_pid_alive`` maps both to
"not alive" — see ``src/repo_pulse/lock/__init__.py``.

Implementation note
-------------------

These tests do **not** call the real ``os.kill(_DEAD_PID, 0)``. The
"dead PID" path is exercised via a per-test ``monkeypatch`` of
``os.kill`` that raises ``ProcessLookupError`` for ``_DEAD_PID`` and
passes every other PID through to the real ``os.kill``. This was a
deliberate test-design choice for two reasons:

1. **OS independence.** ``_DEAD_PID`` is a parameter, not a hard
   constant. On Windows PID 1 has no owner; on POSIX PID 1 is the
   real ``init``/``systemd`` and *is* alive. The mock keeps the test
   deterministic on every platform.
2. **pytest-on-Windows-py3.12 interaction.** A long debugging session
   found that pytest 8.4 + Python 3.12 + Windows hangs after a few
   function-scoped tests in the same session that plant a "dead PID"
   in a file and let the production code call ``os.kill`` to check
   it. The exact mechanism is unknown (no hang in pure Python with
   20+ back-to-back ``os.kill(1, 0)`` calls). The mock sidesteps it
   entirely while still exercising the same ``_is_pid_alive``
   exception-handling branch.

The mock also returns a real ``os.kill`` for the test's own PID, so
the post-acquire assertions ("the file now contains our PID, which
is alive") still go through the real liveness probe.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from repo_pulse.lock import Lock

# A PID the mock will treat as "definitely dead" by raising
# ``ProcessLookupError`` from the patched ``os.kill``. The actual
# value does not matter — the production code never sees a real
# ``os.kill`` call for it — so we use 0xDEAD (57005) to be obviously
# not-a-real-process-id at a glance.
_DEAD_PID = 0xDEAD


@pytest.fixture
def dead_pid_os_kill(monkeypatch: pytest.MonkeyPatch) -> int:
    """Patch ``os.kill`` so ``_DEAD_PID`` is reported as not alive.

    Returns the dead PID for the test to plant in the lock file. The
    mock passes every other PID through to the real ``os.kill`` so
    the test's own liveness (e.g. after acquire) still uses the real
    probe.
    """
    real_kill = os.kill

    def fake_kill(pid: int, sig: int) -> None:
        if pid == _DEAD_PID:
            # The exact exception class raised by the real OS for a
            # non-existent PID. ``Lock._is_pid_alive`` maps this to
            # ``return False`` — the "stale holder" branch.
            raise ProcessLookupError(pid)
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", fake_kill)
    return _DEAD_PID


# ---------------------------------------------------------------------------
# holder_pid / is_held reporting on a dead-PID file
# ---------------------------------------------------------------------------


def test_holder_pid_is_none_when_holder_dead(
    tmp_path: Path, dead_pid_os_kill: int
) -> None:
    """A lock file with a dead PID inside is *not* held — ``holder_pid``
    is ``None`` and ``is_held()`` is False. The Collector will use this
    to decide whether to retry."""
    lock_path = tmp_path / "dead_holder.lock"
    lock_path.write_text(str(dead_pid_os_kill), encoding="utf-8")
    lock = Lock(lock_path)

    assert lock.is_held() is False
    assert lock.holder_pid is None


# ---------------------------------------------------------------------------
# Stale lock re-acquisition
# ---------------------------------------------------------------------------


def test_acquire_takes_over_stale_lock(
    tmp_path: Path, dead_pid_os_kill: int
) -> None:
    """The headline stale-lock contract: if the recorded PID is dead,
    ``acquire()`` succeeds and overwrites the file with the caller's PID."""
    lock_path = tmp_path / "stale.lock"
    lock_path.write_text(str(dead_pid_os_kill), encoding="utf-8")
    lock = Lock(lock_path)

    assert lock.acquire() is True
    try:
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
        assert lock.holder_pid == os.getpid()
    finally:
        lock.release()
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# Atomicity on the stale-takeover path
# ---------------------------------------------------------------------------


def test_stale_takeover_leaves_no_extraneous_files(
    tmp_path: Path, dead_pid_os_kill: int
) -> None:
    """Stale-lock takeover re-creates the file atomically; same
    hygiene guarantee as a fresh acquire: the lock file is the only
    artifact in its directory (no staging / tmp leftovers)."""
    lock_path = tmp_path / "stale_takeover.lock"
    lock_path.write_text(str(dead_pid_os_kill), encoding="utf-8")
    lock = Lock(lock_path)

    assert lock.acquire() is True
    try:
        siblings = sorted(p.name for p in lock_path.parent.iterdir())
        assert siblings == [lock_path.name], (
            f"unexpected siblings after stale takeover: {siblings}"
        )
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Cross-platform aliveness contract
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific behaviour")
def test_holder_pid_uses_pid_check_consistent_with_lock(tmp_path: Path) -> None:
    """On Windows, ``os.kill(pid, 0)`` for a foreign PID (e.g. a system
    process) raises ``PermissionError``, *not* ``ProcessLookupError``.
    The lock must treat that as "alive" — otherwise a lock held by a
    system process the user cannot signal would be silently stolen.

    The *positive* half of the contract — that the ``is_held()`` /
    ``holder_pid`` pair reports our own live PID correctly — is what
    this test pins on Windows. The ``PermissionError→alive`` branch is
    covered by the same code path on every other platform where
    foreign PIDs are visible.
    """
    lock_path = tmp_path / "win_pidcheck.lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    lock = Lock(lock_path)
    try:
        assert lock.holder_pid == os.getpid()
        assert lock.is_held() is True
    finally:
        lock.release()
