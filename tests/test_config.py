"""Tests for the ``repo_pulse.config`` layer.

Covers the acceptance criteria in
``.scratch/repo-pulse/issues/03-config.md``:

* missing GITHUB_TOKEN → ``ConfigError`` mentioning the env var
* missing config.toml → dataclass defaults apply
* partial config.toml → listed values override, others keep defaults
* full config.toml → every value reflects the file
* invalid toml → ``ConfigError`` (not ``TOMLDecodeError``)

Plus edge cases that protect the contract long-term: whitespace-only
tokens, shell env winning over ``.env`` (``override=False``), the
frozen dataclass guarantee, nested directory creation, and
int/float coercion of toml values.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from repo_pulse.config import Config, ConfigError, load_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_with_token(tmp_path: Path, *, token: str = "ghp_test") -> Path:
    """Write a minimal ``.env`` that satisfies ``GITHUB_TOKEN``."""
    p = tmp_path / ".env"
    p.write_text(f"GITHUB_TOKEN={token}\n", encoding="utf-8")
    return p


def _paths_section(tmp_path: Path) -> str:
    """A ``[paths]`` block anchored under ``tmp_path`` so the test never
    creates ``data/`` or ``reports/`` in the project root."""
    return (
        f"[paths]\n"
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n'
        f'reports_dir = "{(tmp_path / "reports").as_posix()}"\n'
    )


# ---------------------------------------------------------------------------
# Missing / malformed env
# ---------------------------------------------------------------------------


def test_missing_github_token_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No GITHUB_TOKEN in env and no .env → ConfigError names the var."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)

    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        load_config(
            env_path=tmp_path / "missing.env",
            toml_path=tmp_path / "missing.toml",
        )


def test_empty_github_token_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GITHUB_TOKEN=`` (empty value) → ConfigError, not a silent success."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        load_config(env_path=env, toml_path=tmp_path / "missing.toml")


def test_whitespace_only_github_token_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace is not a token. ``.strip()`` should reject it.

    Without stripping, a stray newline in the .env would let an
    empty-but-non-empty string through and break the Collector with
    a confusing 401 from GitHub.
    """
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=   \n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        load_config(env_path=env, toml_path=tmp_path / "missing.toml")


# ---------------------------------------------------------------------------
# Defaults and toml overrides
# ---------------------------------------------------------------------------


def test_no_toml_uses_dataclass_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing config.toml → every tunable keeps its dataclass default.

    chdir to tmp_path so the default ``Path("data")`` / ``Path("reports")``
    land in the temp dir, not in the project root.
    """
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    monkeypatch.chdir(tmp_path)

    cfg = load_config(env_path=env, toml_path=tmp_path / "missing.toml")

    assert cfg.github_token == "ghp_test"
    assert cfg.basic_auth_user == ""
    assert cfg.basic_auth_pass == ""
    assert cfg.data_dir == Path("data")
    assert cfg.reports_dir == Path("reports")
    assert cfg.recent_months == 12
    assert cfg.min_absolute_delta == 100
    assert cfg.min_relative_growth == pytest.approx(0.20)
    assert cfg.web_host == "127.0.0.1"
    assert cfg.web_port == 8000
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "reports").is_dir()


def test_partial_toml_overrides_only_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A toml that sets only ``[viral].min_absolute_delta`` → that one
    field is overridden, every other tunable keeps its default.
    """
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"[viral]\nmin_absolute_delta = 50\n{_paths_section(tmp_path)}",
        encoding="utf-8",
    )

    cfg = load_config(env_path=env, toml_path=toml)

    assert cfg.min_absolute_delta == 50  # overridden
    assert cfg.min_relative_growth == pytest.approx(0.20)  # default
    assert cfg.recent_months == 12  # default
    assert cfg.data_dir == tmp_path / "data"  # toml paths
    assert cfg.web_port == 8000  # default


