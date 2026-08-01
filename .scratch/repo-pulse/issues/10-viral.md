# 09 — Viral detection

**What to build:** A pure function `detect_viral(curr_snapshot, prev_snapshot, *, config) -> bool` that returns True iff **both** `stars_delta >= min_absolute_delta` AND `relative_growth >= min_relative_growth`. Both thresholds come from config. Lives inside the Analytics layer — never invoked by the Collector.

**Blocked by:** 08 — Analytics + DTOs

**Status:** ready-for-agent

- [ ] `detect_viral(curr: dict, prev: dict | None, *, config) -> bool`
- [ ] Returns False when `prev is None` (first snapshot, nothing to compare)
- [ ] Returns False when `stars_delta < config.min_absolute_delta`
- [ ] Returns False when `relative_growth < config.min_relative_growth`
- [ ] Returns True only when both conditions are met
- [ ] Boundary tests: `stars_delta = 99` → False; `stars_delta = 100` → depends on relative; `stars_delta = 100` AND `relative = 0.199` → False; `stars_delta = 100` AND `relative = 0.20` → True
- [ ] Edge cases: zero stars in prev (no division by zero — return False), negative growth (stars decreased), zero growth
- [ ] Pure: no IO, no DB, no logging
- [ ] The Collector never imports or calls `detect_viral` (per ADR 0003)
- [ ] Viral status is computed at read time in `analytics.get_viral_events` — never persisted on the snapshot row
- [ ] If config thresholds change, re-reading the same DB produces a different viral feed (no re-collection)


Respects 00-architecture doctrine
