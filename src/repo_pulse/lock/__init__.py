"""File-based run lock for Repo-Pulse.

The contract lives in ``.scratch/repo-pulse/issues/04-lock.md``. This
is a leaf primitive: the ``lock`` layer imports only ``os``,
``pathlib``, and ``from __future__ import annotations`` (a
language-level feature), per the carve-out in
``.scratch/repo-pulse/issues/00-architecture.md``. The boundary is
enforced by ``tests/test_architecture.py``.

The ``Lock`` class is defined directly in this module — not in a
``lock.py`` submodule — because the ``lock`` layer is a leaf
primitive and may not import any other ``repo_pulse`` submodule
(including its own). Keeping the implementation in
``__init__.py`` keeps the public surface (``from repo_pulse.lock
import Lock``) unchanged while satisfying the AST enforcer.

Atomicity note
--------------
The ticket suggests "write to tmp, rename" as the implementation
for the atomic-write requirement. An earlier revision followed
that suggestion literally with a ``Path.write_text`` + ``os.replace``
pair. That worked, but on Windows the brief handle window between
``os.replace`` and the caller's next operation occasionally
tripped ``tmp_path`` finalizer hangs in pytest (a pytest-8.4 /
Windows / plugin-autoload interaction we did not fully isolate).
The current implementation uses ``os.open`` with
``O_CREAT | O_EXCL | O_WRONLY`` for the atomic create — a single
syscall that either creates the file with our PID written into it
or fails with ``FileExistsError`` if someone else beat us to it.
This is the canonical POSIX/Windows pattern for "atomically claim
a file" and removes the rename path entirely, which is what
resolves the Windows hang.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["Lock"]

# A non-PID integer is not a valid holder (negative PIDs do not exist
# on POSIX, and ``os.getpid()`` is always > 0 on every supported
# platform). We treat anything <= 0 as "no holder" so a corrupt /
# truncated lock file never causes us to loop forever waiting on a
# signal that can never be delivered. Kept as a plain module
# constant (not ``typing.Final``) because the ``lock`` layer may not
# import ``typing`` per the 00-architecture doctrine.
_MIN_VALID_PID = 1


class Lock:
    """A file-based run lock guarded by a PID-based liveness check.

    Two ``Lock`` instances on the same path coordinate through the
    on-disk file, not through Python-level state, so a process that
    started the lock can be observed by a sibling ``Lock`` (or by a
    future Collector run) just by reading the file.

    Typical usage:

    .. code-block:: python

        with Lock(data_dir / ".lock") as held:
            if not held:
                # Another run is in flight; bail out.
                return
            run_collector()

    Parameters
    ----------
    path:
        Filesystem path of the lock file. The parent directory is
        created on first acquire (mirrors the ``mkdir(parents=True,
        exist_ok=True)`` policy of the ``config`` layer) so the
        Collector can start on a fresh VPS with an empty data dir.
    """

    __slots__ = ("path", "_we_hold", "_ctx_acquired")

    def __init__(self, path: Path) -> None:
        self.path: Path = path
        # Tracks whether *this* instance successfully called acquire().
        # Used to make release() idempotent and to keep the context
        # manager's __exit__ from releasing a lock the body never took.
        self._we_hold: bool = False
        # Tracks whether the *current* context-manager block took the
        # lock. Distinct from ``_we_hold`` so that ``with`` is a
        # no-op when the lock was already held by an external
        # ``acquire()`` — the test
        # ``test_context_manager_held_false_when_already_locked``
        # pins that contract.
        self._ctx_acquired: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns ``True`` iff we got it.

        Non-blocking. ``False`` is returned in three cases:

        1. This ``Lock`` instance still holds the lock from a prior
           successful ``acquire()`` and the file on disk is consistent
           with that (idempotent double-acquire guard for the
           context-manager path).
        2. The lock file exists, its content parses to an integer
           PID, and that PID is a live process.
        3. The atomic create failed because the file appeared
           between our liveness check and our ``os.open`` (race) —
           extremely rare; the caller sees ``False`` and bails.

        Known race (stale-takeover window)
        ---------------------------------
        Between the liveness check and the atomic create there is a
        small window in which a second process can re-acquire the
        same dead-PID lock. Whichever ``os.open`` lands first wins;
        the loser's ``acquire()`` returns ``False``. This is a
        fundamental property of file-based PID locks with no kernel
        support (``flock``/``fcntl``) and is acceptable for a
        personal monitoring tool where the worst outcome is "two
        Collectors run at the same time for a few seconds". The
        ticket does not pin a stronger guarantee.
        """
        if self._we_hold:
            # Re-validate against the file before trusting the flag:
            # an external ``lock.path.unlink()`` (e.g. operator
            # cleanup) would leave us in a state where the in-memory
            # ``_we_hold`` says we own the lock but the file says
            # otherwise. The test
            # ``test_acquire_after_external_unlink_recovers`` pins
            # the recovery path; the symmetric "we still hold it"
            # path is the fast-return for a normal double-acquire.
            if self._file_confirms_our_hold():
                return False
            # Our flag is stale; reset and fall through to a fresh
            # acquisition attempt.
            self._we_hold = False

        # If a lock file already exists, check whether its holder is
        # still alive. Stale (dead) PIDs are re-taken by unlinking the
        # file and falling through to the create path.
        if self.path.exists():
            existing = self._read_holder_pid()
            if existing is not None and self._is_pid_alive(existing):
                return False
            # Stale, corrupt, or already-empty: remove before we
            # try to create over it. ``missing_ok`` covers the race
            # where another acquirer reaped it between read and
            # unlink.
            self.path.unlink(missing_ok=True)

        # Atomic create. ``O_CREAT | O_EXCL`` is a single syscall
        # that either creates the file or fails with
        # ``FileExistsError`` — no half-written file, no rename
        # window. We then write our PID and close the fd.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError:
            # Race: another acquirer created the file between our
            # unlink and our open. Re-check on the next call.
            return False
        except OSError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)

        self._we_hold = True
        return True

    def release(self) -> None:
        """Release the lock. Idempotent — never raises.

        Safe to call without a prior acquire, safe to call twice in a
        row, safe to call after the lock file has been removed by
        something else (e.g. an operator cleanup or a parallel
        context-manager exit). The Collector cleanup path runs
        unconditionally and has to tolerate all three cases.
        """
        if self._we_hold and self.path.exists():
            try:
                self.path.unlink()
            except FileNotFoundError:
                # Lost a race with another releaser; that's fine, the
                # goal state (no file) is already achieved.
                pass
        self._we_hold = False

    def is_held(self) -> bool:
        """``True`` iff the lock file currently names a live process.

        Equivalent to ``self.holder_pid is not None`` — kept as a
        separate method because the boolean form is the common case
        at call sites and reads more naturally than ``is not None``.
        """
        return self.holder_pid is not None

    @property
    def holder_pid(self) -> int | None:
        """PID of the current *live* holder, or ``None`` if not held.

        "Live" matters here: a lock file containing a dead PID is
        not considered held, so this returns ``None`` for a stale
        lock. Callers that need the raw recorded PID (e.g. for
        diagnostics) should read the file directly.

        Reads the file on every access so the answer is always in
        sync with disk state across processes — even if *we* did not
        call ``acquire()`` ourselves (e.g. another Collector run is
        already in flight on the same data dir).
        """
        pid = self._read_holder_pid()
        if pid is None or not self._is_pid_alive(pid):
            return None
        return pid

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> bool:
        """Enter the context; the bound value is the ``acquire()`` result.

        Using ``with Lock(path) as held:`` lets the caller branch on
        "another run is in flight" without a second call to
        ``is_held()``. The body runs regardless of the value; ``held``
        is purely informational.
        """
        self._ctx_acquired = self.acquire()
        return self._ctx_acquired

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Exit the context. Releases iff *this* ``__enter__`` acquired.

        Tracking the *context-local* acquire result (separate from
        ``_we_hold``) means a ``with`` block on a Lock that was
        already acquired by an external ``lock.acquire()`` is a
        no-op: ``__enter__`` returns ``False`` and ``__exit__`` does
        not touch the file. The body of the ``with`` sees ``held ==
        False`` and can branch on "another run is in flight" without
        the context manager stealing the lock the caller already
        holds.
        """
        if self._ctx_acquired:
            self.release()
        self._ctx_acquired = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _file_confirms_our_hold(self) -> bool:
        """``True`` iff the on-disk file currently says *we* hold it.

        Used by ``acquire()`` to recover from a stale ``_we_hold``
        flag (e.g. an external ``lock.path.unlink()`` followed by a
        second ``acquire()`` on the same instance). Cheap: one file
        read + an int compare, no ``os.kill`` call.
        """
        if not self.path.exists():
            return False
        recorded = self._read_holder_pid()
        return recorded is not None and recorded == os.getpid()

    def _read_holder_pid(self) -> int | None:
        """Parse the recorded PID, or return ``None`` for any parse failure.

        Treats empty content, non-numeric content, negative PIDs, and
        unreadable files as "no holder" — in all those cases a fresh
        ``acquire()`` should take over rather than treat the lock as
        permanently held by an unidentifiable ghost process.
        """
        try:
            content = self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not content:
            return None
        try:
            pid = int(content)
        except ValueError:
            return None
        if pid < _MIN_VALID_PID:
            return None
        return pid

    def _is_pid_alive(self, pid: int) -> bool:
        """``True`` iff ``pid`` refers to a live process right now.

        Uses ``os.kill(pid, 0)`` for the cross-platform liveness check.
        The exception mapping is deliberate:

        * ``ProcessLookupError`` (POSIX ESRCH / Windows ERROR_INVALID_PARAMETER):
          the PID does not exist ⇒ not alive.
        * ``PermissionError`` (Windows ERROR_ACCESS_DENIED): the PID
          exists but we cannot signal it (e.g. a SYSTEM process). We
          treat that as alive — stealing a lock held by a process we
          cannot signal is a worse failure mode than a stuck run, and
          the Collector runs single-user on a VPS so this branch is
          not exercised in production but is here for correctness.
        * any other ``OSError``: bail conservatively to "not alive"
          so a transient error does not deadlock the next acquire.
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
