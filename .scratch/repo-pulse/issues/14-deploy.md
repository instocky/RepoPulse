# 13 — Deploy

**What to build:** Configuration files and a runbook for the one-time VPS installation. After this ticket lands, `git clone` + the README steps = a working set-and-forget deployment.

**Blocked by:** 10 — CLI, 12 — Web, 01 — Project scaffold

**Status:** ready-for-agent

- [ ] `deploy/repo-pulse-snapshot.service` (oneshot) — runs `uv run python -m repo_pulse snapshot` with the right working dir and env
- [ ] `deploy/repo-pulse-snapshot.timer` — daily at 06:00 UTC, `Persistent=true` so missed runs catch up
- [ ] `deploy/repo-pulse-web.service` — runs `uv run python -m repo_pulse serve`, `Restart=on-failure`
- [ ] `deploy/nginx.conf.example` — TLS placeholders, `auth_basic` + `auth_basic_user_file`, reverse proxy to `localhost:8000`
- [ ] `deploy/htpasswd.example.txt` — instructions for creating the htpasswd file (not the file itself — secrets)
- [ ] `deploy/README.md` — step-by-step install:
  1. `git clone` to `/opt/repo-pulse/`
  2. `uv sync`
  3. `cp .env.example .env` and edit
  4. `chmod 600 .env`
  5. Create htpasswd: `htpasswd -c /etc/nginx/.htpasswd <user>`
  6. `cp deploy/nginx.conf.example /etc/nginx/sites-available/repo-pulse` and edit domain
  7. `certbot --nginx -d pulse.example.com`
  8. `systemctl link /opt/repo-pulse/deploy/repo-pulse-snapshot.service` (and timer, and web)
  9. `systemctl enable --now repo-pulse-snapshot.timer repo-pulse-web.service`
  10. `systemctl status` checks
- [ ] No secrets are hardcoded in any config file
- [ ] All paths in the systemd units are absolute (per agent memory: subprocess on Windows inherits `os.environ`; for systemd, we set `EnvironmentFile=/opt/repo-pulse/.env` and explicit `WorkingDirectory=`)
- [ ] `deploy/README.md` notes that the `.env` must be readable by the systemd user (and that on many distros the `www-data` user is fine; on others a dedicated `repo-pulse` user is cleaner)
- [ ] Ticket is a "doc" deliverable — no code, but the doc must be precise enough that a sysadmin can follow it without asking questions