def test_full_toml_overrides_every_tunable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete toml + populated env → every value reflects the file."""
    env = tmp_path / ".env"
    env.write_text(
        "GITHUB_TOKEN=ghp_test\n"
        "BASIC_AUTH_USER=alice\n"
        "BASIC_AUTH_PASS=s3cr3t\n",
        encoding="utf-8",
    )
    toml = tmp_path / "config.toml"
    toml.write_text(
        "[filter]\nrecent_months = 6\n"
        "\n"
        "[viral]\nmin_absolute_delta = 200\nmin_relative_growth = 0.30\n"
        "\n"
        f"{_paths_section(tmp_path)}",
        encoding="utf-8",
    )

    cfg = load_config(env_path=env, toml_path=toml)

    assert cfg.github_token == "ghp_test"
    assert cfg.basic_auth_user == "alice"
    assert cfg.basic_auth_pass == "s3cr3t"
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.reports_dir == tmp_path / "reports"
    assert cfg.recent_months == 6
    assert cfg.min_absolute_delta == 200
    assert cfg.min_relative_growth == pytest.approx(0.30)
    # web_* are not in the toml — dataclass defaults
    assert cfg.web_host == "127.0.0.1"
    assert cfg.web_port == 8000


# ---------------------------------------------------------------------------
# Directory creation
# ---------------------------------------------------------------------------


def test_data_and_reports_dirs_are_created_with_nested_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-existent nested ``data_dir`` / ``reports_dir`` → mkdir parents=True.

    This is the deploy case: the first run on a fresh VPS hits an
    empty ``/opt/repo-pulse/`` and ``data/`` + ``reports/`` need to
    spring into existence without the user running ``mkdir`` first.
    """
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    deep = tmp_path / "deeply" / "nested" / "subdir"
    data_dir = deep / "data"
    reports_dir = deep / "reports"
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"[paths]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'reports_dir = "{reports_dir.as_posix()}"\n',
        encoding="utf-8",
    )

    cfg = load_config(env_path=env, toml_path=toml)

    assert data_dir.is_dir()
    assert reports_dir.is_dir()
    assert cfg.data_dir == data_dir
    assert cfg.reports_dir == reports_dir


def test_existing_dirs_are_not_recreated_and_do_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``exist_ok=True`` means a re-run is a no-op for the directory check."""
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    data_dir.mkdir()
    reports_dir.mkdir()
    sentinel = data_dir / "sentinel.txt"
    sentinel.write_text("kept", encoding="utf-8")
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"[paths]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'reports_dir = "{reports_dir.as_posix()}"\n',
        encoding="utf-8",
    )

    cfg = load_config(env_path=env, toml_path=toml)

    assert sentinel.read_text(encoding="utf-8") == "kept"  # not nuked
    assert cfg.data_dir == data_dir
    assert cfg.reports_dir == reports_dir


# ---------------------------------------------------------------------------
# Invalid toml
# ---------------------------------------------------------------------------


def test_invalid_toml_raises_config_error_not_tomldecodeerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Syntactically broken toml → ``ConfigError`` (not ``TOMLDecodeError``).

    Callers should be able to ``except ConfigError`` and not care
    about which library produced the underlying parse failure.
    """
    env = _env_with_token(tmp_path)
    toml = tmp_path / "config.toml"
    toml.write_text("not valid toml [[[", encoding="utf-8")

    with pytest.raises(ConfigError, match="config.toml"):
        load_config(env_path=env, toml_path=toml)


def test_wrong_type_in_toml_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bool where an int is expected → ``ConfigError``.

    Without this guard, ``min_absolute_delta = true`` would silently
    become ``1`` (because ``bool`` is a subclass of ``int`` in
    Python). The deploy should fail loudly, not pollute the
    analytics with bogus thresholds.
    """
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"[viral]\nmin_absolute_delta = true\n{_paths_section(tmp_path)}",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="min_absolute_delta"):
        load_config(env_path=env, toml_path=toml)


def test_non_numeric_string_in_toml_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``min_relative_growth = "very fast"`` → ``ConfigError``."""
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"[viral]\nmin_relative_growth = \"very fast\"\n"
        f"{_paths_section(tmp_path)}",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="min_relative_growth"):
        load_config(env_path=env, toml_path=toml)


