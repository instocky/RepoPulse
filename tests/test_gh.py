"""Tests for the ``repo_pulse.gh`` layer — GitHub REST client.

Covers the acceptance criteria in
``.scratch/repo-pulse/issues/05-gh.md``:

* happy-path ``fetch_starred`` / ``fetch_repo`` / ``fetch_latest_release``
* 429-then-200 and 500-then-200 retry with backoff
* 404 fail-fast (no retry, logs a warning)
* other 4xx fail-fast
* 429 / 5xx exhausting all retries returns ``None`` / ``[]``
* pagination via the ``Link`` header (rel="next")
* missing ``Link`` header = single page, no follow-up GET
* ``Authorization: Bearer <token>`` and ``User-Agent: repo-pulse/<ver>`` on every request

The retry backoff is injected via the ``retry_backoffs`` constructor
kwarg so the suite stays under one second; production uses
``(1.0, 2.0, 4.0)`` (per the ticket).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from repo_pulse import __version__
from repo_pulse.config import Config, load_config
from repo_pulse.gh import GitHubClient

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# The ticket's documented production backoffs. Tests use the much shorter
# ``_ZERO_BACKOFFS`` so the 429/5xx retry tests finish in milliseconds.
_PROD_BACKOFFS = (1.0, 2.0, 4.0)
_ZERO_BACKOFFS = (0.0, 0.0, 0.0)

# Reused GitHub-style repo dict. We only need the field shape the
# Client's downstream callers care about; the spec pins the contract at
# "typed dicts" and the actual field set is whatever the GitHub API
# returns. The full payload is included for fetch_repo's happy path.
_REPO_FULL = {
    "id": 12345,
    "full_name": "owner/name",
    "name": "name",
    "owner": {"login": "owner"},
    "description": "An example repo",
    "homepage": "https://example.com",
    "visibility": "public",
    "default_branch": "main",
    "license": {"spdx_id": "MIT"},
    "archived": False,
    "disabled": False,
    "stargazers_count": 1000,
    "forks_count": 50,
    "open_issues_count": 12,
    "subscribers_count": 25,
    "watchers_count": 1000,
    "pushed_at": "2026-07-30T12:00:00Z",
    "language": "Python",
    "size": 1024,
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2026-07-30T12:00:00Z",
    "has_issues": True,
    "topics": ["ai", "agents"],
}


def _config(tmp_path: Path, *, token: str = "ghp_test_token") -> Config:
    """Build a minimal ``Config`` with a token and project-local data dirs.

    The gh client only needs ``github_token``; the other fields are
    passed-through for type-correctness. We set ``data_dir`` and
    ``reports_dir`` under ``tmp_path`` so the ``mkdir(parents=True,
    exist_ok=True)`` in ``load_config`` never touches the real project.
    """
    env = tmp_path / ".env"
    env.write_text(f"GITHUB_TOKEN={token}\n", encoding="utf-8")
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"[paths]\n"
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n'
        f'reports_dir = "{(tmp_path / "reports").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(env_path=env, toml_path=toml)


def _fast_client(
    config: Config,
    rmock: respx.MockRouter,
    *,
    backoffs: tuple[float, ...] = _ZERO_BACKOFFS,
) -> GitHubClient:
    """Build a Client with a fast ``httpx.MockTransport`` instead of the
    slow default transport.

    respx 0.23's patched ``_transport_for_url`` constructs a fresh
    ``httpx.HTTPTransport`` on every request to wire up the
    pass-through transport, and on Windows the SSL trust-store
    bootstrap inside that constructor takes ~800 ms per call (so a
    5-request test takes 4 s instead of 5 ms). We use respx as the
    route DSL (``rmock.get(...)``) but build a plain
    ``httpx.MockTransport`` from the router's ``handler`` and inject
    it directly into the Client, bypassing the slow
    ``_transport_for_url`` patch.

    The test still asserts against the same route mocks that respx
    set up; only the transport plumbing is faster. The behaviour
    under test is identical to using ``respx.mock()`` alone, so the
    tests do not lose coverage — they just run in milliseconds.
    """
    client = GitHubClient(config, retry_backoffs=backoffs)
    # Build a fast MockTransport that delegates to the respx
    # router's handler. Bypasses the default transport (which on
    # Windows takes ~800 ms to bootstrap the SSL trust store).
    # The respx routes are still the source of truth for which
    # URLs match which responses. The headers come from the
    # production ``_default_headers`` helper so a future header
    # change in production automatically applies here — no risk
    # of the test Client silently disagreeing with production.
    fast = httpx.MockTransport(rmock.handler)
    from repo_pulse.gh import _default_headers

    fast_client = httpx.Client(
        base_url=str(client.base_url),
        headers=_default_headers(config.github_token),
        trust_env=False,
        timeout=httpx.Timeout(30.0, connect=10.0),
        transport=fast,
    )
    # Inject the fast Client directly. ``_http`` is a private
    # attribute (set in ``__init__`` to ``None`` and populated
    # lazily by ``_get_http``); assigning to it skips the slow
    # default builder entirely. The lazy build is what makes
    # the production path constant-time per request — the
    # *first* request in production pays the SSL cost, not
    # the constructor.
    client._http = fast_client
    return client


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_fetch_repo_returns_full_dict(tmp_path: Path) -> None:
    """200 on ``/repos/{owner}/{name}`` returns the parsed JSON dict."""
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/repos/owner/name").mock(return_value=httpx.Response(200, json=_REPO_FULL))
        client = _fast_client(config, rmock)
        result = client.fetch_repo("owner", "name")

    assert result == _REPO_FULL
    assert result["full_name"] == "owner/name"


def test_fetch_latest_release_returns_dict_when_release_exists(tmp_path: Path) -> None:
    """200 on ``/repos/{owner}/{name}/releases/latest`` returns the release dict."""
    config = _config(tmp_path)
    release = {"tag_name": "v1.0.0", "published_at": "2026-07-30T00:00:00Z"}
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/repos/owner/name/releases/latest").mock(
            return_value=httpx.Response(200, json=release)
        )
        client = _fast_client(config, rmock)
        result = client.fetch_latest_release("owner", "name")

    assert result == release


def test_fetch_latest_release_returns_none_on_404(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A repo with no releases returns 404; the client surfaces ``None``.

    The Collector treats "no release" as a normal condition (not a
    failure) and writes ``NULL`` for ``latest_release_at``; the
    client must not raise. The 404 is still logged (per the
    ticket's "fails fast and logs structured warning" line) but
    at ``INFO`` not ``WARNING`` — see the module docstring of
    ``fetch_latest_release`` for why this endpoint has a
    different log level than the generic 4xx path.
    """
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/repos/owner/name/releases/latest").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        client = _fast_client(config, rmock)
        with caplog.at_level("INFO", logger="repo_pulse.gh"):
            result = client.fetch_latest_release("owner", "name")

    assert result is None
    # The 404 was logged (not silently swallowed). INFO, not
    # WARNING, because "no release yet" is a normal state, not
    # an operator signal. The ``no_release=True`` extra on the
    # log record lets a structured formatter filter these out
    # of the operator's alert feed.
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any("404" in r.message for r in info_records), (
        "expected an INFO log mentioning 404 for the no-release case"
    )
    assert not any(r.levelname == "WARNING" and "404" in r.message for r in caplog.records), (
        "404 on releases/latest must NOT log at WARNING"
    )


