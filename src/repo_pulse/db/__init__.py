"""SQLite storage layer for Repo-Pulse.

The contract lives in ``.scratch/repo-pulse/issues/07-db.md`` and
is enforced as the ticket specifies: ``Database`` is a thin
repository over the four spec tables (``repositories``,
``snapshots``, ``topics``, ``repository_topics``) plus three
indexes, exposing typed write methods the Collector calls once
per repository per day and read methods the Analytics layer calls
in its read-only queries.

Layer placement
---------------
``db`` is the raw-storage layer. Per the 00-architecture doctrine
it may not import any other ``repo_pulse`` submodule (including
its own) — the boundary is enforced by
``tests/test_architecture.py``. The implementation lives in this
``__init__.py`` (not a separate submodule) for the same reason as
``repo_pulse.lock`` and ``repo_pulse.watchlist``: keeping the
public surface (``from repo_pulse.db import Database``) unchanged
while satisfying the AST enforcer, which forbids
``from .. import db`` from sibling layers (closed in the
``test_enforcer_catches_relative_import_layer_only`` regression
net).

Connection lifecycle
--------------------
A ``Database`` owns a single ``sqlite3.Connection`` for its
lifetime. This is the only way to make in-memory SQLite
(``Database(":memory:")``) usable: SQLite's ``:memory:`` is a
per-connection private store, so opening a new connection after
``bootstrap()`` would return an empty database. Keeping one
connection per ``Database`` instance is also the standard advice
for the file-backed path (avoids per-method ``connect`` /
``close`` overhead and means ``PRAGMA foreign_keys = ON`` is set
once instead of on every call).

Use the database as a context manager so the connection is
always closed, even on error:

.. code-block:: python

    with Database(config.data_dir / "pulse.db") as db:
        db.bootstrap()
        db.upsert_repository(...)

The ``test_database_close_is_idempotent`` test pins that
``close()`` is safe to call twice — the Collector cleanup path
runs unconditionally and has to tolerate a double-close.

Write semantics
---------------
Every write method is wrapped in a single transaction: the writes
commit atomically or roll back on any error. The
``test_writes_rollback_on_exception`` test pins this — a
deliberately-bad ``write_snapshot`` (referencing a repo that does
not exist with ``PRAGMA foreign_keys = ON``) raises
``IntegrityError`` and leaves the database state identical to
before the call. The Collector and the Dashboard are separate
processes, so the commit boundary matters: a partial write
would surface as a dangling row in the next Collector run.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Final, cast

__all__ = ["Database"]

# SQLite's special connection name for an in-memory database.
# Tests pass this string directly to ``Database(":memory:")``;
# ``sqlite3.connect`` recognises it as the per-connection private
# memory store. Defined as a module constant so the boundary check
# in ``_open_connection`` is a single equality test, not a magic
# string sprinkled across the implementation.
_MEMORY_SENTINEL: Final[str] = ":memory:"

# DDL for the four spec tables and the three indexes. Kept as a
# module constant so ``bootstrap()`` is a single execute script
# and the schema is greppable from one place. The DDL matches
# ``docs/SPEC.md §Schema`` verbatim; the field-coverage test in
# ``tests/test_db.py`` pins every column this layer writes.
_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS repositories (
    full_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    in_watchlist INTEGER NOT NULL,
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

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS repository_topics (
    repository_full_name TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    PRIMARY KEY (repository_full_name, topic_id),
    FOREIGN KEY (repository_full_name) REFERENCES repositories(full_name),
    FOREIGN KEY (topic_id) REFERENCES topics(id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_full_name ON snapshots(full_name);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_repository_topics_topic_id ON repository_topics(topic_id);
"""


