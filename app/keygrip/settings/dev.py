from .base import *  # noqa: F401,F403
from .base import INSTALLED_APPS, enable_procrastinate, env

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Browsers DON'T isolate cookies by port, so every app on `localhost` — each parallel `./mc`
# instance (8010–8099), the plain dev runserver, even an unrelated localhost app — shares one
# cookie jar under the host `localhost`. With the default `sessionid` name they clobber each
# other's session, which silently breaks the OIDC handshake (the callback loads a session with no
# `oidc_states` → SuspiciousOperation). Namespace the cookies per instance by web port (unique per
# `mc` instance; injected via MC_WEB_PORT in compose.mc.yml) so the handshakes can't collide.
_cookie_ns = env("MC_WEB_PORT", "") or "dev"
SESSION_COOKIE_NAME = f"kg_sessionid_{_cookie_ns}"
CSRF_COOKIE_NAME = f"kg_csrftoken_{_cookie_ns}"

# Label shown in the "dev instance" banner across the top of every page (web.context_processors)
# so a local/throwaway tab is never mistaken for prod. The `mc` instance name when set (e.g.
# "demo"), else "local" for the bare runserver. Prod leaves this None (base.py) ⇒ no banner.
KG_INSTANCE_LABEL = env("MC_INSTANCE_NAME", "") or "local"

# Opt-in local Postgres so the Procrastinate queue + worker run end-to-end locally (ADR 0008).
# Default stays SQLite — fast, zero deps, and the queue is simply absent (Procrastinate not
# installed). To run the queue locally: `docker compose -f compose.dev.yml up -d`, then set
# DEV_DB=postgres (see .env.example / README). Procrastinate is Postgres-only, so it's enabled
# here only in this branch — keeping plain SQLite dev (and the test suite) untouched.
if env("DEV_DB", "sqlite").lower() == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "keygrip.dbretry",
            "NAME": env("DB_NAME", "keygrip"),
            "USER": env("DB_USER", "keygrip"),
            "PASSWORD": env("DB_PASSWORD", "keygrip"),
            "HOST": env("DB_HOST", "localhost"),
            "PORT": env("DB_PORT", "5433"),  # host port from compose.dev.yml (avoids clashing 5432)
        }
    }
    INSTALLED_APPS = enable_procrastinate(INSTALLED_APPS)
