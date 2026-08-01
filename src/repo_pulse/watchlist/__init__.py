"""Watchlist filter for Repo-Pulse.

The contract lives in ``.scratch/repo-pulse/issues/06-watchlist.md`` and
is enforced as the ticket specifies: ``filter_starred`` is a pure
function over a list of GitHub-style repo dicts, returning the subset
that is still "active" by the criteria in ``docs/SPEC.md §Watchlist
lifecycle`` (``archived = false AND pushed_at > now - recent_months``,
plus a defensive default for missing ``pushed_at``). The function is
the only consumer of the GitHub "starred" payload in the Collector
path — it runs after ``gh.fetch_starred`` and before any DB write, so
its output is exactly the set of repos the Collector will snapshot
today.

This is a leaf primitive: per the 00-architecture doctrine the
``watchlist`` layer may not import any other ``repo_pulse`` submodule
(including its own). The boundary is enforced by
``tests/test_architecture.py``. The implementation lives in this
``__init__.py`` (not a separate submodule) for the same reason as
``repo_pulse.lock`` — keeping the public surface
(``from repo_pulse.watchlist import filter_starred``) unchanged while
satisfying the AST enforcer.
"""
from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any

__all__ = ["filter_starred"]


def _utc_now() -> datetime:
    """Return the current time in UTC.

    A module-level indirection so tests can pin the "current
    time" with a single ``monkeypatch.setattr`` call. Without
    this seam, ``datetime.now`` would have to be patched via
    ``unittest.mock.patch`` on the immutable C-level
    ``datetime.datetime`` class — which fails with
    ``TypeError: cannot set 'now' attribute of immutable type``
    on the monkeypatch teardown. The helper exists for
    testability only; production behaviour is unchanged.
    """
    return datetime.now(UTC)


def _parse_pushed_at(value: Any) -> datetime | None:
    """Parse GitHub's ``pushed_at`` ISO-8601 string into a UTC datetime.

    Returns ``None`` for missing, empty, non-string, or unparseable
    values. The "unparseable" case is deliberately treated the same
    as "missing" so the watchlist filter's defensive default
    (incomplete data is not a signal of inactivity) covers both
    edges. A repo whose ``pushed_at`` came back as ``""`` or
    ``None`` from a malformed upstream response must not be silently
    dropped from the watchlist — the next Collector run can refill
    the gap from a working API.

    Naive datetimes (no ``tzinfo``) are coerced to UTC. The cutoff
    is computed in UTC, and comparing a naive datetime to it would
    raise ``TypeError`` on Python 3.11+ — picking UTC for the naive
    case keeps the function total (no raises) on whatever shape
    GitHub returns.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _subtract_months(now: datetime, months: int) -> datetime:
    """Calendar-aware "N months ago" — clamps the day to the new month length.

    ``timedelta(days=30 * months)`` is wrong: 12 * 30 = 360 days, not
    12 calendar months. The watchlist filter needs calendar-accurate
    subtraction so the "exactly 12mo" test case lands on the same
    calendar date the user thinks in (a repo pushed on 2025-08-01 is
    exactly 12 months before 2026-08-01, not 365 days before it).

    Day clamping follows the standard convention: subtracting a
    month from Jan 31 yields Feb 28 (or Feb 29 in a leap year), not
    "Feb 31" which would be normalized to "Mar 3" by Python. Without
    the clamp, ``now.replace(day=31, month=2)`` raises ``ValueError``
    on non-leap years and silently rolls forward on Python's
    datetime constructor otherwise.

    Negative ``months`` is not used by the filter (``recent_months``
    defaults to 12 and is rejected below zero in ``filter_starred``)
    so this helper does not pin a behaviour for that case.
    """
    if months < 0:
        raise ValueError("months must be non-negative")
    total = now.year * 12 + (now.month - 1) - months
    new_year, month_zero = divmod(total, 12)
    new_month = month_zero + 1
    # ``calendar.monthrange`` returns ``(weekday_of_first, days_in_month)``.
    # Clamping the day to ``days_in_month`` is what "subtract a month
    # from Jan 31" means in plain English — Feb 28/29, not Mar 3.
    max_day = calendar.monthrange(new_year, new_month)[1]
    new_day = min(now.day, max_day)
    return now.replace(year=new_year, month=new_month, day=new_day)


def filter_starred(
    repos: list[dict[str, Any]],
    *,
    recent_months: int = 12,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the subset of ``repos`` that is still "active" by the SPEC.

    Drops a repo if **any** of the following holds:

    * ``archived`` is truthy (``True`` / ``1`` / any non-zero value
      GitHub might emit in a future API version).
    * ``disabled`` is truthy (mirror of ``archived`` — GitHub marks
      these as deleted in practice and a disabled repo will never
      produce a new snapshot).
    * ``pushed_at`` parses to a datetime that is **not** strictly
      greater than ``now - recent_months`` (calendar months). The
      boundary is exclusive: a repo pushed at exactly the cutoff
      is dropped.

    A repo is **kept** if ``pushed_at`` is missing, ``None``, empty,
    or unparseable. The ticket pins this as a defensive default
    — incomplete data is not a signal of inactivity, and a
    freshly-starred repo whose upstream response lost the field
    must not be silently dropped on the first day it enters the
    watchlist.

    Parameters
    ----------
    repos:
        The output of ``gh.fetch_starred()`` — a list of GitHub-style
        repo dicts. Non-dict entries are skipped (defensive: the
        function is total on the shape the Collector actually
        produces, but never raises on a malformed upstream).
    recent_months:
        The "active" cutoff, in calendar months. Defaults to 12 per
        the SPEC. The Collector reads this from
        ``config.toml [filter].recent_months`` and passes it
        explicitly so this function does not need to know about
        ``config``.
    now:
        Override the current time. ``None`` (the default) uses
        ``datetime.now(timezone.utc)``. The parameter exists so
        tests can pin the boundary at a frozen instant — a real
        ``datetime.now`` call inside the filter would make the
        "exactly 12mo" boundary non-deterministic.

    Returns
    -------
    list[dict[str, Any]]
        A new list containing only the active repos, in the same
        order as the input. The repo dicts are the same references
        (no deep copy) — the function does not mutate them. This is
        the same contract as ``filter`` builtins: "pure" means no
        side effects, not "returns new objects".
    """
    if recent_months < 0:
        raise ValueError("recent_months must be non-negative")
    if now is None:
        now = _utc_now()
    cutoff = _subtract_months(now, recent_months)

    kept: list[dict[str, Any]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            # Defensive: the Collector always passes a list of dicts
            # from ``gh.fetch_starred``, but a malformed upstream
            # (or a future refactor that mixes in another iterable)
            # should not crash the entire filter. Skipping a
            # non-dict entry preserves the "no per-repo failure
            # aborts the run" invariant from the gh layer.
            continue
        if bool(repo.get("archived")):
            continue
        if bool(repo.get("disabled")):
            continue
        pushed_at = _parse_pushed_at(repo.get("pushed_at"))
        if pushed_at is None:
            # Defensive default per the ticket. Keep the repo —
            # incomplete data is not a signal of inactivity.
            kept.append(repo)
            continue
        if pushed_at > cutoff:
            kept.append(repo)
    return kept
