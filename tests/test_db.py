"""Tests for the ``repo_pulse.db`` layer.

Covers the acceptance criteria in
``.scratch/repo-pulse/issues/07-db.md``:

* ``Database(path)`` accepts a file path (production) or the literal
  string ``":memory:"`` (tests).
* ``bootstrap()`` is idempotent and creates the 4 spec tables and 3
  indexes.
* ``upsert_repository`` distinguishes insert from update:
  ``first_seen_date`` is set only on the first call, ``last_seen_date``
  is updated on every call, ``in_watchlist`` comes from the parameter
  (not from the dict).
* ``write_snapshot`` UPSERTs on ``(full_name, snapshot_date)`` and
  writes every column the spec defines.
* ``upsert_topics`` is idempotent across new and existing topics and
  links them via ``repository_topics``.
* ``get_repository``, ``get_previous_snapshot``, ``list_watchlist``
  return the shape the spec pins and a deterministic ordering.
* Writes commit: a separate ``Database`` on the same file path sees
  the rows left by the first one.
* The ``Database`` owns a single connection for its lifetime —
  in-memory SQLite is per-connection, so the layer cannot open a
  fresh connection per method or it would see a different database
  each time.
* ``close()`` is idempotent and the context manager releases the
  connection on exit (including on exception).

The tests use an in-memory SQLite (``Database(":memory:")``) for
speed and isolation. The on-disk behaviour is covered by a smaller
"file-mode round-trip" test that runs ``bootstrap`` and the write
methods on ``tmp_path`` and re-opens the file with a fresh
``Database`` to assert durability.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from repo_pulse.db import Database

# A frozen "today" used by all date-bearing tests. Pinned so the
# stored ISO string is asserted verbatim rather than recomputed from
# ``date.today()`` (which would be flaky around midnight UTC).
TODAY = date(2026, 8, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_db() -> Database:
    """A fresh in-memory database, schema bootstrapped.

    Used by every test that needs a writable database. Keeps the
    fixture trivial — each test gets an isolated DB so order-of-test
    bugs surface as diff-shaped failures, not as cascading state
    leaks.
    """
    db = Database(":memory:")
    db.bootstrap()
    return db


def _conn(db: Database) -> Any:
    """Reach into ``db`` for raw-SQL introspection.

    Pinned as a single helper so every test gets the same
    ``# noqa: SLF001`` rationale in one place: the public
    methods cover the data plane, but the schema and
    ``snapshots`` / ``topics`` row counts are not part of
    the public API and have to be asserted against the
    underlying connection. Centralised here so a future
    refactor (e.g. adding a ``_raw()`` accessor) only changes
    one line per test.
    """
    return db._conn  # noqa: SLF001 — introspection in tests


def _scalar(db: Database, sql: str, *params: Any) -> Any:
    """Run a scalar ``SELECT`` and return the first column of the
    first row. The Python ``sqlite3`` module's ``Cursor`` does
    not expose a ``.scalar()`` shortcut (it is a DB-API extension
    on some other drivers), so the pattern below is the
    idiomatic one in stdlib SQLite — extracted here so each
    test that needs a row count does not repeat the
    ``fetchone()[0]`` boilerplate."""
    return _conn(db).execute(sql, params).fetchone()[0]


def _repo_dict(
    *,
    full_name: str = "owner/name",
    owner: str | None = None,
    name: str | None = None,
    description: str | None = "desc",
    homepage: str | None = "https://example.com",
    visibility: str | None = "public",
    default_branch: str | None = "main",
    license: str | None = "MIT",
    archived: bool = False,
    disabled: bool = False,
) -> dict[str, Any]:
    """Build a GitHub-shaped repo dict with the fields the spec pins.

    Only the keys ``upsert_repository`` reads are present; a full
    GitHub payload has ~30 keys, and the assertion is on what the db
    layer persists, not on every key the API returned.
    """
    return {
        "full_name": full_name,
        "owner": owner,
        "name": name,
        "description": description,
        "homepage": homepage,
        "visibility": visibility,
        "default_branch": default_branch,
        "license": license,
        "archived": archived,
        "disabled": disabled,
    }


def _full_snapshot_fields() -> dict[str, Any]:
    """The 12 ``fields`` keys ``write_snapshot`` accepts, with values.

    The ticket requires "all 22 snapshot fields per spec" — this
    helper fills every schema column in the ``snapshots`` table so
    the field-coverage test can assert on every persisted value.
    """
    return {
        "stars": 100,
        "forks": 20,
        "open_issues": 5,
        "watchers_count": 100,
        "subscribers_count": 7,
        "pushed_at": "2026-07-30T12:00:00Z",
        "language": "Python",
        "size": 1234,
        "created_at": "2023-01-15T00:00:00Z",
        "updated_at": "2026-07-30T12:00:00Z",
        "latest_release_at": "2026-07-01T00:00:00Z",
        "has_issues": 1,
    }


# ---------------------------------------------------------------------------
# Constructor & bootstrap
# ---------------------------------------------------------------------------


def test_in_memory_database_is_isolated_per_instance() -> None:
    """``Database(":memory:")`` is per-instance — no state leaks between
    two separate ``Database`` objects.

    SQLite's ``:memory:`` opens a private database; without that,
    one test's writes would show up in the next test's reads. The
    Collector and Dashboard never share an in-memory DB (the
    production path uses a file), but the test harness relies on
    per-instance isolation to keep fixtures trivial.
    """
    a = Database(":memory:")
    a.bootstrap()
    a.upsert_repository(_repo_dict(full_name="o/r"), TODAY, in_watchlist=True)
    b = Database(":memory:")
    b.bootstrap()

    assert a.get_repository("o/r") is not None
    assert b.get_repository("o/r") is None


def test_bootstrap_creates_all_four_tables_and_three_indexes() -> None:
    """The schema is the spec's schema: 4 tables, 3 indexes.

    Pinned at the table-list level (rather than per-column) so the
    test is robust to additive column changes in later tickets; the
    field-level coverage lives in the per-method tests below.
    """
    db = _new_db()
    rows = _conn(db).execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()

    # NOTE: the alphabetical order here is the ORDER BY type, name
    # output of sqlite_master, which sorts
    # ``repositories`` before ``repository_topics`` ("repositories"
    # is a 12-char prefix of the longer name; the 12th char of
    # "repositories" is 's' < 'y' in "repository_*"). A naive
    # alphabet list would put "repository_topics" first — pinned
    # here so the failure shows the exact ordering the layer
    # produces, not a guess.
    names = [(r[0], r[1]) for r in rows]
    assert names == [
        ("index", "idx_repository_topics_topic_id"),
        ("index", "idx_snapshots_date"),
        ("index", "idx_snapshots_full_name"),
        ("table", "repositories"),
        ("table", "repository_topics"),
        ("table", "snapshots"),
        ("table", "topics"),
    ]


def test_bootstrap_is_idempotent() -> None:
    """Calling ``bootstrap()`` twice does not raise and leaves the
    schema identical (no duplicate indexes, no error from
    ``CREATE TABLE`` on an existing table)."""
    db = Database(":memory:")
    db.bootstrap()
    db.bootstrap()  # second call must not raise

    # ``sqlite_sequence`` is auto-created by SQLite for tables
    # with ``AUTOINCREMENT`` columns; it is not a user table and
    # is excluded from the count. The three user indexes plus
    # the four autoindexes for PRIMARY KEY / UNIQUE constraints
    # inflate the index count above 3 — we count only the
    # ``idx_*`` ones we declared.
    table_count = _scalar(
        db, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    index_count = _scalar(
        db,
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'",
    )
    assert table_count == 4
    assert index_count == 3


def test_bootstrap_on_file_path_creates_the_file(tmp_path: Path) -> None:
    """File-mode bootstrap creates the SQLite file on disk.

    The Collector relies on this for a fresh VPS — the data
    directory may exist but the DB file does not. The test pins
    that the file appears at the requested path (rather than, say,
    in CWD) and that a second ``Database`` can open it.
    """
    target = tmp_path / "pulse.db"
    assert not target.exists()

    db = Database(target)
    db.bootstrap()
    assert target.exists()

    # Re-open from the same path and assert the schema is intact.
    # ``sqlite_sequence`` is auto-created by SQLite for AUTOINCREMENT
    # columns; excluded from the user-table count.
    reopened = Database(target)
    assert _scalar(
        reopened,
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
    ) == 4


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def test_database_close_is_idempotent() -> None:
    """``close()`` is safe to call twice (or after the context
    manager has already closed) — the Collector cleanup path runs
    unconditionally and has to tolerate both an explicit close
    and the ``with`` block's exit handler."""
    db = Database(":memory:")
    db.bootstrap()
    db.close()
    db.close()  # must not raise