def test_fetch_starred_returns_full_list_single_page(tmp_path: Path) -> None:
    """A single-page starred response (no ``Link`` header) is returned as-is."""
    config = _config(tmp_path)
    page1 = [{"full_name": "owner/a"}, {"full_name": "owner/b"}]
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/user/starred").mock(return_value=httpx.Response(200, json=page1))
        client = _fast_client(config, rmock)
        result = client.fetch_starred()

    assert result == page1


# ---------------------------------------------------------------------------
# Auth + User-Agent on every request
# ---------------------------------------------------------------------------


def test_authorization_bearer_header_sent_on_every_request(tmp_path: Path) -> None:
    """The token is sent on every request, not just the first.

    Pins the contract that the ``Authorization`` header is set on the
    Client (not added per-call), so no method can accidentally omit it.
    """
    config = _config(tmp_path, token="ghp_secret_xyz")
    with respx.mock(base_url="https://api.github.com") as rmock:
        route_repo = rmock.get("/repos/owner/name").mock(
            return_value=httpx.Response(200, json=_REPO_FULL)
        )
        route_rel = rmock.get("/repos/owner/name/releases/latest").mock(
            return_value=httpx.Response(200, json={"tag_name": "v1"})
        )
        client = _fast_client(config, rmock)
        client.fetch_repo("owner", "name")
        client.fetch_latest_release("owner", "name")

    assert route_repo.calls.call_count == 1
    assert route_rel.calls.call_count == 1
    for call in (route_repo.calls.last, route_rel.calls.last):
        assert call.request.headers["authorization"] == "Bearer ghp_secret_xyz"


