# Repo-Pulse — Spec

## Problem Statement

A Tech Lead tracks the AI ecosystem by starring GitHub repositories — they
have roughly 400–700 starred repos across AI tooling, agents, RAG frameworks,
and adjacent communities. They want to monitor the *pulse* of this set —
growth trends, viral breakouts, release cadence — over time, without
manually curating a watch list or maintaining a separate tool.

The current options all have gaps: Repohistory is GitHub-App-bound and
oriented at "your own" repos; Star History has been throttled by GitHub;
RepoStars only compares up to 5 repos and is read-only. A custom
~300-line script could do exactly what is needed, but deploying and
maintaining it across "I forget about it for a month" intervals on a VPS
turns out to be its own problem: a one-time CLI is not the same as a
"set and forget" daily service with a remote viewable dashboard.

## Solution

Repo-Pulse is a Python tool organized as two decoupled layers:

- A **Collector** that fetches fresh data from the GitHub REST API once
  per day and writes raw snapshots to a local SQLite database. The
  Collector is the only component that talks to GitHub.
- An **Analytics** layer that derives rankings, trends, and viral events
  from the snapshot history — purely via SQL. No network IO, no GitHub
  traffic.

A **Dashboard** (FastAPI + Jinja2 templates, with a swappable Chart
Renderer abstraction — Plotly.js in v0) reads from Analytics and
renders a server-rendered web view, exposed on a subdomain with
HTTP Basic Auth.

This split is deliberate. Future analytics features (Top Movers,
Rising Projects, Dormant Repositories, Weekly/Monthly Report, AI
Categories, Release Heatmap) become SQL queries, not new GitHub
traffic. The 5000/hr GitHub rate limit stays uncontested as the
dashboard grows.

## User Stories

1. As a user, I want to run `python -m repo_pulse snapshot` from the CLI
   to take today's snapshot of my starred repos on demand.
2. As a user, I want the snapshot source to be `GET /user/starred` so
   that my watchlist is auto-derived from what I actually star — no list
   to maintain.
3. As a user, I want archived and inactive repos to be filtered out of
   the watchlist so the dashboard isn't cluttered with dead projects.
4. As a user, I want each snapshot to record a comprehensive set of
   GitHub fields (stars, forks, open_issues, watchers_count,
   subscribers_count, pushed_at, language, size, license, archived,
   disabled, default_branch, visibility, has_issues, created_at,
   updated_at, latest_release_at, description, homepage, topics), so
   I can ask research questions months from now without re-fetching.
5. As a user, I want API errors (429, 5xx) retried with exponential
   backoff and skipped silently, so a single bad repo does not abort
   the run.
6. As a user, I want viral events to require **both** a relative
   threshold (default 20% stars/day) **and** an absolute threshold
   (default +100 stars/day), so that +20% on a 10-star repo doesn't
   pollute the viral feed.
7. As a user, I want snapshots stored forever in SQLite, so I can
   study long-term trends and revisit repos I have since unstarred.
8. As a user, I want a daily leaderboard on the dashboard, with
   multiple rankings: current stars, today, 7d, 30d, fork growth,
   recently released, dormant, all-time viral.
9. As a user, I want a per-repo detail page with a trend chart, so I
   can study individual repos in depth.
10. As a user, I want the dashboard protected by HTTP Basic Auth, so
    only I can see it from the open Internet.
11. As a user, I want the system deployed on my VPS via systemd (one
    timer for the Collector, one service for the Dashboard), so it
    runs daily without me logging in.
12. As a user, I want the dashboard reachable over HTTPS at a subdomain
    I control, so I can view it from any browser.
13. As a user, I want each run to leave a structured log, so I can
    debug failures after the fact.
14. As a user, I want the system to acquire a lock at the start of each
    run, so a slow snapshot cannot overlap with the next scheduled one.
15. As a user, I want repos that leave my watchlist (I unstar them) to
    keep their existing history but stop receiving new snapshots, so my
    research data outlives my current interest.
16. As a user, I want to inspect raw data with the `sqlite3` CLI, so I
    can answer ad-hoc research questions without exporting anything.
17. As a user, I want the project to use `uv` for dependency management,
    so the project is portable and reproducible.
18. As a user, I want the snapshot pipeline to be testable in isolation
    with mocked HTTP, so I can refactor without breaking the live
    deployment.