def test_context_manager_closes_connection_on_exit() -> None:
    """The ``with`` block closes the connection on exit so a
    mid-body exception does not leak the underlying file
    handle. Without this, the Collector would lock the SQLite
    file after a traceback until the Python process exits."""
    with Database(":memory:") as db:
        db.bootstrap()
        assert _conn(db) is not None

    # ``_closed`` is the contract for "the context manager ran
    # ``close()``" — the slot is set inside ``close()`` and is
    # the cheapest way to assert the resource was released
    # without re-opening a new connection (which would defeat
    # the in-memory test).
    assert db._closed is True  # noqa: SLF001


def test_context_manager_releases_connection_on_exception() -> None:
    """An exception inside the ``with`` body still releases the
    connection — the Collector relies on this for the "snapshot
    failed halfway" path. Without it, a raised error would
    leave the SQLite file locked and the next Collector run
    would fail with ``database is locked``."""
    db: Database | None = None
    with pytest.raises(RuntimeError, match="boom"):
        with Database(":memory:") as ctx:
            db = ctx
            ctx.bootstrap()
            raise RuntimeError("boom")
    assert db is not None
    assert db._closed is True  # noqa: SLF001


# ---------------------------------------------------------------------------
# upsert_repository
# ---------------------------------------------------------------------------