# ---------------------------------------------------------------------------
# Env precedence and frozen guarantee
# ---------------------------------------------------------------------------


def test_shell_env_wins_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the same var is in both .env and the shell, the shell wins.

    ``python-dotenv`` is called with ``override=False``; a stray
    ``GITHUB_TOKEN`` in the developer's shell (e.g. for an ad-hoc
    curl) must not be silently replaced by the project .env.
    """
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=from_dotenv\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "from_shell")

    cfg = load_config(env_path=env, toml_path=tmp_path / "missing.toml")

    assert cfg.github_token == "from_shell"


def test_basic_auth_defaults_to_empty_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BASIC_AUTH_USER`` / ``BASIC_AUTH_PASS`` are optional.

    The deploy step (ticket 14) needs them to generate ``htpasswd``,
    but a local Collector run doesn't. Defaults of ``""`` keep the
    deploy-only fields from being a precondition for development.
    """
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    monkeypatch.chdir(tmp_path)

    cfg = load_config(env_path=env, toml_path=tmp_path / "missing.toml")

    assert cfg.basic_auth_user == ""
    assert cfg.basic_auth_pass == ""


def test_config_is_frozen_dataclass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Config`` is a frozen dataclass — assignment must raise.

    ``frozen=True`` raises ``dataclasses.FrozenInstanceError`` (a
    subclass of ``AttributeError``). We pin the subclass to catch a
    future regression that swaps the field to a non-frozen
    property or a property with a setter — ``AttributeError``
    alone would not catch that.
    """
    from dataclasses import FrozenInstanceError

    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    monkeypatch.chdir(tmp_path)

    cfg = load_config(env_path=env, toml_path=tmp_path / "missing.toml")

    with pytest.raises(FrozenInstanceError):
        cfg.github_token = "mutated"  # type: ignore[misc]


def test_config_kw_only_minimal_construction() -> None:
    """``Config`` is ``kw_only`` — only ``github_token`` is required.

    The other fields have defaults so a one-off test fixture or a
    future caller that wants to stub a single field doesn't have
    to spell out every tunable.
    """
    cfg = Config(github_token="x")

    assert cfg.github_token == "x"
    assert cfg.basic_auth_user == ""
    assert cfg.recent_months == 12
    assert cfg.data_dir == Path("data")


def test_toml_int_coerces_to_python_int(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An int value in toml must surface as a Python ``int`` (not float/bool)."""
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"[viral]\nmin_absolute_delta = 75\n{_paths_section(tmp_path)}",
        encoding="utf-8",
    )

    cfg = load_config(env_path=env, toml_path=toml)

    assert cfg.min_absolute_delta == 75
    assert isinstance(cfg.min_absolute_delta, int)
    assert type(cfg.min_absolute_delta) is int  # not bool (a subclass)


def test_toml_float_coerces_to_python_float(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A float value in toml must surface as a Python ``float``."""
    env = _env_with_token(tmp_path)
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"[viral]\nmin_relative_growth = 0.15\n{_paths_section(tmp_path)}",
        encoding="utf-8",
    )

    cfg = load_config(env_path=env, toml_path=toml)

    assert cfg.min_relative_growth == pytest.approx(0.15)
    assert isinstance(cfg.min_relative_growth, float)


def test_load_config_uses_cwd_defaults_when_paths_are_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env_path=None`` and ``toml_path=None`` mean ``.env`` /
    ``config.toml`` in CWD — the service-helper pattern.

    To exercise the real defaults we chdir into ``tmp_path`` and
    place files there.
    """
    _env_with_token(tmp_path)
    toml = tmp_path / "config.toml"
    toml.write_text(_paths_section(tmp_path), encoding="utf-8")
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    monkeypatch.chdir(tmp_path)
    # Clear the env that other tests may have set so we test the
    # "only .env" code path.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    cfg = load_config()

    assert cfg.github_token == "ghp_test"
    assert cfg.data_dir == tmp_path / "data"