19. As a user, I want the Collector to be the only component that talks
    to GitHub, so adding new analytics features never increases my API
    usage.
20. As a user, I want a "Rising Projects" view (repos that are growing
    fast but aren't yet big), so I can spot emerging tools.
21. As a user, I want a "Recently Released" view, so I can track
    release cadence of my watchlist.
22. As a user, I want a "Dormant Repositories" view, so I can prune my
    watchlist over time.
23. As a user, I want the Chart Renderer to be a swappable abstraction
    (Plotly in v0), so I can switch libraries without rewriting the
    Dashboard.
24. As a user, I want topics stored as a normalized many-to-many
    relation, so I can query "repos in topic X sorted by stars" with
    a single SQL JOIN.

## Implementation Decisions

### Architecture

The codebase is split into two layers with no shared imports beyond
the SQLite schema:

```
       GitHub REST API
              │
              ▼
   ┌──────────────────┐
   │    Collector     │  ← only thing that hits GitHub
   │  (daily CLI)     │     rate-limited, retry+skip
   └──────────────────┘
              │
              ▼
   ┌──────────────────┐
   │   Storage        │
   │   (SQLite raw)   │  ← append-only snapshots
   │   repositories   │     in_watchlist flag
   │   snapshots      │     topics normalized
   │   topics,        │
   │   repository_topics
   └──────────────────┘
              │
              ▼
   ┌──────────────────┐
   │    Analytics     │  ← pure SQL, no IO
   │   (Python)       │     leaderboards, trends, viral
   └──────────────────┘
              │
              ▼
   ┌──────────────────┐
   │   Dashboard      │  ← FastAPI + Jinja2
   │  (read-only)     │     Chart Renderer (Plotly in v0)
   └──────────────────┘
              │
              ▼
       https://pulse.example.com
```

Two long-running concerns, two processes:

- **Collector** — CLI command, runs daily via systemd timer. Writes
  raw snapshots and topic relations. The only writer.
- **Dashboard** — FastAPI app, runs always-on via systemd service.
  Reads from Analytics. The only reader.

Both share the SQLite file at a known path.

### Watchlist lifecycle

```
   user stars a repo
        │
        ▼
   Repository enters Watchlist
     in_watchlist = 1
     first_seen_date = today
        │
        ▼
   daily Collector run
     snapshots rows accumulate
     repository row updated with new fields
        │
        ▼
   one of:
     (a) user unstars the repo, OR
     (b) repo becomes archived, OR
     (c) repo's pushed_at drops below now-12mo
        │
        ▼
   in_watchlist = 0
   last_seen_date = last snapshot date
        │
        ▼
   history preserved
     snapshots remain queryable forever
     topics remain linked
```

A repo is "active" (in Watchlist) iff it passes the active filter:
`archived = false AND pushed_at > now-12mo`. The Collector recomputes
the Watchlist on every run; a repo that becomes inactive or is unstarred
gets `in_watchlist = 0` but keeps all past snapshots. Re-starring
(manually by the user) is a fresh entry: `in_watchlist = 1` again,
`first_seen_date` reset.

### Tech stack

- **Python 3.11+** — modern syntax, stable on Linux distros shipped
  with current LTS VPS images.
- **uv** — single tool for venv + dependency management.
- **httpx** — synchronous HTTP client with first-class retry support
  and explicit timeout configuration.
- **typer** — CLI framework with auto-generated help.
- **FastAPI + uvicorn** — web framework and ASGI server.
- **Jinja2** — server-side templates.
- **Plotly.js (CDN)** — in v0, behind a Chart Renderer abstraction.
- **SQLite** — single-file database.

### Modules

- **config** — loads `.env` and `config.toml`. No business logic.
- **gh** — GitHub REST client. Owns authentication, rate-limit
  handling, and retry. Returns typed dicts. No DB access.
- **watchlist** — pure function `filter_starred(repos) -> list[repo]`
  with the active filter (`archived=false` AND `pushed_at > now-12mo`).
- **collector** — orchestration: `run_daily_snapshot(date)` calls
  `gh`, `watchlist`, `db` in order. Top-level error boundary.
  This is the only writer of snapshots.
- **db** — SQLite repository: schema bootstrap, upsert repository,
  write snapshot, write topics. Exposes raw CRUD primitives.
- **analytics** — domain layer. Pure SQL on top of `db`. Returns
  typed DTOs (`LeaderboardEntry`, `TrendPoint`, `ViralEvent`,
  `RepositoryDetails`) — never raw SQLite rows. Owns leaderboard
  strategy dispatch. No IO beyond the DB.
- **viral** — pure function `detect_viral(curr, prev) -> bool`
  enforcing both absolute and relative thresholds. Lives inside
  the Analytics layer; never invoked from the Collector.
- **charts** — Chart spec builder. Each chart type returns a
  `Chart` object (typed data + axis config), not a rendered
  string. The Dashboard converts a `Chart` to HTML via the
  v0 Plotly renderer. Future renderers (PNG, SVG, PDF) plug in
  without touching Analytics.
- **lock** — file-based run lock (`data/.lock`). Acquired at start
  of Collector, released at end. Refuses to start if held.
- **cli** — typer entry point: `snapshot`, `serve`, `status`
  subcommands.
- **web** — FastAPI app, routes, Jinja2 templates. Reads from
  Analytics, never from `db` directly.

### Data model

#### Aggregates

```
Repository (1) ──< (N) Snapshot
Repository (1) ──< (N) repository_topics >── (N) Topic
```

- **Repository** is the persistent identity. Holds current metadata
  (description, default_branch, license, etc.) and the membership
  state (`in_watchlist`, first/last seen dates).
- **Snapshot** is the historical record. Append-only. One row per
  (Repository, date). Carries 22 fields covering the full
  `/repos/{owner}/{repo}` response (minus nested objects we don't
  need day-over-day).
- **Topic** is a normalized many-to-many. New topics are inserted
  on first sight; linking happens via `repository_topics`.

#### Schema

```sql
CREATE TABLE repositories (
  full_name TEXT PRIMARY KEY,         -- "owner/name"
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  in_watchlist INTEGER NOT NULL,      -- 1 = active, 0 = unstarred/inactive
  first_seen_date TEXT NOT NULL,
  last_seen_date TEXT NOT NULL,
  description TEXT,
  homepage TEXT,
  visibility TEXT,
  default_branch TEXT,
  license TEXT,
  archived INTEGER NOT NULL DEFAULT 0,
  disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name TEXT NOT NULL,
  snapshot_date TEXT NOT NULL,        -- ISO date, e.g. "2026-08-01"
  stars INTEGER NOT NULL,
  forks INTEGER NOT NULL,
  open_issues INTEGER NOT NULL,
  watchers_count INTEGER,
  subscribers_count INTEGER,
  pushed_at TEXT,
  language TEXT,
  size INTEGER,
  created_at TEXT,
  updated_at TEXT,
  latest_release_at TEXT,
  has_issues INTEGER,
  FOREIGN KEY (full_name) REFERENCES repositories(full_name),
  UNIQUE (full_name, snapshot_date)
);

CREATE TABLE topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE repository_topics (
  repository_full_name TEXT NOT NULL,
  topic_id INTEGER NOT NULL,
  PRIMARY KEY (repository_full_name, topic_id),
  FOREIGN KEY (repository_full_name) REFERENCES repositories(full_name),
  FOREIGN KEY (topic_id) REFERENCES topics(id)
);

CREATE INDEX idx_snapshots_full_name ON snapshots(full_name);
CREATE INDEX idx_snapshots_date ON snapshots(snapshot_date);
CREATE INDEX idx_repository_topics_topic_id ON repository_topics(topic_id);
```

The `repositories` table holds the current state; the `snapshots`
table holds the history. Topics are normalized so a single SQL JOIN
answers "repos in topic X sorted by stars" without JSON parsing.

#### Data Transfer Objects (DTOs)

The Analytics layer's public contract is a small set of typed
DTOs. The Dashboard consumes these; SQLite rows never cross the
layer boundary. This keeps the storage backend replaceable
(SQLite today, DuckDB or PostgreSQL tomorrow) without touching
the Dashboard.

- `LeaderboardEntry(full_name, current_stars, today_delta, 7d_delta, 30d_delta)`
- `TrendPoint(date, value, kind)` where `kind ∈ {"stars", "forks", "open_issues"}`
- `ViralEvent(full_name, date, absolute_delta, relative_growth)`
- `RepositoryDetails(full_name, current_snapshot, history: list[TrendPoint], recent_viral: list[ViralEvent])`

All DTOs are immutable (`frozen=True` dataclasses). Analytics
functions return them, never dicts and never raw rows.

### Viral event

A Snapshot is "viral" iff **both**:

- `stars_delta >= min_absolute_delta` (default `100`), AND
- `relative_growth >= min_relative_growth` (default `0.20`)

A 10→12 snapshot (+20% but only +2) is not viral. A 1000→1100 (+10%
but +100) is not viral. A 1000→1200 (+20% AND +200) is viral.

The first snapshot of a repository is never viral (no prior to
compare). A 404 on a previous snapshot is logged and the current
is treated as not viral (no prior to compute against).

Both thresholds live in `config.toml`.

Viral status is **computed by Analytics at read time** and never
persisted on the snapshot row. If the threshold changes (e.g. from
20% to 15%), no re-collection of historical data is needed — the
same snapshots produce a different viral feed against the new
threshold.

### Chart Renderer

Charts go through a `repo_pulse.charts` abstraction that returns
a `Chart` spec, not a rendered string:

```python
@dataclass
class Chart:
    title: str
    kind: Literal["line", "bar", "heatmap"]
    data: ChartData
    layout: ChartLayout

def build_line(series: list[TimeSeries], title: str) -> Chart: ...
def build_bar(categories: list[BarItem], title: str) -> Chart: ...
def build_heatmap(matrix: list[list[int]], title: str) -> Chart: ...
```

The Dashboard takes a `Chart` and asks a `Renderer` to turn it into
HTML (v0: Plotly.js, `include_plotlyjs='cdn'`). Future renderers can
target PNG, SVG, or PDF without touching the Analytics layer — only
the Renderer changes. The `Chart` shape is the public contract.

### Web routes

All routes are server-rendered Jinja2 templates. No JSON API surface
in v0.

- `GET /` — index. Today's leaderboard (top 20 by current stars),
  viral events in the last 24h, small "by category" chart.
- `GET /repo/{owner}/{name}` — repo detail. Trend chart (stars over
  time), current snapshot, recent viral events.
- `GET /viral` — viral events archive, paginated by week.
- `GET /category/{topic}` — repos in watchlist with the given topic,
  ranked by current stars.
- `GET /leaderboard/{kind}` — explicit leaderboards, with `kind` in:
  - `current` — by current stars
  - `today` — by stars gained today
  - `7d` — by stars gained in last 7 days
  - `30d` — by stars gained in last 30 days
  - `forks` — by fork growth
  - `releases` — most recently released (top by `latest_release_at`)
  - `dormant` — least active (top by oldest `pushed_at`)
  - `viral` — all-time viral events, sorted by relative growth

Leaderboards are dispatched via a **strategy pattern**: a
`LEADERBOARDS` registry keyed by `kind` maps to a function that
returns `list[LeaderboardEntry]`. The route handler is a thin
shim that looks up the strategy by name. Adding a new ranking
means adding a new function to the registry — no `if/elif` chain
grows in the route.

### Deployment

- **Project location** — `/opt/repo-pulse/` on the VPS.
- **Data** — `data/pulse.db` and `data/.lock` inside the project.
  Gitignored.
- **Logs** — `logs/YYYY-MM-DD.log` inside the project. Rotated by an
  external logrotate (out of scope).
- **`.env`** — at project root, mode `0600`, contains `GITHUB_TOKEN`,
  `BASIC_AUTH_USER`, `BASIC_AUTH_PASS`. Not in git.
- **systemd units** — three files:
  - `repo-pulse-snapshot.service` (oneshot)
  - `repo-pulse-snapshot.timer` (daily at 06:00 UTC)
  - `repo-pulse-web.service` (always-on, restarts on failure)
- **nginx** — listens on 443, TLS via certbot, Basic Auth via
  `auth_basic` and `auth_basic_user_file`, proxies to `localhost:8000`.

### Auth

HTTP Basic Auth at the nginx layer. The password never enters the
Python process. Pros: zero auth code in the application, the only
attack surface is the nginx config and htpasswd file.

### Error handling

Per-request retry policy in the `gh` module: on `429` or `5xx`,
exponential backoff (3 attempts, 1s/2s/4s). On `4xx` (notably `404`),
fail-fast — the repo was likely deleted or made private. A failed
request logs a structured warning and is skipped; the rest of the
Collector run continues. The Collector itself never aborts on a
per-repo error.

### Configurability

A `config.toml` exposes:

```toml
[filter]
recent_months = 12

[viral]
min_absolute_delta = 100
min_relative_growth = 0.20

[paths]
data_dir = "data"
reports_dir = "reports"
```

A `.env` exposes:

```bash
GITHUB_TOKEN=ghp_xxx
BASIC_AUTH_USER=user
BASIC_AUTH_PASS=pass
```

## Testing Decisions

### What makes a good test

- Test **external behavior**, not internal calls. Assert on data shape
  (rows, attributes) and HTTP responses, not on which functions were
  invoked.
- **Boundary inputs** matter most for pure logic. Filter, viral
  detection, and date math get explicit boundary tests (0%, exactly
  20%, 20.01%, 100% growth; 0 repos, 1 repo, 500 repos).
- **Mock at the network boundary**, not at internal module boundaries.
  Use `httpx.MockTransport` or `respx` to fake GitHub; never mock
  internal repo-pulse modules from within repo-pulse tests.

### What gets tested

- `watchlist` — pure, no IO. Full coverage of the filter (archived,
  pushed_at, missing fields, edge cases).
- `viral` — pure, no IO. Boundary inputs (no previous snapshot,
  exactly at absolute, exactly at relative, infinite growth, zero
  growth, negative growth).
- `db` — uses an in-memory SQLite. Tests schema bootstrap, write
  idempotency (re-running the same date), watchlist membership
  transitions, topic upsert.
- `gh` — uses `respx` to mock httpx. Tests 200, 404, 429-then-200,
  500-then-200, network timeout, pagination, headers.
- `collector` — uses mocked `gh` and in-memory `db`. One happy-path
  test, one test with 2 skipped repos, one test that exercises the
  lock file.
- `analytics` — uses an in-memory SQLite seeded with synthetic
  snapshots. Tests leaderboard, viral, trends, dormant, category
  queries against known data. Pure SQL, no mocks.
- `charts` — golden-file test: assert rendered HTML/JS string is
  well-formed for known inputs.
- `web` — uses `fastapi.testclient.TestClient` against an in-memory
  DB. Asserts on HTML response structure, not on internal calls.

### E2E smoke

A single e2e test that:

1. Spins up a TestClient.
2. Runs the Collector against a recorded fixture (respx-recorded).
3. Asserts on the resulting DB rows.
4. Renders `/`, `/repo/{owner}/{name}`, `/viral`,
   `/leaderboard/viral` and asserts the expected sections are
   present.

This test is the regression net for "the whole thing still works."

## Out of Scope

- **Multi-user support.** One user, one GitHub identity, one PAT.
- **OAuth / GitHub-login flow.** HTTP Basic Auth at nginx is enough.
- **Historical backfill.** The DB only knows about days after the
  system was installed. No GitHub Archive, no GitHub Star History API.
- **Notifications.** No email, no webhook, no push on viral events.
  The user visits the dashboard.
- **Topic-based discovery.** Only the user's starred list is used.
  No `topic:llm` searches, no auto-discovery.
- **Custom dashboards / configurable charts per user.** The chart
  library and style are global; the user can swap implementations
  via the Chart Renderer abstraction.
- **Backup / restore tooling.** The user is responsible for backing
  up the SQLite file. A cron job pushing the DB to S3 is a separate
  project.
- **Auto-update of the deployed app.** The user pulls and restarts
  services when they want to update.
- **Rate-limit budget tracking.** We rely on the standard 5000/hr
  authenticated limit; we do not implement conditional request
  caching (ETag) or pre-flight budget checks.
- **Public API / JSON endpoints.** Server-rendered HTML only.

## Further Notes

This is v0 — the minimum that delivers the dashboard the user wants.
Future iterations may add: notifications, ETag-based caching, public
API, multi-user support, and topic discovery.

The system is intentionally a single-tenant personal tool. The 5000/hr
GitHub rate limit is far above the ~250–400 requests/day the Collector
needs, so a multi-user version would not even need rate-limit
fundamentally rethought — it would need proper auth and isolation.

The "set and forget" deployment is a one-time install: `git clone`,
`uv sync`, copy `.env`, `systemctl enable --now`. After that, the
user only ssh-es in to upgrade or debug.