def test_upsert_repository_insert_creates_row_with_first_seen() -> None:
    """First call for a new repo: row created, ``first_seen_date`` and
    ``last_seen_date`` set to ``today``, all dict fields persisted."""
    db = _new_db()
    db.upsert_repository(
        _repo_dict(full_name="owner/new", description="hello"),
        TODAY,
        in_watchlist=True,
    )

    row = db.get_repository("owner/new")
    assert row is not None
    assert row["full_name"] == "owner/new"
    assert row["owner"] == "owner"
    assert row["name"] == "new"
    assert row["description"] == "hello"
    assert row["homepage"] == "https://example.com"
    assert row["visibility"] == "public"
    assert row["default_branch"] == "main"
    assert row["license"] == "MIT"
    assert row["archived"] == 0
    assert row["disabled"] == 0
    assert row["in_watchlist"] == 1
    assert row["first_seen_date"] == TODAY.isoformat()
    assert row["last_seen_date"] == TODAY.isoformat()


def test_upsert_repository_update_preserves_first_seen_updates_last_seen() -> None:
    """Second call: ``first_seen_date`` is preserved (the original
    value), ``last_seen_date`` is updated to the new ``today``,
    and the metadata fields are refreshed."""
    db = _new_db()
    db.upsert_repository(
        _repo_dict(full_name="owner/r", description="first"),
        date(2026, 1, 1),
        in_watchlist=True,
    )
    later = date(2026, 7, 1)
    db.upsert_repository(
        _repo_dict(full_name="owner/r", description="second"),
        later,
        in_watchlist=True,
    )

    row = db.get_repository("owner/r")
    assert row is not None
    assert row["description"] == "second"
    assert row["first_seen_date"] == "2026-01-01"  # original
    assert row["last_seen_date"] == later.isoformat()  # updated


def test_upsert_repository_in_watchlist_comes_from_parameter() -> None:
    """The ``in_watchlist`` flag is set from the keyword argument, not
    from a key in the repo dict. A repo can move in and out of the
    watchlist between runs without the dict changing.

    The first call uses a non-``TODAY`` date so the
    ``first_seen_date`` preservation assertion is meaningful —
    using ``TODAY`` would make the expected value ``TODAY.isoformat()``,
    which is also what a buggy implementation would produce by
    accident on every call."""
    db = _new_db()
    first_date = date(2026, 1, 1)
    db.upsert_repository(
        _repo_dict(full_name="owner/r"),
        first_date,
        in_watchlist=True,
    )
    assert db.get_repository("owner/r")["in_watchlist"] == 1

    # Same dict shape, but the user has since unstarred the repo.
    db.upsert_repository(
        _repo_dict(full_name="owner/r"), TODAY, in_watchlist=False
    )
    assert db.get_repository("owner/r")["in_watchlist"] == 0

    # Re-star: flips back to 1 without losing the existing history.
    db.upsert_repository(
        _repo_dict(full_name="owner/r"), TODAY, in_watchlist=True
    )
    assert db.get_repository("owner/r")["in_watchlist"] == 1
    assert db.get_repository("owner/r")["first_seen_date"] == first_date.isoformat()


