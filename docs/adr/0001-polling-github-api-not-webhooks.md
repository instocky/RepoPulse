# 0001 — Polling the GitHub API, not webhooks

Repo-Pulse gets repo vital signs by polling `GET /repos/{owner}/{repo}`
once per day, not by receiving webhook deliveries from GitHub.

Polling matches the deployment shape: a one-shot daily job on a VPS
that no public Internet service talks to. Webhooks would require a
publicly reachable endpoint with TLS, a webhook secret, retry handling
for delivery failures, and verification of GitHub's signature header.
For a "set and forget" personal tool with a 24h detection latency
budget, the simpler shape wins.

Trade-off: a viral event can only be detected at the next scheduled
poll (up to 24h late). For personal research this is acceptable; if
faster detection becomes important, the path is to add a second timer
that polls the top-N repos hourly, not to switch to webhooks.
