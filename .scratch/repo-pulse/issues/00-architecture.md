# 00 — Architecture doctrine

**What to build:** A one-page doctrine that codifies the import rules of the
Repo-Pulse codebase. This is the *check-list for code review* — every PR
should be greppable against these rules. The doctrine is enforced
mechanically by `tests/test_architecture.py`.

**Blocked by:** None — must be agreed before ticket 01 starts.

**Status:** ready-for-agent

## Layer boundaries

```
       GitHub REST API
              │
              ▼
   ┌──────────────────┐
   │    Collector     │  ← only writer of snapshots
   └──────────────────┘
              │
              ▼
   ┌──────────────────┐
   │   Storage        │  ← raw SQLite, no analytics queries
   │   (SQLite)       │
   └──────────────────┘
              │
              ▼
   ┌──────────────────┐
   │    Analytics     │  ← only reader of raw data
   └──────────────────┘
              │
              ▼
   ┌──────────────────┐
   │   Charts         │  ← spec builder, no IO
   └──────────────────┘
              │
              ▼
   ┌──────────────────┐
   │   Web            │  ← only consumer of Analytics + Charts
   └──────────────────┘
```

## Permission matrix

| Layer       | Can read from | Can write to | Network |
|-------------|---------------|--------------|---------|
| `collector` | `db`          | `db`         | `gh`    |
| `db`        | own tables    | own tables   | (none)  |
| `analytics` | `db`          | (none)       | (none)  |
| `charts`    | (none)        | (none)       | (none)  |
| `web`       | `analytics`   | (none)       | (none)  |
| `config`    | env + toml    | (none)       | (none)  |
| `lock`      | (filesystem)  | (filesystem) | (none)  |

## Forbidden imports (enforce in code review and in CI)

- `web` → `db` ❌ — Web must go through Analytics
- `web` → `gh` ❌ — Web never talks to GitHub
- `analytics` → `gh` ❌ — Analytics is offline
- `analytics` → `web` ❌ — no upward imports
- `charts` → `db` ❌ — Charts is a pure spec builder
- `charts` → `web` ❌ — no upward imports
- `charts` → `analytics` ❌ — Charts takes DTOs, not Analytics objects
- `collector` → `analytics` ❌ — Collector writes raw, Analytics reads
- `db` → `gh` ❌ — db is pure storage
- `gh` → `collector` ❌ — gh is a leaf primitive (closes asymmetric gap from ticket 05; the original doctrine listed `db → gh` but not the symmetric `gh → db`, which let an accidental `from repo_pulse.db import X` slip past the enforcer in `gh/__init__.py`).
- `gh` → `db` ❌ — gh is a leaf primitive
- `gh` → `analytics` ❌ — gh is a leaf primitive
- `gh` → `charts` ❌ — gh is a leaf primitive
- `gh` → `web` ❌ — gh is a leaf primitive
- `lock` → anything except `os` and `pathlib` ❌ — lock is a leaf primitive.
  The AST enforcer also permits `from __future__ import ...` (language-level
  feature, not a runtime dependency); the enforcer's test documents the
  rationale. Update the doctrine and the enforcer together if more carve-outs
  are ever needed.

The allowed direction is strictly downward in the diagram. A lower layer
imports a higher layer only to satisfy type hints for the *boundary*
types (DTOs, primitives) — never for behaviour.

## Enforcement

A `tests/test_architecture.py` walks the AST of every module under
`src/repo_pulse/` and asserts that the forbidden imports are absent.
The test is ~30 lines, runs in <100ms, fails the build (and CI) on
violation. This is the cheapest possible way to keep the layers honest
across 13 future tickets.

## Acceptance criteria

- [ ] This file exists at `.scratch/repo-pulse/issues/00-architecture.md`
- [ ] `tests/test_architecture.py` exists and passes
- [ ] The test fails when a forbidden import is added (regression net)
- [ ] Every ticket in `issues/` ends with: "Respects 00-architecture doctrine"
- [ ] This file is referenced in the project `README.md` under "Architecture"
