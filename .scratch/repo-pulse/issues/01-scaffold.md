# 01 — Project scaffold

**What to build:** A working Python project skeleton that another developer can clone and `uv sync` to get a runnable environment. `import repo_pulse` succeeds, deps are declared, sensitive files are gitignored.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] `pyproject.toml` declares Python 3.11+ and direct dependencies (httpx, typer, fastapi, uvicorn, jinja2, python-dotenv, respx for tests, pytest)
- [ ] `src/repo_pulse/__init__.py` exists with `__version__`
- [ ] `src/repo_pulse/__main__.py` exists (will be wired to CLI in ticket 10)
- [ ] `.env.example` lists `GITHUB_TOKEN`, `BASIC_AUTH_USER`, `BASIC_AUTH_PASS`
- [ ] `.gitignore` excludes `data/`, `logs/`, `reports/`, `.env`, `__pycache__/`, `.venv/`, `*.db`, `*.pyc`
- [ ] `README.md` is a placeholder with the project name and one-line description
- [ ] `uv sync` succeeds in a clean clone
- [ ] `python -c "import repo_pulse"` succeeds
- [ ] `pytest` runs (no tests yet, but the runner works)


Respects 00-architecture doctrine
