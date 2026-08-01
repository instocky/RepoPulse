# Repo-Pulse

A personal daily pulse monitor for the GitHub repositories you've starred.
Collects snapshots via the GitHub REST API, stores them in SQLite, and
serves a read-only dashboard with leaderboards, trend charts, and viral
events.

This is **v0** — the minimum that delivers the dashboard. See
[`docs/SPEC.md`](docs/SPEC.md) for the full specification and
[`.scratch/repo-pulse/issues/`](.scratch/repo-pulse/issues/) for the
implementation ticket breakdown.

## Status

Project scaffold only. The Collector, Analytics, and Dashboard layers
will land in tickets 02–14.

## Quick start

```bash
# Requires Python 3.11+ and uv
uv sync
cp .env.example .env  # then fill in GITHUB_TOKEN
python -c "import repo_pulse; print(repo_pulse.__version__)"
```

## Architecture

The codebase is split into decoupled layers with import rules enforced by
[`tests/test_architecture.py`](tests/test_architecture.py). See
[`.scratch/repo-pulse/issues/00-architecture.md`](.scratch/repo-pulse/issues/00-architecture.md)
for the doctrine:

```
GitHub REST API
       │
       ▼
   Collector        ← only writer of snapshots; the only thing that hits GitHub
       │
       ▼
   Storage (SQLite) ← raw snapshots, no analytics
       │
       ▼
   Analytics        ← pure SQL, no IO
       │
       ▼
   Charts           ← spec builder, no IO
       │
       ▼
   Web (FastAPI)    ← only consumer of Analytics + Charts
```

## License

MIT.
