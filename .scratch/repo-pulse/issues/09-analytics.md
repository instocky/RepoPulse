# 09 — Analytics + DTOs

**What to build:** The Analytics layer with typed DTOs and SQL queries. The Dashboard's only public contract — never returns raw SQLite rows, never leaks storage types. Owns the leaderboard strategy dispatch.

**Blocked by:** 07 — Storage

**Status:** ready-for-agent

- [ ] DTOs are `@dataclass(frozen=True, slots=True)`: `LeaderboardEntry(full_name, current_stars, today_delta, 7d_delta, 30d_delta)`, `TrendPoint(date, value, kind)`, `ViralEvent(full_name, date, absolute_delta, relative_growth)`, `RepositoryDetails(full_name, current_snapshot, history, recent_viral)`
- [ ] `slots=True` is non-negotiable — it gives immutable, memory-efficient value objects that can be hashed and compared
- [ ] `get_leaderboard(kind: str) -> list[LeaderboardEntry]` dispatches via the `LEADERBOARDS` registry
- [ ] `get_repo_details(owner, name) -> RepositoryDetails | None`
- [ ] `get_trend(full_name, kind: Literal["stars","forks","open_issues"]) -> list[TrendPoint]`
- [ ] `get_viral_events(from_date, to_date, *, config) -> list[ViralEvent]` — viral is computed here at read time using `viral.detect_viral` against consecutive snapshots
- [ ] `get_dormant_repos(limit=20) -> list[LeaderboardEntry]` (oldest `pushed_at` among watchlisted)
- [ ] `get_recently_released(limit=20) -> list[LeaderboardEntry]` (most recent `latest_release_at` among watchlisted)
- [ ] `get_repos_by_topic(topic: str) -> list[LeaderboardEntry]` — uses JOIN on `repository_topics`, not JSON parsing
- [ ] `LEADERBOARDS` registry is typed `dict[str, LeaderboardStrategy]` where `LeaderboardStrategy` is a `Protocol` with signature `(Database, Config) -> list[LeaderboardEntry]` — gives IDE/mypy checking on each registered strategy
- [ ] Adding a new kind means: (1) write a function matching `LeaderboardStrategy`, (2) add it to the `LEADERBOARDS` dict — no `if/elif` chain in the route handler
- [ ] All 8 leaderboard kinds from spec are implemented: `current`, `today`, `7d`, `30d`, `forks`, `releases`, `dormant`, `viral`
- [ ] Unknown `kind` raises `ValueError`
- [ ] Analytics depends only on `db` and `config`; never on `web` or `charts` (per 00-architecture doctrine)
- [ ] Tests seed in-memory DB with synthetic snapshots covering 30+ days and 10+ repos
- [ ] Tests assert on DTO shape (attribute equality, hashability); never on dict keys or row positions
- [ ] Tests cover each leaderboard kind against known seed data
- [ ] Respects 00-architecture doctrine (Analytics is a leaf above Storage; no upward imports)
