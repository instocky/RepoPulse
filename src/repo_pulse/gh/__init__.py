"""``GitHubClient`` — the only component that talks to the GitHub REST API.

Layer: ``gh`` (leaf primitive). Per the 00-architecture doctrine this
module is reachable only from the ``collector`` layer; ``db``,
``analytics``, ``web``, and ``charts`` may not import from it. The
boundary is enforced by ``tests/test_architecture.py``.

Design
------
The client wraps a single ``httpx.Client`` that:

* Pre-sets the ``Authorization: Bearer <token>`` and
  ``User-Agent: repo-pulse/<version>`` headers (so no method can
  accidentally omit them — the contract is enforced at the transport
  layer, not at every call site).
* Targets ``https://api.github.com`` by default.

A small ``_request_response`` helper implements the retry policy
from the ticket: 429 and 5xx are retried with an
exponential-backoff sequence (default ``(1.0, 2.0, 4.0)``); all
other 4xx (including 404) fail fast with a structured warning and
``None`` (or, for the starred list, ``[]``). ``httpx`` network
errors are treated the same as 5xx — retried, and surfaced as
``None`` if the budget is exhausted.

Return shape
------------
The three public methods return typed dicts (per the ticket). The
shape is whatever the GitHub REST API emits:

* ``fetch_repo`` → ``/repos/{owner}/{name}`` payload (full repo dict).
* ``fetch_latest_release`` → ``/repos/{owner}/{name}/releases/latest``
  payload, or ``None`` if the repo has no release yet (the API
  signals this with 404, which the helper turns into ``None``
  silently — the Collector treats "no release" as a normal condition).
* ``fetch_starred`` → concatenated list of starred-repo dicts across
  every page (walked via the ``Link: rel="next"`` header).

The dict shape is the GitHub schema verbatim. Validation / typed
DTOs are a future concern (the analytics layer is where shapes get
narrowed).
"""

from __future__ import annotations

import json
import logging
import re
import time
from types import TracebackType
from typing import Any

import httpx

from repo_pulse import __version__
from repo_pulse.config import Config

__all__ = ["GitHubClient", "DEFAULT_BASE_URL", "DEFAULT_RETRY_BACKOFFS"]

# Ticket-mandated defaults. ``DEFAULT_RETRY_BACKOFFS`` is the
# single source of truth for the production sequence — a test
# asserts on it (``test_production_default_backoff_is_1_2_4``)
# so a refactor of the default cannot silently change production
# behavior.
DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_RETRY_BACKOFFS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Status codes that trigger a retry. 429 (rate-limited) and the
# standard 5xx server-error set; 502/503/504 are the cases GitHub
# returns during a deploy or transient outage.
_STATUS_RETRY: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Status codes that fail fast (no retry). 400 / 401 / 403 / 410
# are all "the request itself is wrong" and will not change on
# retry. 404 is the most common "the resource is gone" case and
# is also a fail-fast per the ticket; the 404 path additionally
# logs a structured warning so the operator can spot a watchlist
# drift.
_STATUS_FAIL_FAST: frozenset[int] = frozenset({400, 401, 403, 404, 410})

# Match the ``<URL>; rel="<name>"`` form per RFC 5988. We don't
# try to parse quoted parameters beyond ``rel``; the GitHub API
# only emits ``rel`` in practice.
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')

# Module-level logger. Tests use ``caplog.at_level("WARNING",
# logger="repo_pulse.gh")`` to assert on the structured warnings
# for 4xx and exhausted-retry cases.
_log = logging.getLogger(__name__)


def _parse_next_link(header: str | None) -> str | None:
    """Return the absolute URL of the ``rel="next"`` link, or ``None``.

    GitHub paginates ``GET /user/starred`` by emitting a ``Link``
    response header in the form::

        Link: <https://api.github.com/user/starred?page=2>; rel="next",
              <https://api.github.com/user/starred?page=42>; rel="last"

    We pull out the URL whose ``rel`` is ``"next"``; if no such
    entry exists, this is the last page. A missing or empty header
    is also "last page". The function never raises — a malformed
    header returns ``None`` (fail-soft) so a bad ``Link`` line
    does not abort the pagination loop.
    """
    if not header:
        return None
    for match in _LINK_RE.finditer(header):
        url, rel = match.group(1), match.group(2)
        if rel == "next":
            return url
    return None