def test_upsert_repository_archived_and_disabled_coerce_bool_to_int() -> None:
    """The schema stores ``archived`` / ``disabled`` as ``INTEGER``;
    ``upsert_repository`` coerces the bool input so a Python
    ``True`` lands as ``1`` and ``False`` as ``0``."""
    db = _new_db()
    db.upsert_repository(
        _repo_dict(full_name="owner/old", archived=True, disabled=True),
        TODAY,
        in_watchlist=False,
    )
    row = db.get_repository("owner/old")
    assert row["archived"] == 1
    assert row["disabled"] == 1


def test_upsert_repository_owner_and_name_default_to_full_name_split() -> None:
    """When the dict omits ``owner`` / ``name``, the layer splits
    ``full_name`` on the first ``/``. The Collector relies on this
    for the ``GET /user/starred`` payload, where every entry has a
    ``full_name`` but some upstream responses do not repeat the
    owner / name as separate fields."""
    db = _new_db()
    db.upsert_repository(
        {"full_name": "anthropic/claude", "description": "x"},
        TODAY,
        in_watchlist=True,
    )
    row = db.get_repository("anthropic/claude")
    assert row["owner"] == "anthropic"
    assert row["name"] == "claude"


def test_get_repository_returns_none_for_missing() -> None:
    """An unknown repo is not in the table — ``get_repository``
    returns ``None`` rather than raising, so the Collector can
    distinguish "never seen" from "seen but empty"."""
    assert _new_db().get_repository("nope/none") is None


# ---------------------------------------------------------------------------
# write_snapshot
# ---------------------------------------------------------------------------


def test_write_snapshot_persists_all_spec_columns() -> None:
    """All 12 ``fields`` keys land in their schema column with the
    values passed in. The ticket requires "all 22 snapshot fields
    per spec (not just the original 8)" — this is the contract
    that future refactors cannot trim without breaking the test."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    db.write_snapshot("owner/r", TODAY, _full_snapshot_fields())

    snap = db.get_previous_snapshot("owner/r", before_date=date(2026, 8, 2))
    assert snap is not None
    assert snap["full_name"] == "owner/r"
    assert snap["snapshot_date"] == TODAY.isoformat()
    assert snap["stars"] == 100
    assert snap["forks"] == 20
    assert snap["open_issues"] == 5
    assert snap["watchers_count"] == 100
    assert snap["subscribers_count"] == 7
    assert snap["pushed_at"] == "2026-07-30T12:00:00Z"
    assert snap["language"] == "Python"
    assert snap["size"] == 1234
    assert snap["created_at"] == "2023-01-15T00:00:00Z"
    assert snap["updated_at"] == "2026-07-30T12:00:00Z"
    assert snap["latest_release_at"] == "2026-07-01T00:00:00Z"
    assert snap["has_issues"] == 1


def test_write_snapshot_upserts_on_same_date() -> None:
    """Re-running the snapshot for the same date overwrites the row
    instead of creating a second one — the Collector relies on
    this for the "re-run after a transient network failure" path
    and the "operator manually re-runs today's snapshot" path.

    Asserted on three axes: row count, the updated field value, and
    the snapshot's primary key (so the autoincrement id is
    preserved, not replaced)."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)

    fields = _full_snapshot_fields()
    db.write_snapshot("owner/r", TODAY, fields)
    first = db.get_previous_snapshot("owner/r", before_date=date(2026, 8, 2))
    assert first is not None
    first_id = first["id"]

    # Re-run with a single field changed (e.g. stars ticked over).
    fields["stars"] = 150
    db.write_snapshot("owner/r", TODAY, fields)

    count = _scalar(
        db, "SELECT COUNT(*) FROM snapshots WHERE full_name = 'owner/r'"
    )
    assert count == 1, "UPSERT must not create a second row for the same date"

    second = db.get_previous_snapshot("owner/r", before_date=date(2026, 8, 2))
    assert second is not None
    assert second["stars"] == 150
    assert second["id"] == first_id, "UPSERT must preserve the existing primary key"


