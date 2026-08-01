# 03 — Lock

**What to build:** A file-based run lock primitive that prevents two Collector runs from overlapping. The lock is a file at a known path (e.g. `data/.lock`) containing the holder's PID. `acquire()` returns False if already held; `release()` removes the file.

**Blocked by:** 02 — Config

**Status:** ready-for-agent

- [ ] `Lock` class with `acquire()` (returns bool), `release()` (idempotent), `is_held()` (returns bool), `holder_pid` (int or None)
- [ ] `acquire()` writes the current PID atomically (write to tmp, rename)
- [ ] If the lock is held by a dead PID (process not running), `acquire()` can re-take it (stale-lock detection)
- [ ] `release()` does not raise if the lock is already gone
- [ ] `Lock` is used as a context manager: `with Lock(path) as held: ...`
- [ ] Tests use `tmp_path` fixture; no real filesystem state
- [ ] Tests cover: acquire→release cycle, second acquire returns False, stale PID re-acquisition, missing parent dir creates it