class GitHubClient:
    """Synchronous GitHub REST client with retry + pagination.

    Parameters
    ----------
    config:
        A populated ``Config`` (from ``load_config``). Only
        ``github_token`` is read.
    base_url:
        Override the API root. Defaults to ``https://api.github.com``;
        kept as a parameter for future GitHub Enterprise support.
    retry_backoffs:
        Sequence of seconds to sleep between retries, consumed in
        order. The ticket pins ``(1.0, 2.0, 4.0)``; tests use
        ``(0.0, 0.0, 0.0)`` so the suite stays under a second.
        A request that fails on attempt N (1-indexed) sleeps
        ``retry_backoffs[N-1]`` seconds before retry N+1. If the
        sequence is shorter than the failure count, the request
        is given up (returns ``None``) after the last sleep.

    Used as a context manager so the underlying ``httpx.Client``'s
    connection pool is closed deterministically::

        with GitHubClient(config) as gh:
            repos = gh.fetch_starred()

    A request that exhausts its retry budget never raises — it
    returns ``None`` (per-repo methods) or ``[]`` (the starred
    list). The Collector relies on this to keep going on a single
    bad request.
    """

    __slots__ = ("_config", "_base_url", "_retry_backoffs", "_http", "_build_http")

    def __init__(
        self,
        config: Config,
        *,
        base_url: str = DEFAULT_BASE_URL,
        retry_backoffs: tuple[float, ...] = DEFAULT_RETRY_BACKOFFS,
    ) -> None:
        self._config = config
        self._base_url = base_url
        self._retry_backoffs = tuple(retry_backoffs)
        # ``_http`` is lazy: the first access via ``_get_http``
        # builds the underlying ``httpx.Client`` via ``_build_http``
        # and caches it. The default builder creates a Client
        # with the default transport, which on Windows takes
        # ~800 ms (one-time SSL trust-store bootstrap) —
        # acceptable for a daily Collector run, but catastrophic
        # for the test suite where every test would pay it. Tests
        # can pre-build a ``httpx.Client`` with a MockTransport
        # and assign it to ``_http`` directly — that path is
        # constant-time.
        self._http: httpx.Client | None = None
        self._build_http = self._default_build_http

    def _get_http(self) -> httpx.Client:
        """Return the underlying ``httpx.Client``, building it on first use.

        Splitting "build" from "use" lets the test suite override
        the build with a fast MockTransport without paying the
        Windows SSL-bootstrap cost. The first ``request`` call
        triggers the build; the result is cached for the lifetime
        of the GitHubClient.
        """
        if self._http is None:
            self._http = self._build_http()
        return self._http

    def _default_build_http(self) -> httpx.Client:
        """Build the default ``httpx.Client`` for production.

        ``trust_env=False`` so the connection ignores
        ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY`` from the
        shell. The Collector runs in a systemd unit on a VPS
        where those are not set, but developer shells may have
        them — keeping behaviour deterministic across
        environments means we never pick up a proxy unless
        explicitly configured.
        """
        return httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._config.github_token}",
                "User-Agent": f"repo-pulse/{__version__}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            trust_env=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    # ------------------------------------------------------------------
    # Public attributes (read-only)
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """The API root the Client targets. Read-only."""
        return self._base_url

    @property
    def retry_backoffs(self) -> tuple[float, ...]:
        """The retry backoff sequence (read-only snapshot)."""
        return self._retry_backoffs

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> GitHubClient:
        self._get_http().__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._get_http().__exit__(exc_type, exc, tb)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def fetch_starred(self) -> list[dict[str, Any]]:
        """Return the authenticated user's full starred list.

        Pages are walked via the ``Link: rel="next"`` header. The
        result is the concatenation of every page in order. If the
        first page fails after all retries, the method returns
        ``[]`` (no data to process) rather than partially-completed
        pages — a partial list would obscure which repos were
        dropped at the failure point, and the Collector cannot
        meaningfully proceed on an empty watchlist anyway.

        A later-page failure returns the partial list collected so
        far. The Collector treats a partial list as a degraded run
        and the warning in the log is the operator's signal to
        investigate.
        """
        collected: list[dict[str, Any]] = []
        url: str | None = "/user/starred"
        while url is not None:
            response = self._request_response("GET", url, op="fetch_starred")
            if response is None:
                # First page failure → empty list. Later-page
                # failure → return what we have. The Collector can
                # detect the degraded case via the warning log.
                return collected
            payload = self._safe_json(response)
            if isinstance(payload, list):
                collected.extend(item for item in payload if isinstance(item, dict))
            else:
                # Defensive: an unexpected non-list body is a
                # contract break on GitHub's side. Log and stop
                # paginating so we don't loop forever.
                _log.warning(
                    "fetch_starred: unexpected non-list body at %s, stopping pagination",
                    url,
                )
                break
            url = _parse_next_link(response.headers.get("Link"))
        return collected

    def fetch_repo(self, owner: str, name: str) -> dict[str, Any] | None:
        """Return the full ``/repos/{owner}/{name}`` payload, or ``None``.

        A 404 is the most common "no result" path: the user has the
        repo in their watchlist because it was once starred, but it
        has since been deleted or made private. The Collector
        treats this as a per-repo failure and the gh layer surfaces
        it as ``None`` without raising.
        """
        path = f"/repos/{owner}/{name}"
        response = self._request_response("GET", path, op=f"fetch_repo({owner}/{name})")
        if response is None:
            return None
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            return None
        return payload

    def fetch_latest_release(self, owner: str, name: str) -> dict[str, Any] | None:
        """Return the latest release for a repo, or ``None`` if there is none.

        GitHub returns 404 for repos with no published release; we
        treat that as ``None`` (no error, no log) so the Collector
        can write ``NULL`` to ``snapshots.latest_release_at`` and
        move on. All other 4xx (e.g. 403) do log a warning — they
        are not expected on this endpoint and indicate something
        worth a look.
        """
        path = f"/repos/{owner}/{name}/releases/latest"
        response = self._request_response(
            "GET",
            path,
            op=f"fetch_latest_release({owner}/{name})",
            # 404 on releases/latest is the *expected* "no release
            # yet" answer, not a failure. Suppress the warning so
            # the operator's logs are not flooded for every
            # not-yet-released repo in the watchlist.
            suppress_404_log=True,
        )
        if response is None:
            return None
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            return None
        return payload

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        """Parse the response body as JSON, returning ``None`` on bad data.

        A successful HTTP status with a non-JSON body is a
        contract break on GitHub's side; we return ``None`` rather
        than raising so the Collector's "no per-repo failure
        aborts the run" invariant holds.
        """
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            _log.warning(
                "gh: %d with invalid JSON body: %s",
                response.status_code,
                exc,
            )
            return None

    def _request_response(
        self,
        method: str,
        url: str,
        *,
        op: str,
        suppress_404_log: bool = False,
    ) -> httpx.Response | None:
        """Issue a request with the retry policy; return the response or ``None``.

        The retry loop:

        * On 2xx: return the response. Body is left to the caller
          (we return the response object so ``fetch_starred`` can
          read the ``Link`` header).
        * On 4xx in ``_STATUS_FAIL_FAST`` (notably 404): log a
          warning (unless ``suppress_404_log``) and return
          ``None``. No retry. A 4xx means the request itself is
          wrong, so retrying will fail identically.
        * On 429 or 5xx: sleep ``retry_backoffs[i]`` and try
          again, until the backoff sequence is exhausted, then
          log a warning and return ``None``.
        * On an ``httpx`` network error: same retry budget as
          5xx.

        Any non-exception failure path returns ``None``; only
        programmer errors (a malformed URL passed to ``httpx``)
        propagate. The Collector relies on the "no raise"
        guarantee.
        """
        attempts = 1 + len(self._retry_backoffs)
        last_status: int | None = None
        last_error: str | None = None
        for attempt in range(attempts):
            try:
                response = self._get_http().request(method, url)
            except httpx.HTTPError as exc:
                # Network / connect / read / timeout errors are
                # transient — retry with the same backoff budget
                # as 5xx. Per-attempt is logged at debug so an
                # operator running with ``-v`` can see the retry
                # storm; the final failure is logged at warning
                # (after the loop below).
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
                _log.debug(
                    "%s: network error attempt %d/%d: %s",
                    op,
                    attempt + 1,
                    attempts,
                    last_error,
                )
            else:
                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    return response
                if response.status_code in _STATUS_FAIL_FAST:
                    if response.status_code == 404 and suppress_404_log:
                        return None
                    _log.warning(
                        "%s: %d %s (no retry)",
                        op,
                        response.status_code,
                        response.reason_phrase or "",
                    )
                    return None
                if response.status_code in _STATUS_RETRY:
                    last_error = f"status {response.status_code}"
                    _log.debug(
                        "%s: %d on attempt %d/%d, will retry",
                        op,
                        response.status_code,
                        attempt + 1,
                        attempts,
                    )
                else:
                    # Unknown status (1xx / 3xx / something
                    # exotic). Fail-soft: log and return ``None``
                    # rather than guessing whether to retry.
                    _log.warning(
                        "%s: unexpected status %d, returning None",
                        op,
                        response.status_code,
                    )
                    return None
            # If we have a remaining backoff, sleep and retry. We
            # always call ``time.sleep`` (even with a 0.0 backoff)
            # so the test suite can assert on the call pattern
            # without having to inject a different backoff
            # mechanism — ``time.sleep(0.0)`` is a documented
            # no-op, so the runtime cost is the same.
            if attempt < len(self._retry_backoffs):
                time.sleep(self._retry_backoffs[attempt])

        # All attempts exhausted.
        if last_status is not None:
            _log.warning(
                "%s: %s after %d attempts, giving up",
                op,
                last_error or f"status {last_status}",
                attempts,
            )
        else:
            _log.warning(
                "%s: %s after %d attempts, giving up",
                op,
                last_error or "network error",
                attempts,
            )
        return None