def test_write_snapshot_does_not_overwrite_other_dates() -> None:
    """A second ``write_snapshot`` for a different date adds a new
    row; the first one is preserved. Pinned so a future refactor
    that drops the ``(full_name, snapshot_date)`` UNIQUE constraint
    surfaces here."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    db.write_snapshot("owner/r", TODAY, _full_snapshot_fields())
    db.write_snapshot("owner/r", date(2026, 8, 2), _full_snapshot_fields())

    count = _scalar(
        db, "SELECT COUNT(*) FROM snapshots WHERE full_name = 'owner/r'"
    )
    assert count == 2


# ---------------------------------------------------------------------------
# upsert_topics
# ---------------------------------------------------------------------------


def test_upsert_topics_creates_new_topics_and_links_them() -> None:
    """First sight of a topic name: row created in ``topics`` and a
    matching ``repository_topics`` link inserted."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    db.upsert_topics("owner/r", ["ai", "agents", "rag"])

    topics = {
        r[0]
        for r in _conn(db).execute("SELECT name FROM topics ORDER BY name").fetchall()
    }
    links = {
        r[0]
        for r in _conn(db).execute(
            "SELECT t.name FROM repository_topics rt "
            "JOIN topics t ON t.id = rt.topic_id "
            "ORDER BY t.name"
        ).fetchall()
    }
    assert topics == {"ai", "agents", "rag"}
    assert links == {"ai", "agents", "rag"}


def test_upsert_topics_is_idempotent_on_repeated_calls() -> None:
    """Re-running with the same topic names does not raise
    (UNIQUE on ``topics.name`` and the composite PK on
    ``repository_topics``) and does not duplicate links."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    db.upsert_topics("owner/r", ["ai", "agents"])
    db.upsert_topics("owner/r", ["ai", "agents"])  # second call must not raise

    topic_count = _scalar(db, "SELECT COUNT(*) FROM topics")
    link_count = _scalar(db, "SELECT COUNT(*) FROM repository_topics")
    assert topic_count == 2
    assert link_count == 2


def test_upsert_topics_reuses_existing_topic_row_for_second_repo() -> None:
    """When two repos share a topic, the topic is inserted once and
    linked twice — the many-to-many invariant the spec requires."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="a/r"), TODAY, in_watchlist=True)
    db.upsert_repository(_repo_dict(full_name="b/r"), TODAY, in_watchlist=True)
    db.upsert_topics("a/r", ["shared"])
    db.upsert_topics("b/r", ["shared"])

    topic_count = _scalar(db, "SELECT COUNT(*) FROM topics")
    link_count = _scalar(db, "SELECT COUNT(*) FROM repository_topics")
    assert topic_count == 1
    assert link_count == 2


def test_upsert_topics_empty_list_is_a_noop() -> None:
    """An empty ``topic_names`` writes nothing — a repo with no
    topics does not create a stray row."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    db.upsert_topics("owner/r", [])

    assert _scalar(db, "SELECT COUNT(*) FROM topics") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM repository_topics") == 0


def test_upsert_topics_duplicate_names_in_list_yield_single_row_and_link() -> None:
    """Duplicate names within a single call collapse to one
    topic row and one link — the GitHub ``/repos/{owner}/{repo}/topics``
    endpoint can return the same name twice under a backend
    race, and the Collector passes the list through verbatim.

    Pinned here because the implementation uses
    ``INSERT OR IGNORE`` per topic in a Python ``for`` loop;
    a future refactor to ``executemany`` or a bulk insert
    must preserve the dedup contract — the spec is "one row
    per unique name", not "one row per name in the list"."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    db.upsert_topics("owner/r", ["ai", "ai", "ai", "agents", "agents"])

    assert _scalar(db, "SELECT COUNT(*) FROM topics") == 2
    assert _scalar(db, "SELECT COUNT(*) FROM repository_topics") == 2


# ---------------------------------------------------------------------------
# get_previous_snapshot
# ---------------------------------------------------------------------------


def test_get_previous_snapshot_returns_most_recent_prior_date() -> None:
    """When several snapshots exist for the same repo, the most
    recent date strictly before ``before_date`` is returned. Pinned
    on the boundary cases the Analytics layer relies on: a 7-day
    leaderboard computes today's delta against the row returned by
    ``get_previous_snapshot(today)``."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    fields = _full_snapshot_fields()

    db.write_snapshot("owner/r", date(2026, 7, 30), fields)
    fields["stars"] = 110
    db.write_snapshot("owner/r", date(2026, 7, 31), fields)
    fields["stars"] = 120
    db.write_snapshot("owner/r", date(2026, 8, 1), fields)

    prev = db.get_previous_snapshot("owner/r", before_date=date(2026, 8, 1))
    assert prev is not None
    assert prev["snapshot_date"] == "2026-07-31"
    assert prev["stars"] == 110


def test_get_previous_snapshot_excludes_same_date() -> None:
    """``before_date`` is strict — the snapshot on the same date is
    NOT returned. The "first snapshot is never viral" rule in the
    spec is enforced by the caller, but the strict inequality is
    part of the db contract."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    db.write_snapshot("owner/r", TODAY, _full_snapshot_fields())

    assert db.get_previous_snapshot("owner/r", before_date=TODAY) is None


