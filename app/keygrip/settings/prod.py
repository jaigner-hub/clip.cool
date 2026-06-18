"""Production settings — DEBUG off, Postgres, behind cloudflared/Cloudflare (TLS at the edge)."""
from .base import *  # noqa: F401,F403
from .base import BASE_DIR, INSTALLED_APPS, MIDDLEWARE, enable_procrastinate, env

DEBUG = False

# --- Async task queue: Procrastinate (ADR 0008) ---
# Always on in prod (Postgres). Auto-registers a read-only Django admin for jobs + workers (the
# monitoring surface at /admin/procrastinate/ and /admin/web/).
# Worker: `manage.py procrastinate worker --queues default,batch,workflow,aeo` (compose `worker`).
# The DjangoConnector reuses DATABASES["default"]; keep that a direct/session connection so the
# worker's LISTEN/NOTIFY isn't pinned behind a transaction-mode pooler (ADR 0008 watch-out).
INSTALLED_APPS = enable_procrastinate(INSTALLED_APPS)
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# Cloudflare terminates TLS; cloudflared forwards X-Forwarded-Proto.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
# The break-glass /admin path (ADR 0002) is served over the tailnet via Tailscale Serve on a
# non-443 port (:8447). Trust the forwarded port so request.build_absolute_uri() (and thus the
# OIDC callback) carries that port and round-trips against the registered keygrip-web redirect
# URI. Harmless behind cloudflared (:443).
USE_X_FORWARDED_PORT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

DATABASES = {
    "default": {
        "ENGINE": "keygrip.dbretry",
        "NAME": env("DB_NAME", "keygrip"),
        "USER": env("DB_USER", "keygrip"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "appdb"),
        "PORT": env("DB_PORT", "5432"),
        # The long-lived Procrastinate worker can hold a connection the server later drops
        # (PgBouncer/HAProxy restart, leader switchover). Health-check the connection on reuse so a
        # dead one is detected + reconnected instead of erroring mid-query; recycle every 60s. Tasks
        # also close_old_connections() at start (clips/tasks.py) to trigger the check per job.
        "CONN_HEALTH_CHECKS": True,
        "CONN_MAX_AGE": 60,
    }
}

# WhiteNoise serves static (admin assets etc.) — insert right after SecurityMiddleware
# (not index 0: PrometheusBeforeMiddleware is first).
_sec = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
MIDDLEWARE = MIDDLEWARE[:_sec + 1] + ["whitenoise.middleware.WhiteNoiseMiddleware"] + MIDDLEWARE[_sec + 1:]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Email via Resend SMTP relay (ADR 0005).
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", "smtp.resend.com")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "resend")
EMAIL_HOST_PASSWORD = env("RESEND_API_KEY", "")
EMAIL_USE_TLS = True
SERVER_EMAIL = DEFAULT_FROM_EMAIL
