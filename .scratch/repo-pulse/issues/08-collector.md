# 07 — Collector

**What to build:** The orchestration that runs a daily snapshot end-to-end. `run_daily_snapshot(date)` acquires the lock, fetches the starred list, filters via watchlist, fetches each repo's full data + latest release, writes snapshots, marks `in_watchlist`, releases the lock. On a single-repo error, logs and continues. This is the only writer of snapshots.

**Blocked by:** 03 — Lock, 04 — GitHub client, 05 — Watchlist filter, 06 — Storage

**Status:** ready-for-agent

- [ ] `run_daily_snapshot(date: date, *, config, gh, db) -> RunSummary` is the public entry point
- [ ] Acquires `Lock(data_dir / ".lock")` at start; releases on success and on error
- [ ] Fetches starred list via `gh.fetch_starred()`
- [ ] Applies `filter_starred(...)` to derive the watchlist
- [ ] For each watchlisted repo: fetches full repo via `gh.fetch_repo(owner, name)` and latest release via `gh.fetch_latest_release(...)`
- [ ] Writes the repo row via `db.upsert_repository(...)` with `in_watchlist=True`
- [ ] Writes the snapshot via `db.write_snapshot(...)` with all 22 fields
- [ ] Writes topics via `db.upsert_topics(...)`
- [ ] Repos that were in last run's watchlist but not in this run are marked `in_watchlist=False` (lifecycle transition)
- [ ] On a per-repo error: logs a structured warning with `full_name` and exception, increments `skipped` counter, continues
- [ ] Returns `RunSummary(date, total=200, succeeded=195, skipped=5, errors=[...])`
- [ ] Tests use mocked `gh` (return canned data) and in-memory `db`
- [ ] Tests cover: happy path with 3 repos, 2 repos skipped due to 404, 1 repo with 500 (retry then skip), lock contention, watchlist transition (3 in last, 2 in this — 1 marked inactive)
- [ ] The Collector never invokes viral detection (per ADR 0003 — that's analytics' job)


Respects 00-architecture doctrine
