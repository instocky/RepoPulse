"""``Config`` dataclass and the ``load_config`` factory.

The factory is the only public entry point. It:

1. Reads ``.env`` (if present) via ``dotenv_values`` (no
   ``os.environ`` mutation — see ``_load_env`` for the why).
2. Merges the file values with the live ``os.environ``, giving
   the shell precedence over ``.env`` (matches systemd
   ``Environment=`` over ``EnvironmentFile=`` semantics).
3. Reads the tunables from ``config.toml`` (if present) and
   overlays them on the dataclass defaults.
4. Creates ``data_dir`` and ``reports_dir`` (with
   ``mkdir(parents=True, exist_ok=True)``).
5. Returns a frozen ``Config`` dataclass.

Layer: ``config`` (leaf). Per the 00-architecture doctrine this
module imports only stdlib and third-party libs (``dotenv``,
``tomllib``); it never imports other ``repo_pulse`` submodules.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

__all__ = ["Config", "ConfigError", "load_config"]


class ConfigError(RuntimeError):
    """Raised when the environment is unusable.

    The most common cause is a missing ``GITHUB_TOKEN``. We raise
    early — before any other module runs — so the user sees a
    single actionable error instead of a stack trace through
    httpx / typer / the Collector.
    """


_DEFAULT_ENV_PATH = Path(".env")
_DEFAULT_TOML_PATH = Path("config.toml")


@dataclass(frozen=True, kw_only=True)
class Config:
    """Typed snapshot of runtime configuration.

    Constructed by ``load_config``. Fields are keyword-only so the
    dataclass can mix required and defaulted fields cleanly;
    ``load_config`` always fills every field from env + toml.

    Attributes
    ----------
    github_token:
        GitHub Personal Access Token. **Required.** Loaded from the
        ``GITHUB_TOKEN`` env var (set in ``.env`` or exported in
        the shell). Raises ``ConfigError`` if missing or empty.
    basic_auth_user:
        HTTP Basic Auth username. Loaded from ``BASIC_AUTH_USER``.
        Used only by the deploy step to generate ``htpasswd``; the
        Python app never sees this on the request path. Defaults
        to ``""`` so local runs work without it.
    basic_auth_pass:
        HTTP Basic Auth password. Loaded from ``BASIC_AUTH_PASS``.
        Same deploy-only use; defaults to ``""``.
    data_dir:
        SQLite + lock file location. Set via
        ``config.toml [paths].data_dir``. Created on load
        (``mkdir(parents=True, exist_ok=True)``). Default:
        ``Path("data")`` (project-local).
    reports_dir:
        Where the daily report HTML is written. Set via
        ``config.toml [paths].reports_dir``. Created on load.
        Default: ``Path("reports")``.
    recent_months:
        A repository is "active" iff ``pushed_at > now - recent_months``.
        Set via ``config.toml [filter].recent_months``. Default: 12.
    min_absolute_delta:
        Viral events require at least this many stars gained per
        snapshot. Set via ``config.toml [viral].min_absolute_delta``.
        Default: 100.
    min_relative_growth:
        Viral events require at least this fractional growth per
        snapshot. Set via ``config.toml [viral].min_relative_growth``.
        Default: 0.20 (20 %).
    web_host:
        Dashboard bind address. Default: ``"127.0.0.1"``. Not yet
        exposed in ``config.toml``; the dataclass default is the
        contract for ticket 13 (web).
    web_port:
        Dashboard bind port. Default: 8000. Same future-toml caveat
        as ``web_host``.
    """

    github_token: str
    basic_auth_user: str = ""
    basic_auth_pass: str = ""
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")
    recent_months: int = 12
    min_absolute_delta: int = 100
    min_relative_growth: float = 0.20
    web_host: str = "127.0.0.1"
    web_port: int = 8000


def load_config(
    env_path: Path | None = None,
    toml_path: Path | None = None,
) -> Config:
    """Load configuration from ``.env`` and ``config.toml``.

    Parameters
    ----------
    env_path:
        Path to the dotenv file. ``None`` means ``.env`` in CWD.
        A missing file is not an error — values may already be in
        ``os.environ`` (e.g. set in the shell or systemd unit).
    toml_path:
        Path to the config toml. ``None`` means ``config.toml`` in
        CWD. A missing file is not an error — dataclass defaults
        apply for every tunable.

    Returns
    -------
    Config
        A frozen dataclass with every field populated.

    Raises
    ------
    ConfigError
        ``GITHUB_TOKEN`` is empty or missing in both ``.env`` and
        the shell, ``config.toml`` is syntactically invalid, or a
        toml value cannot be coerced to its declared type.
    """
    env_file_values = _load_env(env_path)
    toml_data = _load_toml(toml_path)

    # ``_env_value`` is presence-based, so an explicit empty shell
    # value would still reach us here. ``GITHUB_TOKEN`` is the one
    # required field — strip first (a stray newline in .env is a
    # common authoring mistake) and reject the empty case.
    github_token = _env_value("GITHUB_TOKEN", env_file_values).strip()
    if not github_token:
        raise ConfigError(
            "GITHUB_TOKEN is missing or empty. "
            "Set it in .env or export it in the environment."
        )

    # `or {}` guards against a toml section that is explicitly `section = {}`,
    # which tomllib parses as an empty dict, but defends against odd shapes.
    paths = toml_data.get("paths") or {}
    filter_ = toml_data.get("filter") or {}
    viral = toml_data.get("viral") or {}

    data_dir = _coerce_path(paths.get("data_dir", "data"))
    reports_dir = _coerce_path(paths.get("reports_dir", "reports"))

    data_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        github_token=github_token,
        basic_auth_user=_env_value("BASIC_AUTH_USER", env_file_values),
        basic_auth_pass=_env_value("BASIC_AUTH_PASS", env_file_values),
        data_dir=data_dir,
        reports_dir=reports_dir,
        recent_months=_coerce_int(filter_.get("recent_months", 12), "recent_months"),
        min_absolute_delta=_coerce_int(
            viral.get("min_absolute_delta", 100), "min_absolute_delta"
        ),
        min_relative_growth=_coerce_float(
            viral.get("min_relative_growth", 0.20), "min_relative_growth"
        ),
        # web_host / web_port take the dataclass defaults — they are not
        # exposed in config.toml yet (deferred to ticket 13: web).
    )


def _load_env(env_path: Path | None) -> dict[str, str]:
    """Read the dotenv file into a dict. Does NOT mutate ``os.environ``.

    We deliberately avoid ``load_dotenv`` here. ``load_dotenv`` writes
    into ``os.environ`` as a side effect, which leaks state between
    tests (an empty value written by test N poisons test N+1, even
    with monkeypatch restoration — pytest's restoration does not
    re-parse dotenv files). Reading into a dict and merging
    ourselves keeps the load hermetic and the precedence rule
    ("shell wins over .env") explicit in the loader.
    """
    path = env_path if env_path is not None else _DEFAULT_ENV_PATH
    if not path.exists():
        return {}
    raw = dotenv_values(path) or {}
    # ``dotenv_values`` returns ``None`` for variable-less lines (e.g.
    # ``export FOO``); drop those — they would shadow the dataclass
    # default with ``None`` downstream.
    return {k: v for k, v in raw.items() if v is not None}


def _env_value(name: str, env_file_values: dict[str, str]) -> str:
    """Resolve a config var with the precedence: shell > .env > missing.

    The check is presence-based, not truthiness-based: an empty
    shell value is treated as an explicit "I want this empty" and
    wins over a non-empty .env value. This matches systemd unit
    semantics, where ``Environment=`` overrides ``EnvironmentFile=``.
    """
    if name in os.environ:
        return os.environ[name]
    return env_file_values.get(name, "")


def _load_toml(toml_path: Path | None) -> dict[str, Any]:
    """Parse ``config.toml`` and return its dict, or ``{}`` if missing.

    Wraps ``tomllib.TOMLDecodeError`` as ``ConfigError`` so callers
    get a single error type from this module.
    """
    path = toml_path if toml_path is not None else _DEFAULT_TOML_PATH
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml at {path} is invalid: {e}") from e


def _coerce_path(value: Any) -> Path:
    """Coerce a toml value to a ``Path`` (via ``str()``)."""
    return Path(str(value))


def _coerce_int(value: Any, name: str) -> int:
    """Coerce a toml value to ``int``.

    Rejects ``bool`` explicitly because in Python ``bool`` is a
    subclass of ``int`` (``int(True) == 1``), which would silently
    turn ``recent_months = true`` into ``1``. Rejects everything
    that is not ``int`` or a numeric string with a clear error.
    """
    if isinstance(value, bool):
        raise ConfigError(f"config.toml: {name!r} must be an integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as e:
            raise ConfigError(
                f"config.toml: {name!r}={value!r} is not an integer"
            ) from e
    raise ConfigError(
        f"config.toml: {name!r} must be an integer, got {type(value).__name__}"
    )


def _coerce_float(value: Any, name: str) -> float:
    """Coerce a toml value to ``float``.

    Same bool-rejection rationale as ``_coerce_int``. Accepts
    ``int`` (e.g. ``min_relative_growth = 1``) and numeric strings.
    """
    if isinstance(value, bool):
        raise ConfigError(f"config.toml: {name!r} must be a number, got bool")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as e:
            raise ConfigError(
                f"config.toml: {name!r}={value!r} is not a number"
            ) from e
    raise ConfigError(
        f"config.toml: {name!r} must be a number, got {type(value).__name__}"
    )