class Database:
    """A thin repository over the spec's SQLite schema.

    The class is intentionally minimal: it owns the connection
    lifecycle, exposes one method per spec operation, and does no
    business logic. The Analytics layer composes higher-level
    queries on top of these primitives — this class never joins
    across ``snapshots`` and ``repositories`` for an aggregate
    answer, and never inspects ``in_watchlist`` to filter a list
    (the watchlist filter lives in Analytics).

    Parameters
    ----------
    path:
        Either a ``Path`` to a SQLite file (production), or the
        literal string ``":memory:"`` (tests). The string form is
        a SQLite convention for a private in-memory database; the
        spec calls it out so a test can spin up a fresh DB per
        fixture without touching the filesystem. Any other string
        is treated as a file path (delegated to ``sqlite3.connect``
        which interprets the argument as a path on every
        supported platform).
    """

    __slots__ = ("_target", "_conn", "_closed")

    def __init__(self, path: Path | str) -> None:
        self._target: Path | str = path
        self._conn: sqlite3.Connection = self._open_connection()
        # Tracks whether ``close()`` has already been called so
        # the context manager's ``__exit__`` and an explicit
        # ``close()`` can coexist without a double-close raising
        # ``ProgrammingError: Cannot operate on a closed
        # database``. The ``test_database_close_is_idempotent``
        # test pins this.
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Create the schema idempotently.

        All DDL uses ``IF NOT EXISTS``; calling ``bootstrap()``
        twice on the same database is a no-op (the
        ``test_bootstrap_is_idempotent`` test pins this). The
        schema matches ``docs/SPEC.md §Schema`` exactly — every
        column, every index — so the field-coverage test in
        ``tests/test_db.py`` is the contract this method honours.

        Foreign keys are enabled per-connection in
        ``_open_connection``, not here, because
        ``PRAGMA foreign_keys`` is a connection-level setting in
        SQLite (it does not persist across ``connect`` calls on
        a file-backed DB).
        """
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def upsert_repository(
        self,
        repo: dict[str, Any],
        today: date,
        *,
        in_watchlist: bool,
    ) -> None:
        """Insert or update a single repository row.

        On the first call for a ``full_name`` the row is created
        with ``first_seen_date = last_seen_date = today`` and
        ``in_watchlist`` from the parameter. On subsequent calls
        every metadata field is refreshed, ``last_seen_date`` is
        set to the new ``today``, and ``first_seen_date`` is
        preserved — the ticket's "first_seen only on first
        insert" rule.

        The ``in_watchlist`` flag is set from the keyword
        argument rather than the dict so the Collector can
        toggle a repo's membership without rebuilding the full
        GitHub payload. A repo that was unstarred and re-starred
        keeps its original ``first_seen_date`` — the ticket does
        not ask for a reset on re-entry, and the spec's
        "history preserved" rule says the original date is the
        audit-relevant one.

        Parameters
        ----------
        repo:
            A GitHub-shaped dict. ``full_name`` is required; the
            other keys (``owner``, ``name``, ``description``,
            ``homepage``, ``visibility``, ``default_branch``,
            ``license``, ``archived``, ``disabled``) are
            optional and default to ``None`` / ``False``. When
            ``owner`` / ``name`` are missing they are inferred
            from the first ``/`` in ``full_name`` — the
            ``GET /user/starred`` payload sometimes omits them.
        today:
            The "current" date the Collector is running for.
            Stored as the ISO format string ``YYYY-MM-DD``.
        in_watchlist:
            Whether the repo is in the active watchlist. Comes
            from the parameter, not the dict, so the watchlist
            filter (ticket 06) can drive it independently of the
            GitHub payload.
        """
        full_name = self._require_full_name(repo)
        owner = self._coerce_owner(repo, full_name)
        name = self._coerce_name(repo, full_name)
        archived = self._coerce_bool_int(repo.get("archived", False))
        disabled = self._coerce_bool_int(repo.get("disabled", False))
        today_iso = today.isoformat()

        with self._transaction() as conn:
            # ``INSERT ... ON CONFLICT DO UPDATE`` is SQLite's
            # native UPSERT. The first insert sets
            # ``first_seen_date`` to today; the update branch
            # excludes it from the SET clause so the original
            # value survives. ``last_seen_date`` and
            # ``in_watchlist`` are refreshed on every call.
            conn.execute(
                """
                INSERT INTO repositories (
                    full_name, owner, name, in_watchlist,
                    first_seen_date, last_seen_date,
                    description, homepage, visibility,
                    default_branch, license, archived, disabled
                ) VALUES (
                    :full_name, :owner, :name, :in_watchlist,
                    :first_seen_date, :last_seen_date,
                    :description, :homepage, :visibility,
                    :default_branch, :license, :archived, :disabled
                )
                ON CONFLICT(full_name) DO UPDATE SET
                    owner = excluded.owner,
                    name = excluded.name,
                    in_watchlist = excluded.in_watchlist,
                    last_seen_date = excluded.last_seen_date,
                    description = excluded.description,
                    homepage = excluded.homepage,
                    visibility = excluded.visibility,
                    default_branch = excluded.default_branch,
                    license = excluded.license,
                    archived = excluded.archived,
                    disabled = excluded.disabled
                """,
                {
                    "full_name": full_name,
                    "owner": owner,
                    "name": name,
                    "in_watchlist": 1 if in_watchlist else 0,
                    "first_seen_date": today_iso,
                    "last_seen_date": today_iso,
                    "description": repo.get("description"),
                    "homepage": repo.get("homepage"),
                    "visibility": repo.get("visibility"),
                    "default_branch": repo.get("default_branch"),
                    "license": repo.get("license"),
                    "archived": archived,
                    "disabled": disabled,
                },
            )

    def write_snapshot(
        self,
        full_name: str,
        snapshot_date: date,
        fields: dict[str, Any],
    ) -> None:
        """Insert or update a single day's snapshot for one repo.

        The ``(full_name, snapshot_date)`` UNIQUE constraint makes
        this an UPSERT: re-running the Collector for the same
        date (e.g. after a transient network failure, or a
        manual re-run) overwrites the existing row rather than
        creating a second one. The autoincrement ``id`` is
        preserved on update — the test
        ``test_write_snapshot_upserts_on_same_date`` pins both
        invariants.

        Parameters
        ----------
        full_name:
            The ``owner/name`` of the repository. Must already
            exist in ``repositories`` when foreign keys are
            enforced (the test
            ``test_writes_rollback_on_exception`` covers the
            error path).
        snapshot_date:
            The date the snapshot is for. Stored as ISO
            ``YYYY-MM-DD``.
        fields:
            The 12 ``snapshots`` columns. ``stars``, ``forks``,
            and ``open_issues`` are required (schema
            ``NOT NULL``); the rest are nullable. ``None`` is
            passed through as SQLite NULL — the GitHub API can
            return ``null`` for any of these and the
            ``null_count`` / ``watchers_count`` fields are
            explicitly nullable in the spec schema.
        """
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO snapshots (
                    full_name, snapshot_date,
                    stars, forks, open_issues,
                    watchers_count, subscribers_count,
                    pushed_at, language, size,
                    created_at, updated_at,
                    latest_release_at, has_issues
                ) VALUES (
                    :full_name, :snapshot_date,
                    :stars, :forks, :open_issues,
                    :watchers_count, :subscribers_count,
                    :pushed_at, :language, :size,
                    :created_at, :updated_at,
                    :latest_release_at, :has_issues
                )
                ON CONFLICT(full_name, snapshot_date) DO UPDATE SET
                    stars = excluded.stars,
                    forks = excluded.forks,
                    open_issues = excluded.open_issues,
                    watchers_count = excluded.watchers_count,
                    subscribers_count = excluded.subscribers_count,
                    pushed_at = excluded.pushed_at,
                    language = excluded.language,
                    size = excluded.size,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    latest_release_at = excluded.latest_release_at,
                    has_issues = excluded.has_issues
                """,
                {
                    "full_name": full_name,
                    "snapshot_date": snapshot_date.isoformat(),
                    "stars": fields["stars"],
                    "forks": fields["forks"],
                    "open_issues": fields["open_issues"],
                    "watchers_count": fields.get("watchers_count"),
                    "subscribers_count": fields.get("subscribers_count"),
                    "pushed_at": fields.get("pushed_at"),
                    "language": fields.get("language"),
                    "size": fields.get("size"),
                    "created_at": fields.get("created_at"),
                    "updated_at": fields.get("updated_at"),
                    "latest_release_at": fields.get("latest_release_at"),
                    "has_issues": fields.get("has_issues"),
                },
            )

    def upsert_topics(
        self,
        full_name: str,
        topic_names: list[str],
    ) -> None:
        """Insert any new topic names and link them to ``full_name``.

        Idempotent across new and existing topics:

        * ``INSERT OR IGNORE INTO topics`` ensures the topic row
          is created at most once (UNIQUE on ``topics.name``).
        * ``INSERT OR IGNORE INTO repository_topics`` ensures the
          link is created at most once per (repo, topic) pair
          (composite PK on ``repository_topics``).

        An empty ``topic_names`` list is a no-op — a repo with
        no topics does not create a stray row. A repo that loses
        all its topics between runs (e.g. the user rewrote them
        upstream) is NOT cleared here: the link row is still
        accurate, and the next Collector run will leave it
        alone. Topic reconciliation is a separate ticket's
        concern.

        Parameters
        ----------
        full_name:
            The ``owner/name`` of the repository. Must exist in
            ``repositories`` (FK is enforced when foreign keys
            are enabled).
        topic_names:
            The list of topic names. Duplicates within the list
            collapse silently — SQLite's UNIQUE on
            ``topics.name`` deduplicates, and the second
            ``INSERT OR IGNORE`` for the same name is a no-op.
        """
        if not topic_names:
            return
        with self._transaction() as conn:
            for name in topic_names:
                # ``INSERT OR IGNORE`` returns 0 (no-op) on a
                # UNIQUE conflict; the row's ``id`` is already
                # known to SQLite even when the statement is a
                # no-op, so the follow-up SELECT-by-name
                # resolves to the existing id.
                conn.execute(
                    "INSERT OR IGNORE INTO topics (name) VALUES (?)",
                    (name,),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO repository_topics
                        (repository_full_name, topic_id)
                    SELECT ?, id FROM topics WHERE name = ?
                    """,
                    (full_name, name),
                )

    def get_repository(self, full_name: str) -> dict[str, Any] | None:
        """Return the ``repositories`` row for ``full_name`` as a dict,
        or ``None`` if no such row exists.

        The dict is built from a ``sqlite3.Row`` so the test can
        index by column name (``row["stars"]``) instead of by
        position. ``None`` is the contract for "never seen" so
        the Collector and the Analytics layer can branch on
        "no history" uniformly — the alternative (raising
        ``KeyError``) would force every caller into a
        try/except.
        """
        row = self._conn.execute(
            "SELECT * FROM repositories WHERE full_name = ?",
            (full_name,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_previous_snapshot(
        self,
        full_name: str,
        before_date: date,
    ) -> dict[str, Any] | None:
        """Return the most recent snapshot for ``full_name`` strictly
        before ``before_date``, or ``None`` if no such row exists.

        "Strictly before" is the contract the Analytics layer
        relies on for the "first snapshot is never viral" rule:
        the snapshot on ``before_date`` itself is not the
        previous, and the caller computes a delta against the
        returned row (or suppresses the calculation when the
        return is ``None``).

        Ordered by ``snapshot_date DESC LIMIT 1`` so the
        ``test_get_previous_snapshot_returns_most_recent_prior_date``
        test pins the right row in the multi-snapshot case.
        """
        row = self._conn.execute(
            """
            SELECT * FROM snapshots
            WHERE full_name = ? AND snapshot_date < ?
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            (full_name, before_date.isoformat()),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_watchlist(self) -> list[str]:
        """Return every ``full_name`` with ``in_watchlist = 1``,
        sorted alphabetically for deterministic output.

        The sort is part of the public contract: the leaderboard
        dispatch in Analytics (ticket 09) iterates the watchlist
        in this order, and a non-deterministic order would make
        flaky tests out of any "first repo" assertion.
        """
        rows = self._conn.execute(
            "SELECT full_name FROM repositories "
            "WHERE in_watchlist = 1 "
            "ORDER BY full_name"
        ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        """Close the underlying connection. Idempotent.

        Safe to call without an explicit context manager, safe to
        call twice in a row, safe to call after the database has
        been used inside a ``with`` block (the context manager
        will also call ``close()`` on exit). The Collector
        cleanup path runs unconditionally and has to tolerate
        all three cases.
        """
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Database:
        """Enter the context. Returns ``self`` so the body uses
        the same instance and its already-open connection."""
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Exit the context. Always closes the connection so a
        raised exception does not leak the underlying file
        handle. The Collector relies on this — without it, a
        mid-run traceback would leave the SQLite file locked
        until the Python process exits."""
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_connection(self) -> sqlite3.Connection:
        """Open and configure the single connection this instance
        owns.

        ``sqlite3.Row`` is the row factory so callers can use
        ``row["column_name"]`` and so ``dict(row)`` produces a
        clean dict in the read methods. ``PRAGMA foreign_keys =
        ON`` is set on every connection — SQLite does not
        persist this setting across opens on a file-backed DB,
        so a per-connection call is the only way to honour the
        FK constraints declared in the schema.
        """
        target = ":memory:" if self._target == _MEMORY_SENTINEL else str(self._target)
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block as a single SQLite transaction.

        On normal exit the implicit transaction commits; on any
        exception the transaction rolls back so the database
        state is identical to before the block. This is the
        "transactions on writes" guarantee from the ticket —
        a partial write would surface as a dangling row in the
        next Collector run because the Collector and Dashboard
        are separate processes sharing the SQLite file.
        """
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @staticmethod
    def _require_full_name(repo: dict[str, Any]) -> str:
        """Return ``repo['full_name']`` or raise ``KeyError``.

        The Collector always passes a dict with ``full_name``
        because the field is the primary key of the
        ``repositories`` table. A missing ``full_name`` is a
        programming error upstream, not a runtime data error,
        so ``KeyError`` is the right surface — the caller sees
        a clear traceback instead of a silent skip.
        """
        # ``cast`` strips the ``Any`` from ``dict[str, Any]`` —
        # the dict's value type is ``Any`` because the Collector
        # passes the raw GitHub payload, but ``full_name`` is
        # always a string in practice. Without the cast mypy
        # flags the return as ``Any`` and the function's return
        # annotation breaks.
        return cast(str, repo["full_name"])

    @staticmethod
    def _coerce_owner(repo: dict[str, Any], full_name: str) -> str:
        """Return ``repo['owner']`` if present, else the prefix of
        ``full_name`` before the first ``/``.

        ``GET /user/starred`` returns every entry with a
        ``full_name`` but the owner is sometimes only embedded
        in that field. Splitting on ``/`` is the spec's
        canonical parsing rule and matches the GitHub API
        documentation for the ``full_name`` shape
        (``"{owner}/{name}"``).
        """
        if "owner" in repo and repo["owner"] is not None:
            return str(repo["owner"])
        return full_name.split("/", 1)[0]

    @staticmethod
    def _coerce_name(repo: dict[str, Any], full_name: str) -> str:
        """Return ``repo['name']`` if present, else the suffix of
        ``full_name`` after the first ``/``.

        Symmetric to ``_coerce_owner``: the GitHub API sometimes
        omits the standalone ``name`` field on the
        ``/user/starred`` payload but always populates
        ``full_name``.
        """
        if "name" in repo and repo["name"] is not None:
            return str(repo["name"])
        parts = full_name.split("/", 1)
        return parts[1] if len(parts) == 2 else parts[0]

    @staticmethod
    def _coerce_bool_int(value: Any) -> int:
        """Coerce a Python ``bool`` (or ``0`` / ``1``) to SQLite's integer.

        The schema stores ``archived`` / ``disabled`` as
        ``INTEGER NOT NULL DEFAULT 0``. A Python ``True`` would
        bind to ``1`` automatically (sqlite3 maps ``bool`` to
        ``int``), but unknown shapes would bind to ``NULL`` and
        violate the constraint, or — worse — silently bind to a
        wrong integer and corrupt a downstream filter. ``None``
        is the GitHub-API-returns-null case; everything else is
        an upstream bug and must surface as a clear error.

        The ``isinstance(value, bool)`` check MUST come first:
        ``bool`` is a subclass of ``int`` in Python, so a plain
        ``isinstance(value, int)`` would accept ``True`` / ``False``
        and skip the explicit ``1 if value else 0`` branch —
        harmless here, but a refactor target. The order is
        pinned in the test ``test_upsert_repository_archived_and_disabled_coerce_bool_to_int``.
        """
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, int):
            return value
        if value is None:
            # GitHub's API can return ``null`` for ``archived``
            # / ``disabled`` on a malformed payload. Coerce to 0
            # (the schema default) rather than violating the
            # NOT NULL constraint — losing the "is archived"
            # signal is recoverable, a hard error would abort
            # the Collector run for one bad row.
            return 0
        raise TypeError(
            f"expected bool, int, or None for archived/disabled; "
            f"got {type(value).__name__}: {value!r}"
        )
