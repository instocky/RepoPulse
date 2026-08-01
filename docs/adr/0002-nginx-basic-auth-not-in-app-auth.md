# 0002 — HTTP Basic Auth at nginx, not in the application

The web dashboard's authentication lives in the nginx config
(`auth_basic` + `auth_basic_user_file`), not in the FastAPI app.

The password never enters the Python process. The application code
has no notion of who the user is. Pros: zero auth surface in the
application, the only secrets to manage are the htpasswd file on the
VPS. Cons: password sits in a config file (mode 0640) and basic auth
over HTTPS is the only transport.

This is the right shape for a single-user personal tool on a private
subdomain. If multi-user support is ever needed, the path is to add
session-based auth in the app and keep nginx doing TLS termination —
not to extend Basic Auth.
