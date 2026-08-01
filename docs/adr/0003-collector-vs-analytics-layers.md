# 0003 — Collector and Analytics are separate layers

The Repo-Pulse codebase is split into two layers with no shared
imports beyond the SQLite schema:

- **Collector** (`collector` module + `gh` + `watchlist` + `db`
  writes). The only thing that talks to GitHub. Runs daily.
- **Analytics** (`analytics` module + `db` reads + `viral` +
  `charts`). Pure SQL queries over the snapshot history. Runs on
  every Dashboard request.

This split is what makes features like Top Movers, Rising Projects,
Dormant Repositories, Weekly/Monthly Report, AI Categories, and
Release Heatmap cheap to add: they're SQL queries, not new GitHub
traffic. The 5000/hr GitHub rate limit stays uncontested as the
dashboard grows.

Reversing this decision later (e.g. computing analytics inside the
Collector) would mean every new metric either requires a new GitHub
call (wasteful) or a recomputation of all snapshots (expensive).
Doing it right now is a one-time architectural cost.

The Dashboard reads only from Analytics, never from `db` directly —
this is what keeps the Analytics layer a clean SQL surface that can
be tested without mocking anything.
