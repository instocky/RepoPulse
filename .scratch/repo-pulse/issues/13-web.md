# 12 — Web (Dashboard)

**What to build:** A FastAPI app with 5 routes, Jinja2 templates for each, and a shared `base.html`. The Dashboard reads from Analytics only — never from `db` directly. Charts are built via the Chart spec builder, rendered via `PlotlyRenderer`.

**Blocked by:** 08 — Analytics + DTOs, 11 — Charts, 02 — Config

**Status:** ready-for-agent

- [ ] `GET /` — index: today's leaderboard (top 20 by current stars), viral events in last 24h, "by category" small chart
- [ ] `GET /repo/{owner}/{name}` — repo detail: trend chart (stars over time), current snapshot, recent viral events
- [ ] `GET /viral` — viral events archive, paginated by week
- [ ] `GET /category/{topic}` — repos in watchlist with the given topic, ranked by current stars
- [ ] `GET /leaderboard/{kind}` — dispatches via the `LEADERBOARDS` registry; unknown kind returns 404
- [ ] All routes return 200 with rendered HTML (404 for missing repo/topic)
- [ ] Templates use a shared `base.html` with header, nav, footer
- [ ] Static files (CSS) served from `/static`
- [ ] uvicorn host/port come from `config.web_host` and `config.web_port`
- [ ] The Dashboard never imports `db` directly — only `analytics`
- [ ] Tests use `fastapi.testclient.TestClient` with in-memory DB
- [ ] Tests assert on HTML structure (presence of expected `<h1>`, chart div, etc.) — not on internal calls
- [ ] Tests cover: each route happy path, 404 for missing repo, 404 for unknown leaderboard kind, empty DB
- [ ] No JS framework — just Plotly CDN + a few inline scripts for interactivity
