# 02 — Config

**What to build:** A typed `Config` object that loads from `.env` (required secrets) and `config.toml` (tunable parameters). Missing `GITHUB_TOKEN` raises a clear error before any other code runs. Defaults from the spec are used when `config.toml` is missing.

**Blocked by:** 01 — Project scaffold

**Status:** ready-for-agent

- [ ] `load_config(env_path=None, toml_path=None) -> Config` returns a frozen dataclass
- [ ] `Config` has: `github_token: str`, `basic_auth_user: str`, `basic_auth_pass: str`, `data_dir: Path`, `reports_dir: Path`, `recent_months: int` (default 12), `min_absolute_delta: int` (default 100), `min_relative_growth: float` (default 0.20), `web_host: str` (default "127.0.0.1"), `web_port: int` (default 8000)
- [ ] Missing `GITHUB_TOKEN` raises `ConfigError` with a message naming the env var
- [ ] Missing `config.toml` is not an error — defaults apply
- [ ] `config.toml` values override dataclass defaults
- [ ] `data_dir` and `reports_dir` are created if missing (mkdir parents=True, exist_ok=True)
- [ ] Tests cover: missing env, partial config, full config, no config.toml, invalid toml
- [ ] `python-dotenv` reads `.env` directly (memory note: copy right-hand side of `=` only, no type annotations)
- [ ] Service-helper pattern: helpers accept `path: Path | None = None` and read env internally (per agent memory)
