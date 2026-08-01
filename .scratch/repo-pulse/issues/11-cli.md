# 10 — CLI

**What to build:** A typer-based CLI with three subcommands: `snapshot` (run the Collector), `serve` (run the Web app), `status` (show last run, repo count, snapshot count). The developer-facing entry point.

**Blocked by:** 07 — Collector, 02 — Config

**Status:** ready-for-agent

- [ ] `python -m repo_pulse snapshot` runs the Collector for today
- [ ] `python -m repo_pulse snapshot --date 2026-08-01` runs for a specific date
- [ ] `python -m repo_pulse serve` starts uvicorn on `config.web_host:config.web_port`
- [ ] `python -m repo_pulse status` prints: last run date, watchlist size, total snapshot count, viral events in last 7 days
- [ ] `python -m repo_pulse --help` shows all three subcommands with descriptions
- [ ] Errors print to stderr with non-zero exit code
- [ ] `snapshot` returns exit code 0 on full success, 1 on any failure
- [ ] Logs go to `logs/YYYY-MM-DD.log` (daily file, no rotation in v0)
- [ ] Tests use `typer.testing.CliRunner`
- [ ] Tests cover: each subcommand happy path, snapshot with --date flag, status with empty DB
- [ ] The CLI does NOT take the PAT as an argument — it reads from config (which reads from `.env`)


Respects 00-architecture doctrine
