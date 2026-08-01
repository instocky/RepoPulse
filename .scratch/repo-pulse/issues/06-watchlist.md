# 05 — Watchlist filter

**What to build:** A pure function `filter_starred(repos) -> list[repo]` that drops archived and inactive repos from the user's starred list. Cutoff is `pushed_at > now-12mo` (configurable via `recent_months`).

**Blocked by:** 02 — Config

**Status:** ready-for-agent

- [ ] `filter_starred(repos, recent_months=12, now=None) -> list[dict]`
- [ ] `now` defaults to `datetime.now(UTC)` but is overridable for tests
- [ ] Archived repos (`archived == True`) are dropped
- [ ] Repos with `pushed_at` older than `recent_months` ago are dropped
- [ ] Repos with missing `pushed_at` are KEPT (defensive default — incomplete data, not a signal of inactivity)
- [ ] Disabled repos are dropped (mirror of archived, GitHub marks these as deleted in practice)
- [ ] Function is pure: no IO, no DB, no logging
- [ ] Tests cover boundary cases: exactly 12mo, 12mo+1s, 11mo+30d, missing pushed_at, archived=true, archived=false, disabled=true
- [ ] Tests use frozen `datetime` instances; no real time dependency