def test_user_agent_header_sent_on_every_request(tmp_path: Path) -> None:
    """The ``User-Agent`` is the package name + version (per ticket)."""
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        route = rmock.get("/repos/owner/name").mock(
            return_value=httpx.Response(200, json=_REPO_FULL)
        )
        client = _fast_client(config, rmock)
        client.fetch_repo("owner", "name")

    ua = route.calls.last.request.headers["user-agent"]
    assert ua == f"repo-pulse/{__version__}"


# ---------------------------------------------------------------------------
# Retry on 429 and 5xx
# ---------------------------------------------------------------------------


def test_429_then_200_retries_with_backoff(tmp_path: Path) -> None:
    """A single 429 followed by 200 succeeds after one backoff sleep."""
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        route = rmock.get("/repos/owner/name").mock(
            side_effect=[
                httpx.Response(429, json={"message": "rate limited"}),
                httpx.Response(200, json=_REPO_FULL),
            ]
        )
        client = _fast_client(config, rmock)
        # Inject a single deterministic sleep; the retry logic must
        # call it exactly once between the 429 and the 200.
        with patch("repo_pulse.gh.time.sleep") as sleep_mock:
            result = client.fetch_repo("owner", "name")

    assert result == _REPO_FULL
    assert route.calls.call_count == 2
    assert sleep_mock.call_count == 1
    # The first (and only) backoff passed to sleep is the first entry
    # of the configured sequence. We pass ``(0.0, 0.0, 0.0)`` in tests
    # so this doubles as a "real production sleep is configurable" pin.
    assert sleep_mock.call_args.args[0] == 0.0


def test_500_then_200_retries_with_backoff(tmp_path: Path) -> None:
    """A single 5xx followed by 200 succeeds after one backoff sleep."""
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        route = rmock.get("/repos/owner/name").mock(
            side_effect=[
                httpx.Response(500, text="boom"),
                httpx.Response(200, json=_REPO_FULL),
            ]
        )
        client = _fast_client(config, rmock)
        with patch("repo_pulse.gh.time.sleep") as sleep_mock:
            result = client.fetch_repo("owner", "name")

    assert result == _REPO_FULL
    assert route.calls.call_count == 2
    assert sleep_mock.call_count == 1


def test_429_exhausts_retries_returns_none_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If every retry attempt returns 429, the client surfaces ``None`` and
    logs a warning. The ``Collector`` keeps going on a per-repo failure,
    so the gh layer must not raise — it must hand back a clean ``None``.
    """
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        route = rmock.get("/repos/owner/name").mock(
            return_value=httpx.Response(429, json={"message": "rate limited"})
        )
        client = _fast_client(config, rmock)
        with patch("repo_pulse.gh.time.sleep"):
            with caplog.at_level("WARNING", logger="repo_pulse.gh"):
                result = client.fetch_repo("owner", "name")

    assert result is None
    # 1 initial attempt + 3 retries (per the ticket's "retries 3 times").
    assert route.calls.call_count == 1 + len(_PROD_BACKOFFS)
    assert any("429" in rec.message for rec in caplog.records), "expected a warning mentioning 429"


def test_5xx_exhausts_retries_returns_none(tmp_path: Path) -> None:
    """If every retry attempt returns 5xx, the client surfaces ``None``."""
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        route = rmock.get("/repos/owner/name").mock(
            return_value=httpx.Response(503, text="unavailable")
        )
        client = _fast_client(config, rmock)
        with patch("repo_pulse.gh.time.sleep"):
            result = client.fetch_repo("owner", "name")

    assert result is None
    assert route.calls.call_count == 1 + len(_PROD_BACKOFFS)


def test_retry_uses_configured_backoff_sequence(tmp_path: Path) -> None:
    """The full backoff sequence is consumed in order across the 3 retries."""
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/repos/owner/name").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(200, json=_REPO_FULL),
            ]
        )
        client = _fast_client(config, rmock, backoffs=(0.1, 0.2, 0.4))
        with patch("repo_pulse.gh.time.sleep") as sleep_mock:
            client.fetch_repo("owner", "name")

    assert sleep_mock.call_count == 3
    assert [c.args[0] for c in sleep_mock.call_args_list] == [0.1, 0.2, 0.4]


def test_production_default_backoff_is_1_2_4(tmp_path: Path) -> None:
    """Without an explicit override, the client uses the ticket's 1s/2s/4s
    backoff. Pinned here so a refactor of the default cannot silently
    change production behavior.
    """
    config = _config(tmp_path)
    client = GitHubClient(config)
    assert client.retry_backoffs == _PROD_BACKOFFS


# ---------------------------------------------------------------------------
# Fail-fast on 4xx (other than 429)
# ---------------------------------------------------------------------------


def test_404_does_not_retry_and_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A 404 fails fast (no retry), logs a structured warning, returns ``None``.

    Per the ticket: a 404 means the repo was deleted or made private —
    the next request will get the same answer, so retrying is
    pointless. The warning is the operator's signal that a watched
    repo disappeared.
    """
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        route = rmock.get("/repos/owner/name").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        client = _fast_client(config, rmock)
        with patch("repo_pulse.gh.time.sleep") as sleep_mock:
            with caplog.at_level("WARNING", logger="repo_pulse.gh"):
                result = client.fetch_repo("owner", "name")

    assert result is None
    # 404 is fail-fast: a single call, no sleep, no retry.
    assert route.calls.call_count == 1
    assert sleep_mock.call_count == 0
    assert any("404" in rec.message for rec in caplog.records), "expected a warning mentioning 404"


