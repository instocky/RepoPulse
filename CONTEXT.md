# Repo-Pulse

A personal research tool that records the daily pulse of GitHub repositories
the user has starred. Source: GitHub REST API. Storage: SQLite. Cadence: daily.

## Language

**Repository**:
A GitHub repository, identified by the pair (owner, name). The unit of
monitoring; one row of interest per Repository in a Snapshot.
_Avoid_: Repo, project, package — the last two are ambiguous here (a "project"
can be a GitHub Project, a "package" can be a registry artifact).

**Starred List**:
The authenticated user's GitHub starred repositories, fetched at the start of
each run via `GET /user/starred`. The raw input, before filtering.
_Avoid_: Stars, my-stars, favorites.

**Watchlist**:
The set of Repositories currently in active monitoring. Computed each run by
filtering the Starred List.
_Avoid_: Monitor list, target list, tracked set.

**Snapshot**:
A single point-in-time reading of one Repository, persisted on a daily
cadence. One row in the snapshots table per (Repository, date).
_Avoid_: Sample, reading, scrape.

**Viral Event**:
A Snapshot in which a Repository's star count grew by at least 20% relative
to the previous Snapshot for the same Repository. Surfaced in the daily
HTML report as a "viral events" section.
_Avoid_: Spike, surge, breakout.