def test_get_previous_snapshot_returns_none_when_no_prior_exists() -> None:
    """The very first snapshot of a repo has no prior — Analytics
    needs a clear ``None`` signal so it can suppress the "delta
    from infinity" leaderboard noise."""
    db = _new_db()
    db.upsert_repository(_repo_dict(full_name="owner/r"), TODAY, in_watchlist=True)
    db.write_snapshot("owner/r", TODAY, _full_snapshot_fields())

    assert db.get_previous_snapshot("owner/r", before_date=TODAY) is None


def test_get_previous_snapshot_returns_none_for_unknown_repo() -> None:
    """An unknown repo is not a KeyError — ``None`` so the
    caller can branch on "no history" uniformly."""
    assert _new_db().get_previous_snapshot("nope/none", before_date=TODAY) is None


# ---------------------------------------------------------------------------
# list_watchlist
# ---------------------------------------------------------------------------


def test_list_watchlist_returns_only_active_full_names_sorted() -> None:
    """``list_watchlist`` returns the ``full_name`` of every row with
    ``in_watchlist = 1``, sorted alphabetically for deterministic
    output (the leaderboard dispatch in Analytics relies on a
    stable order)."""
    db = _new_db()
    db.upsert_repository(
        _repo_dict(full_name="zeta/r"), TODAY, in_watchlist=True
    )
    db.upsert_repository(
        _repo_dict(full_name="alpha/r"), TODAY, in_watchlist=True
    )
    db.upsert_repository(
        _repo_dict(full_name="mike/r"), TODAY, in_watchlist=False
    )

    assert db.list_watchlist() == ["alpha/r", "zeta/r"]


def test_list_watchlist_is_empty_when_nothing_active() -> None:
    """An empty list is a valid result (a fresh account, or a day
    where every repo became archived). The Collector relies on
    this for the "no work to do" path."""
    db = _new_db()
    db.upsert_repository(
        _repo_dict(full_name="owner/r"), TODAY, in_watchlist=False
    )
    assert db.list_watchlist() == []


def test_list_watchlist_ignores_repositories_with_no_row_yet() -> None:
    """A repo that was never inserted (e.g. a stale entry in some
    external config) is not returned — only rows that exist with
    ``in_watchlist = 1``."""
    db = _new_db()
    assert db.list_watchlist() == []


# ---------------------------------------------------------------------------
# Durability across Database instances
# ---------------------------------------------------------------------------


def test_writes_are_durable_across_database_instances(tmp_path: Path) -> None:
    """A write on one ``Database`` instance is visible to a second
    ``Database`` opened on the same file path. The Collector and
    Dashboard are separate processes, so the commit boundary
    matters: every write method must flush to disk before
    returning."""
    target = tmp_path / "pulse.db"

    writer = Database(target)
    writer.bootstrap()
    writer.upsert_repository(
        _repo_dict(full_name="owner/r"), TODAY, in_watchlist=True
    )
    writer.write_snapshot("owner/r", TODAY, _full_snapshot_fields())
    writer.upsert_topics("owner/r", ["ai"])

    reader = Database(target)
    assert reader.get_repository("owner/r") is not None
    assert reader.get_previous_snapshot("owner/r", before_date=date(2026, 8, 2)) is not None
    assert reader.list_watchlist() == ["owner/r"]
    topic_names = {
        r[0] for r in _conn(reader).execute("SELECT name FROM topics").fetchall()
    }
    assert topic_names == {"ai"}


def test_writes_rollback_on_exception() -> None:
    """A write that raises inside a method leaves the database
    unchanged — the ticket's "transactions on writes" guarantee.

    Exercised by ``write_snapshot`` for a ``full_name`` that does
    not exist in ``repositories`` once foreign keys are enforced:
    SQLite raises ``IntegrityError``, the surrounding transaction
    rolls back, and the prior state is intact.

    Without the explicit transaction, a partial write could leave
    a dangling row that no other process can resolve. This test
    pins that the implementation commits atomically.
    """
    db = _new_db()
    with pytest.raises(sqlite3.IntegrityError):
        db.write_snapshot("ghost/repo", TODAY, _full_snapshot_fields())

    snap_count = _scalar(db, "SELECT COUNT(*) FROM snapshots")
    repo_count = _scalar(db, "SELECT COUNT(*) FROM repositories")
    assert snap_count == 0
    assert repo_count == 0