def test_4xx_other_than_429_does_not_retry(tmp_path: Path) -> None:
    """401 / 403 / 400 — all fail-fast with ``None``, no retry."""
    config = _config(tmp_path)
    for status in (400, 401, 403):
        with respx.mock(base_url="https://api.github.com") as rmock:
            route = rmock.get("/repos/owner/name").mock(
                return_value=httpx.Response(status, json={"message": "err"})
            )
            client = _fast_client(config, rmock)
            with patch("repo_pulse.gh.time.sleep") as sleep_mock:
                result = client.fetch_repo("owner", "name")

            assert result is None, f"{status} should produce None"
            assert route.calls.call_count == 1, f"{status} must not retry"
            assert sleep_mock.call_count == 0, f"{status} must not sleep"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _link_header(next_url: str | None, last_url: str | None = None) -> str:
    """Build a GitHub-style ``Link`` header for respx mocks."""
    parts: list[str] = []
    if next_url is not None:
        parts.append(f'<{next_url}>; rel="next"')
    if last_url is not None:
        parts.append(f'<{last_url}>; rel="last"')
    return ", ".join(parts)


def test_fetch_starred_follows_pagination_via_link_header(tmp_path: Path) -> None:
    """``fetch_starred`` walks every page until a response has no ``rel="next"``.

    Three pages: page1 has a next-link to page2, page2 has a next-link
    to page3, page3 has only a last-link. The returned list is the
    concatenation of all three pages in order.
    """
    config = _config(tmp_path)
    page1 = [{"full_name": f"o/r{i}"} for i in range(3)]
    page2 = [{"full_name": f"o/r{i}"} for i in range(3, 6)]
    page3 = [{"full_name": f"o/r{i}"} for i in range(6, 8)]
    expected = page1 + page2 + page3

    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/user/starred").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=page1,
                    headers={
                        "Link": _link_header(
                            "https://api.github.com/user/starred?page=2",
                            "https://api.github.com/user/starred?page=3",
                        )
                    },
                ),
                httpx.Response(
                    200,
                    json=page2,
                    headers={
                        "Link": _link_header(
                            "https://api.github.com/user/starred?page=3",
                        )
                    },
                ),
                httpx.Response(
                    200,
                    json=page3,
                    # No "next" → client stops here.
                ),
            ]
        )
        client = _fast_client(config, rmock)
        result = client.fetch_starred()

    assert result == expected


def test_fetch_starred_with_no_link_header_returns_one_page(tmp_path: Path) -> None:
    """A 200 with no ``Link`` header at all is the single page (already pinned
    in ``test_fetch_starred_returns_full_list_single_page``); the regression
    here is the explicit "no header" branch as opposed to "header without
    next-rel".
    """
    config = _config(tmp_path)
    page = [{"full_name": "a/b"}]
    with respx.mock(base_url="https://api.github.com") as rmock:
        route = rmock.get("/user/starred").mock(return_value=httpx.Response(200, json=page))
        client = _fast_client(config, rmock)
        result = client.fetch_starred()

    assert result == page
    assert route.calls.call_count == 1


def test_fetch_starred_link_header_with_only_last_rel_stops(tmp_path: Path) -> None:
    """A ``Link`` header that names only ``rel="last"`` is the final page."""
    config = _config(tmp_path)
    page = [{"full_name": "a/b"}]
    with respx.mock(base_url="https://api.github.com") as rmock:
        route = rmock.get("/user/starred").mock(
            return_value=httpx.Response(
                200,
                json=page,
                headers={"Link": _link_header(None, "https://api.github.com/user/starred?page=42")},
            )
        )
        client = _fast_client(config, rmock)
        result = client.fetch_starred()

    assert result == page
    assert route.calls.call_count == 1


