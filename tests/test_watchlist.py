"""Tests for the ``repo_pulse.watchlist`` layer.

Covers the acceptance criteria in
``.scratch/repo-pulse/issues/06-watchlist.md``:

* ``filter_starred(repos, *, recent_months=12, now=None) -> list[dict]``
* ``now`` defaults to ``datetime.now(UTC)`` but is overridable for tests
* ``archived`` truthy repos are dropped
* ``disabled`` truthy repos are dropped
* ``pushed_at`` older than ``recent_months`` ago is dropped
* missing / empty / unparseable ``pushed_at`` is KEPT (defensive default)
* the function is pure: no IO, no DB, no logging, no input mutation
* the boundary is calendar-month aware (not ``timedelta(days=30*N)``)

The test plan pins "exactly 12mo", "12mo+1s", "11mo+30d" as the
boundary triplet. The "11mo+30d" case is month-length-dependent
(30-day subtraction from the 11-month-ago date lands in different
places for different starting months) — the tests below pick
``now = 2026-08-01`` because August has 31 days, so
``11mo+30d ago == 2025-08-02`` and ``12mo ago == 2025-08-01``,
which lines up cleanly with the "kept vs dropped at the boundary"
distinction the ticket is asking for.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from repo_pulse.watchlist import filter_starred

# A fixed ``now`` used by the boundary tests. Picked to make the
# "11mo+30d" case cleanly KEPT: August has 31 days, so subtracting
# 30 from "11 months ago" (2025-09-01) lands on 2025-08-02, which
# is one day newer than "12 months ago" (2025-08-01) and therefore
# passes the exclusive boundary. The "exactly 12mo" case pins
# 2025-08-01 00:00:00 — a strict ``>`` against the cutoff yields
# False, so the repo is dropped.
FROZEN_NOW = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)

# The "active" cutoff for ``FROZEN_NOW`` with the default 12 months —
# every test below uses this as a sanity check, not a behavioural
# assertion (the filter derives it from ``now`` itself, so the value
# is implicit in the test inputs).
CUTOFF_12MO = datetime(2025, 8, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo(
    *,
    name: str = "owner/name",
    archived: bool = False,
    disabled: bool = False,
    pushed_at: str | None = "2026-07-15T00:00:00Z",
) -> dict[str, Any]:
    """Build a GitHub-shaped repo dict with only the fields the filter reads.

    The full GitHub payload has ~30 keys; the filter only touches
    ``archived``, ``disabled``, and ``pushed_at``. Tests construct
    only what they need — a real-shaped payload here would obscure
    the test intent ("does the filter drop archived repos?") under
    noise ("did I spell the language field right?").
    """
    return {
        "full_name": name,
        "archived": archived,
        "disabled": disabled,
        "pushed_at": pushed_at,
    }


def _kept_names(result: list[dict[str, Any]]) -> list[str]:
    """Return the ``full_name`` of each kept repo, in order.

    Order is the same as input order — the filter preserves order
    (it's a list comprehension, not a set). Tests assert on the
    name list rather than on the dict identity so the assertion
    is readable.
    """
    return [r["full_name"] for r in result]


# ---------------------------------------------------------------------------
# Defaults and signature
# ---------------------------------------------------------------------------


def test_returns_empty_list_for_empty_input() -> None:
    """No repos in → no repos out. The Collector relies on this for
    the "no starred repos" case (a fresh GitHub account, or a
    revoked token — both produce an empty list from gh)."""
    assert filter_starred([], now=FROZEN_NOW) == []


def test_default_recent_months_is_12() -> None:
    """The ticket pins ``recent_months=12`` as the default. A repo
    pushed 11 months ago is kept; a repo pushed 13 months ago is
    dropped — the same boundary as the explicit default."""
    just_kept = _repo(name="kept", pushed_at="2025-09-01T00:00:00Z")
    just_dropped = _repo(name="dropped", pushed_at="2025-06-15T00:00:00Z")

    result = filter_starred([just_kept, just_dropped], now=FROZEN_NOW)

    assert _kept_names(result) == ["kept"]


def test_now_defaults_to_utc_now_but_is_overridable() -> None:
    """``now=None`` uses ``datetime.now(timezone.utc)``; ``now`` can
    be pinned for tests. We don't assert on the live-clock default
    (it would be flaky by definition) — we assert on the contract
    that passing a frozen ``now`` overrides it deterministically."""
    very_old = _repo(name="old", pushed_at="2020-01-01T00:00:00Z")
    recent = _repo(name="recent", pushed_at="2026-07-15T00:00:00Z")

    # With FROZEN_NOW (2026-08-01), the 2020 repo is well past the
    # 12-month cutoff and must be dropped; the 2026 repo is recent
    # and must be kept. If ``now`` were NOT honored, this assertion
    # would flake near the calendar-month boundary on the live clock.
    result = filter_starred([very_old, recent], now=FROZEN_NOW)

    assert _kept_names(result) == ["recent"]


# ---------------------------------------------------------------------------
# archived / disabled — boolean (and truthy) drop
# ---------------------------------------------------------------------------


def test_archived_true_is_dropped() -> None:
    """``archived: true`` → repo leaves the watchlist. Without this,
    the dashboard would surface dead projects forever."""
    archived = _repo(name="dead", archived=True)
    active = _repo(name="alive")

    result = filter_starred([archived, active], now=FROZEN_NOW)

    assert _kept_names(result) == ["alive"]


def test_archived_false_is_kept() -> None:
    """Explicit ``archived: false`` is the same as missing: the repo
    is not archived and stays in the watchlist. Pins the falsey
    branch of the truthy check."""
    active = _repo(name="alive", archived=False)

    result = filter_starred([active], now=FROZEN_NOW)

    assert _kept_names(result) == ["alive"]


def test_disabled_true_is_dropped() -> None:
    """``disabled: true`` → dropped, mirror of ``archived``. GitHub
    marks these as deleted in practice (per the ticket) so they
    will never produce a new snapshot."""
    disabled = _repo(name="gone", disabled=True)
    active = _repo(name="alive")

    result = filter_starred([disabled, active], now=FROZEN_NOW)

    assert _kept_names(result) == ["alive"]


def test_all_archived_and_disabled_flag_combinations_drop_correctly() -> None:
    """Every (archived, disabled) combination in {True, False} is
    handled: any truthy flag drops the repo, and only the all-false
    case survives. Pinned as a single test because the four cases
    are uniform — the individual ``archived`` / ``disabled`` tests
    cover each flag in isolation, and this one pins the union.

    Note on the "precedence" framing this replaces: the function
    checks ``archived`` first then ``disabled`` (or vice versa in
    a future refactor) and the drop outcome is the same — a repo
    with either flag set is dropped. There is no observable
    precedence to pin because the function does not return a
    "reason for the drop"; the older test name promised more than
    the body could observe.
    """
    only_archived = _repo(name="archived", archived=True, disabled=False)
    only_disabled = _repo(name="disabled", archived=False, disabled=True)
    both = _repo(name="both", archived=True, disabled=True)
    neither = _repo(name="neither")

    result = filter_starred(
        [only_archived, only_disabled, both, neither], now=FROZEN_NOW
    )

    assert _kept_names(result) == ["neither"]


def test_truthy_non_boolean_archived_is_dropped() -> None:
    """A defensive truthy check covers ``archived=1`` (an int from
    a future API quirk or a hand-crafted dict in a test). A
    strict ``is True`` would let a ``1`` slip through and the repo
    would never be dropped, polluting the watchlist with a
    supposedly-archived project."""
    archived_int = _repo(name="one", archived=1)  # type: ignore[arg-type]

    result = filter_starred([archived_int], now=FROZEN_NOW)

    assert result == []


# ---------------------------------------------------------------------------
# pushed_at — the calendar-month boundary
# ---------------------------------------------------------------------------


def test_recent_pushed_at_is_kept() -> None:
    """A repo pushed 1 day ago is well within the active window."""
    repo = _repo(name="today", pushed_at="2026-07-31T00:00:00Z")

    result = filter_starred([repo], now=FROZEN_NOW)

    assert _kept_names(result) == ["today"]


def test_old_pushed_at_is_dropped() -> None:
    """A repo pushed 2 years ago is well past the 12-month cutoff."""
    repo = _repo(name="ancient", pushed_at="2024-01-01T00:00:00Z")

    result = filter_starred([repo], now=FROZEN_NOW)

    assert result == []


def test_boundary_exactly_12mo_is_dropped() -> None:
    """``pushed_at == cutoff`` is dropped: the boundary is exclusive
    (``pushed_at > cutoff`` is the active condition). The
    "exactly 12mo" case is the most likely to silently regress
    if a future refactor switches to ``>=``."""
    exactly = _repo(
        name="boundary",
        pushed_at="2025-08-01T00:00:00Z",  # == CUTOFF_12MO
    )

    result = filter_starred([exactly], now=FROZEN_NOW)

    assert result == [], "boundary must be exclusive (pushed_at > cutoff)"


def test_boundary_12mo_plus_1s_is_dropped() -> None:
    """One second past the cutoff is dropped — the most clearly
    "inactive" boundary case. Together with
    ``test_boundary_exactly_12mo_is_dropped`` this pins the
    exclusive boundary from both sides."""
    just_past = _repo(
        name="just-past",
        pushed_at="2025-07-31T23:59:59Z",  # 1s older than CUTOFF_12MO
    )

    result = filter_starred([just_past], now=FROZEN_NOW)

    assert result == []


def test_boundary_11mo_plus_30d_is_kept() -> None:
    """The positive boundary case from the ticket plan. With
    ``FROZEN_NOW = 2026-08-01`` and a 31-day August, 11mo+30d ago
    is 2025-08-02 — one day newer than the 12mo cutoff of
    2025-08-01, so the repo is kept. This case specifically
    catches a refactor that switches to ``timedelta(days=30 * N)``
    (which would give 11mo+30d == 12mo == 2025-08-01 and
    incorrectly drop the repo)."""
    just_inside = _repo(
        name="just-inside",
        pushed_at="2025-08-02T00:00:00Z",  # = 11mo+30d before FROZEN_NOW
    )

    result = filter_starred(
        [just_inside], now=FROZEN_NOW
    )

    assert _kept_names(result) == ["just-inside"], (
        "calendar-aware subtraction expected; "
        f"result={result!r}"
    )


# ---------------------------------------------------------------------------
# pushed_at — defensive default for missing / empty / unparseable
# ---------------------------------------------------------------------------


def test_missing_pushed_at_is_kept() -> None:
    """The ``pushed_at`` key is absent from the dict. Per the
    ticket, this is a defensive default: incomplete data is not
    a signal of inactivity, and a freshly-starred repo whose
    upstream response lost the field must not be silently dropped
    on the first day it enters the watchlist."""
    repo = _repo(name="missing-field")
    repo.pop("pushed_at")

    result = filter_starred([repo], now=FROZEN_NOW)

    assert _kept_names(result) == ["missing-field"]


def test_none_pushed_at_is_kept() -> None:
    """Explicit ``pushed_at: None`` is the same as missing — the
    upstream API sometimes returns ``null`` for very-recently-pushed
    repos during a write race. Kept, per the defensive default."""
    repo = _repo(name="none-value", pushed_at=None)

    result = filter_starred([repo], now=FROZEN_NOW)

    assert _kept_names(result) == ["none-value"]


def test_empty_string_pushed_at_is_kept() -> None:
    """``pushed_at: ""`` is the same as missing — an empty value
    from a malformed upstream. Kept, per the defensive default."""
    repo = _repo(name="empty-string", pushed_at="")

    result = filter_starred([repo], now=FROZEN_NOW)

    assert _kept_names(result) == ["empty-string"]


def test_unparseable_pushed_at_is_kept() -> None:
    """A non-ISO-8601 string in ``pushed_at`` is kept, same logic
    as missing: a malformed upstream response is not a signal of
    inactivity. Without this, a single bad parse would silently
    evict the repo from the watchlist."""
    repo = _repo(name="bad-format", pushed_at="not-a-date")

    result = filter_starred([repo], now=FROZEN_NOW)

    assert _kept_names(result) == ["bad-format"]


def test_naive_pushed_at_is_treated_as_utc() -> None:
    """A naive (no-tzinfo) ``pushed_at`` is interpreted as UTC, not
    local time. Without this, the same timestamp would compare
    differently on a developer laptop in Moscow vs the production
    VPS in UTC — the watchlist membership would depend on the
    machine, not the data. The function is total on the shape
    GitHub returns, so a stray naive datetime must not raise
    ``TypeError`` from a tz-aware vs tz-naive comparison."""
    # 11 months ago, naive. Same moment as FROZEN_NOW - 11mo.
    naive = _repo(
        name="naive",
        pushed_at="2025-09-01T00:00:00",  # no tz suffix
    )

    result = filter_starred([naive], now=FROZEN_NOW)

    assert _kept_names(result) == ["naive"]


# ---------------------------------------------------------------------------
# recent_months — the configurable cutoff
# ---------------------------------------------------------------------------


def test_recent_months_6_uses_six_month_cutoff() -> None:
    """``recent_months=6`` moves the cutoff to ``now - 6mo``. A
    repo pushed 5 months ago is kept; a repo pushed 7 months ago
    is dropped. Pins that the parameter is wired through to the
    cutoff, not hard-coded to 12."""
    five_months_ago = _repo(name="5mo", pushed_at="2026-03-01T00:00:00Z")
    seven_months_ago = _repo(name="7mo", pushed_at="2026-01-01T00:00:00Z")

    result = filter_starred(
        [five_months_ago, seven_months_ago],
        recent_months=6,
        now=FROZEN_NOW,
    )

    assert _kept_names(result) == ["5mo"]


def test_recent_months_0_drops_all_dated_repos() -> None:
    """``recent_months=0`` is a degenerate but valid value: the
    cutoff is ``now``, and any repo with a non-future ``pushed_at``
    is dropped. A future-dated ``pushed_at`` is kept. The point
    of the test is to confirm the parameter is honored at the
    zero boundary, not that someone should ever use it."""
    now_exact = _repo(
        name="exact",
        pushed_at="2026-08-01T00:00:00Z",  # == FROZEN_NOW
    )

    result = filter_starred([now_exact], recent_months=0, now=FROZEN_NOW)

    # Boundary is exclusive; pushed_at == cutoff is dropped.
    assert result == []


def test_recent_months_negative_raises_value_error() -> None:
    """A negative ``recent_months`` is a programming error — the
    Collector never passes it and the SPEC's tunables are non-
    negative. Raise early so a future caller that accidentally
    inverts a sign does not silently keep every repo forever."""
    with pytest.raises(ValueError, match="non-negative"):
        filter_starred([_repo()], recent_months=-1, now=FROZEN_NOW)


# ---------------------------------------------------------------------------
# Purity — no IO, no mutation, total on shape
# ---------------------------------------------------------------------------


def test_function_does_not_mutate_input_list() -> None:
    """The input list is read, never written. The Collector
    passes the GitHub response list in directly and may want to
    inspect it after the filter for debugging; in-place
    modification would be a surprising side effect."""
    input_list = [
        _repo(name="a"),
        _repo(name="b", archived=True),
        _repo(name="c"),
    ]
    snapshot = list(input_list)

    filter_starred(input_list, now=FROZEN_NOW)

    assert input_list == snapshot, "input list was mutated"


def test_function_does_not_mutate_input_dicts() -> None:
    """The repo dicts are passed through by reference — a deep
    copy would be wasted allocation (the Collector only reads
    them downstream) and would also hide any dict-level bugs
    in the filter ("did the field get rewritten?"). Mutation
    is forbidden; the test pins that."""
    repo = _repo(name="alive", pushed_at="2026-07-15T00:00:00Z")
    snapshot = dict(repo)

    filter_starred([repo], now=FROZEN_NOW)

    assert repo == snapshot, "input dict was mutated"


def test_input_list_order_is_preserved_in_output() -> None:
    """Stable order matches the input. The Collector relies on
    this for the "snapshot in the order we received them" log
    line; a sort would re-order the watchlist and make that
    log unreadable."""
    input_list = [
        _repo(name="first"),
        _repo(name="second", archived=True),  # dropped
        _repo(name="third"),
        _repo(name="fourth", pushed_at="2020-01-01T00:00:00Z"),  # dropped
        _repo(name="fifth"),
    ]

    result = filter_starred(input_list, now=FROZEN_NOW)

    assert _kept_names(result) == ["first", "third", "fifth"]


def test_non_dict_entries_are_skipped() -> None:
    """A list with a non-dict entry (e.g. a stray string from a
    future refactor or a malformed upstream) must not crash the
    filter. The function is total on shape: skipping a
    non-dict preserves the "no per-item failure aborts the run"
    invariant from the gh layer."""
    items: list[Any] = [
        _repo(name="real"),
        "not-a-dict",
        None,
        42,
        _repo(name="also-real"),
    ]

    result = filter_starred(items, now=FROZEN_NOW)

    assert _kept_names(result) == ["real", "also-real"]


def test_all_archived_list_yields_empty_result() -> None:
    """Degenerate input: every repo is archived. The result is
    the empty list, not an error. A refactor that returns
    ``None`` for "all dropped" would break the Collector's
    iteration (``for repo in result``) silently — the test
    pins the return type."""
    items = [
        _repo(name=f"dead-{i}", archived=True) for i in range(5)
    ]

    result = filter_starred(items, now=FROZEN_NOW)

    assert result == []


def test_filter_does_not_call_datetime_now_when_now_is_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The function must use the ``now`` argument verbatim when
    provided — a fallback to ``_utc_now()`` even when ``now`` is
    set would break the frozen-time contract the rest of the test
    suite relies on.

    Pinned via ``monkeypatch.setattr`` on the module-private
    ``_utc_now`` helper (the same pattern ``test_gh.py`` uses for
    ``time.sleep`` and ``test_lock.py`` uses for ``os.kill``). The
    indirection is intentional: ``datetime.now`` is a C-level
    method of the immutable ``datetime.datetime`` class and
    cannot be patched directly. If ``filter_starred`` ever
    drifted to calling ``_utc_now()`` again even when ``now`` was
    provided, the patched callable would raise ``AssertionError``
    and the test would fail with a clear stack trace.
    """

    def boom() -> datetime:
        raise AssertionError("_utc_now called; now= was provided")

    monkeypatch.setattr("repo_pulse.watchlist._utc_now", boom)

    fresh_repo = _repo(name="fresh", pushed_at=FROZEN_NOW.isoformat())

    result = filter_starred([fresh_repo], now=FROZEN_NOW)

    assert _kept_names(result) == ["fresh"]