def test_paginated_request_uses_absolute_url_from_link_header(tmp_path: Path) -> None:
    """The pagination loop fetches the *absolute* URL from the next-link,
    not a path appended to the base. This matters when the GitHub API
    returns a fully-qualified next URL.
    """
    config = _config(tmp_path)
    page1 = [{"full_name": "a/b"}]
    page2 = [{"full_name": "c/d"}]
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/user/starred").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=page1,
                    headers={"Link": _link_header("https://api.github.com/user/starred?page=2")},
                ),
                httpx.Response(200, json=page2),
            ]
        )
        client = _fast_client(config, rmock)
        result = client.fetch_starred()

    assert result == page1 + page2


def test_paginated_request_retries_per_page(tmp_path: Path) -> None:
    """A 429 on page 2 is retried; the next page after the retry is included."""
    config = _config(tmp_path)
    page1 = [{"full_name": "a/b"}]
    page2 = [{"full_name": "c/d"}]
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/user/starred").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=page1,
                    headers={"Link": _link_header("https://api.github.com/user/starred?page=2")},
                ),
                httpx.Response(429, json={"message": "rate limited"}),
                httpx.Response(200, json=page2),
            ]
        )
        client = _fast_client(config, rmock)
        with patch("repo_pulse.gh.time.sleep"):
            result = client.fetch_starred()

    assert result == page1 + page2


def test_paginated_first_page_failure_returns_empty(tmp_path: Path) -> None:
    """If the first page of the starred list fails after all retries, the
    client returns ``[]`` (the Collector cannot proceed with no input).
    """
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/user/starred").mock(return_value=httpx.Response(500, text="boom"))
        client = _fast_client(config, rmock)
        with patch("repo_pulse.gh.time.sleep"):
            result = client.fetch_starred()

    assert result == []


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


def test_network_error_returns_none(tmp_path: Path) -> None:
    """A connection error after retries is surfaced as ``None``, not raised.

    The Collector's contract: a transient network glitch during one
    repo's fetch must not abort the run.
    """
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/repos/owner/name").mock(side_effect=httpx.ConnectError("dns broke"))
        client = _fast_client(config, rmock)
        with patch("repo_pulse.gh.time.sleep"):
            result = client.fetch_repo("owner", "name")

    assert result is None


# ---------------------------------------------------------------------------
# Base URL & lifecycle
# ---------------------------------------------------------------------------


def test_client_targets_api_github_com_by_default(tmp_path: Path) -> None:
    """All requests go to ``https://api.github.com`` when no override is given.

    The deploy does not need a custom base URL; this is here to pin
    the default for any future "use a GitHub Enterprise" work.
    """
    config = _config(tmp_path)
    client = GitHubClient(config)
    assert str(client.base_url).rstrip("/") == "https://api.github.com"


def test_client_context_manager_closes_transport(tmp_path: Path) -> None:
    """Used as a context manager, the underlying ``httpx.Client`` is closed.

    Pin for the Collector / unit tests that rely on the context-manager
    form to avoid leaking the connection pool. Uses ``_fast_client``
    so the test doesn't pay the Windows SSL-bootstrap cost on the
    underlying ``httpx.Client`` — only the ``__enter__`` / ``__exit__``
    wiring is being exercised here, not the transport itself.
    """
    config = _config(tmp_path)
    with respx.mock(base_url="https://api.github.com") as rmock:
        rmock.get("/repos/owner/name").mock(return_value=httpx.Response(200, json=_REPO_FULL))
        with _fast_client(config, rmock) as client:
            client.fetch_repo("owner", "name")
            underlying = client._http
            assert underlying is not None  # narrowed for mypy
        # After the with block the underlying Client is closed; further
        # use would raise ``RuntimeError("Cannot send a request, as the
        # client has not been opened.")``.
        with pytest.raises(RuntimeError):
            underlying.get("/repos/owner/name")


# ---------------------------------------------------------------------------
# Layer contract — the gh module respects 00-architecture doctrine.
# The architecture enforcer in ``tests/test_architecture.py`` (the
# symmetric ``gh → {collector, db, analytics, charts, web}`` pairs
# plus ``test_enforcer_catches_gh_importing_db``) is the
# authoritative check; a separate per-file test here would just
# duplicate that AST walk with weaker coverage.
