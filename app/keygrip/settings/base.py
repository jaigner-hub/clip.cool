"""Base settings. Env-driven; secrets via .env locally (SOPS-managed elsewhere)."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# app/  (manage.py lives here)
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return str(env(key, str(default))).lower() in ("1", "true", "yes", "on")


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django_prometheus",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "mozilla_django_oidc",
    "ninja",
    "hijack",
    "hijack.contrib.admin",  # adds an "Impersonate" button to the admin user list
    "web",
    "tenancy",
    "recommendations",
]

def enable_procrastinate(installed_apps):
    """Insert the Procrastinate Django app (ADR 0008) just before our own apps, so it readies
    before their `tasks.py` autodiscover. Procrastinate is Postgres-only — callers enable it only
    when DATABASES['default'] is Postgres: always in prod, opt-in (DEV_DB=postgres) for local dev."""
    i = installed_apps.index("web")
    return [*installed_apps[:i], "procrastinate.contrib.django", *installed_apps[i:]]


MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",  # first
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "hijack.middleware.HijackUserMiddleware",  # marks request.user.is_hijacked (before org resolve)
    "tenancy.middleware.OrganizationMiddleware",  # request.organization (needs request.user)
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "web.middleware.CSPMiddleware",  # strict nonce-based CSP + Permissions-Policy on every response
    "django_prometheus.middleware.PrometheusAfterMiddleware",  # last
]

# --- Security response headers (mirror the static marketing/portal `_headers`) ---
# SecurityMiddleware (above) emits these two when set; they're env-independent so they live in
# base so dev mirrors prod. HSTS is intentionally NOT set here — Cloudflare terminates TLS and
# owns the HSTS header for every *.vent.dog surface at the edge.
SECURE_CONTENT_TYPE_NOSNIFF = True  # X-Content-Type-Options: nosniff
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
# Permissions-Policy has no Django built-in; CSPMiddleware emits this value. Empty allow-lists
# `()` deny the feature to every origin (incl. self). Matches marketing/public/_headers.
PERMISSIONS_POLICY = "geolocation=(), microphone=(), camera=()"

ROOT_URLCONF = "keygrip.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "tenancy.context_processors.org_admin",
                "web.context_processors.admin_link",
                "web.context_processors.instance_banner",
            ]
        },
    }
]
WSGI_APPLICATION = "keygrip.wsgi.application"
ASGI_APPLICATION = "keygrip.asgi.application"

DATABASES = {
    "default": {"ENGINE": "django_prometheus.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
}

# OIDC-only — no local passwords.
AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- OIDC against the Keycloak keygrip realm (mozilla-django-oidc) ---
# OIDC is primary (tried first). ModelBackend is the deliberate, audited break-glass exception
# to "Keycloak sole auth / no Django passwords" (ADR 0002): a single local superuser must be able
# to log into /admin when Keycloak/Google SSO is unavailable. It is safe to enable globally
# because every OIDC-provisioned user gets set_unusable_password() (web/auth.py), so ModelBackend
# rejects them — only an account with a real (usable) password can authenticate locally, and by
# policy that is exactly one break-glass account on a dedicated non-SSO email.
AUTHENTICATION_BACKENDS = [
    "web.auth.KeygripOIDCBackend",
    "django.contrib.auth.backends.ModelBackend",
]
LOGIN_URL = "/oidc/authenticate/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Where staff-facing "Admin" links point — same-origin `/admin/` everywhere (the admin is served
# on the public app host; staff get in via their normal Keycloak SSO session). Kept env-overridable
# for odd topologies. Exposed to templates by web.context_processors.admin_link.
ADMIN_URL = env("DJANGO_ADMIN_URL", "/admin/")

# ADR 0002 — hostnames (port ignored if given) where the Django admin *password* login form is
# served: the break-glass path. Empty (dev/mc/tests) ⇒ the form renders on any host. In prod this
# is the Tailscale Serve hostname only — on every other host /admin/login/ neither renders nor
# processes the form (web.views.admin_login_gate sends anonymous users through OIDC instead), so
# the break-glass credential is unusable from the public internet even though /admin itself is
# public.
BREAK_GLASS_LOGIN_HOSTS = [h for h in env("DJANGO_BREAK_GLASS_LOGIN_HOSTS", "").split(",") if h]

# "Dev instance" banner label. None ⇒ no banner (prod). dev.py sets it to the local/mc instance
# name so a throwaway local tab is never mistaken for production. Exposed via web.context_processors.
KG_INSTANCE_LABEL = None

_ISSUER = env("KEYCLOAK_ISSUER", "https://id.vent.dog/realms/keygrip").rstrip("/")
KEYCLOAK_ISSUER = _ISSUER  # used by the JSON API bearer auth (web.api_auth)
# Logout posts to /oidc/logout/ which 302s to Keycloak; CSP form-action is enforced across
# that redirect, so the Keycloak origin must be allowed.
_kc = urlsplit(_ISSUER)
CSP_FORM_ACTION_EXTRA = [f"{_kc.scheme}://{_kc.netloc}"]
OIDC_RP_CLIENT_ID = env("OIDC_RP_CLIENT_ID", "keygrip-web")
OIDC_RP_CLIENT_SECRET = env("OIDC_RP_CLIENT_SECRET", "")
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_RP_SCOPES = "openid email profile"
OIDC_USERNAME_ALGO = "web.auth.generate_username"   # username = email, not a hash
OIDC_OP_AUTHORIZATION_ENDPOINT = f"{_ISSUER}/protocol/openid-connect/auth"
OIDC_OP_TOKEN_ENDPOINT = f"{_ISSUER}/protocol/openid-connect/token"
OIDC_OP_USER_ENDPOINT = f"{_ISSUER}/protocol/openid-connect/userinfo"
OIDC_OP_JWKS_ENDPOINT = f"{_ISSUER}/protocol/openid-connect/certs"
OIDC_OP_LOGOUT_ENDPOINT = f"{_ISSUER}/protocol/openid-connect/logout"
# Store the id_token so logout can pass id_token_hint -> ends the Keycloak SSO session too.
OIDC_STORE_ID_TOKEN = True
OIDC_OP_LOGOUT_URL_METHOD = "web.auth.provider_logout"
# Clock-skew slack (seconds) for ID-token iat/nbf/exp validation. mozilla-django-oidc passes
# no leeway to PyJWT (leeway=0), so a token whose `iat` is even a second ahead of this box's
# clock raises ImmatureSignatureError. Keycloak (vent.dog) and the app run on different hosts;
# a few seconds of skew is normal. Consumed by web.auth.KeygripOIDCBackend._verify_jws.
OIDC_CLOCK_SKEW_LEEWAY = int(env("OIDC_CLOCK_SKEW_LEEWAY", "30"))

# --- JSON API (ADR 0011) ---
# Keycloak access-token audience verification is off until a proper aud mapper is configured.
API_VERIFY_AUD = env_bool("API_VERIFY_AUD", False)
# JWKS source for bearer-token validation. Defaults to the public endpoint, but prod points
# this at the INTERNAL Keycloak (http://keycloak:8080/...) to bypass Cloudflare — PyJWT fetches
# JWKS with urllib, whose User-Agent Cloudflare's bot protection 403s (login works only because
# mozilla-django-oidc uses the requests library, which Cloudflare allows).
API_JWKS_URL = env("API_JWKS_URL", OIDC_OP_JWKS_ENDPOINT)

# App -> Keycloak Admin API, to mint/rotate/revoke customer service-account clients
# (self-serve API credentials, ADR 0011). Internal URL skips Cloudflare.
KEYCLOAK_ADMIN_BASE = env("KEYCLOAK_ADMIN_BASE", "http://keycloak:8080").rstrip("/")
KEYCLOAK_REALM = env("KEYCLOAK_REALM", "keygrip")
KC_ADMIN_CLIENT_ID = env("KC_ADMIN_CLIENT_ID", "keygrip-kc-admin")
KC_ADMIN_CLIENT_SECRET = env("KC_ADMIN_CLIENT_SECRET", "")
# Strict CSP is global; the Swagger UI explorer self-hosts its JS but injects inline styles,
# so relax CSP ONLY on the docs path. Every real app page stays strict.
CSP_RELAXED_PREFIXES = ["/api/v1/docs"]

# --- Recommendations API → OpenRouter (ADR 0013) ---
# Same provider as the old zrag system: Claude models via OpenRouter's OpenAI-compatible API.
# Key is SOPS-managed in prod (see docs/adr/0001-secrets-management.md inventory); unset locally
# ⇒ generation raises a clear LLMError (the rest of the app still runs).
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", "")
OPENROUTER_REFERER = env("OPENROUTER_REFERER", "https://app.vent.dog")  # OpenRouter attribution
OPENROUTER_TITLE = env("OPENROUTER_TITLE", "Keygrip Recommendations")

# --- Staff impersonation (django-hijack; ADR 0010) ---
# Superuser-only — platform staff only, never org admins. Explicit (also the library default).
HIJACK_PERMISSION_CHECK = "hijack.permissions.superusers_only"

# --- Email --- dev prints to the console; prod overrides with Resend SMTP.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "noreply@vent.dog")

# --- Marketing contact form (ADR 0012) --- public endpoint redirects the browser here after submit.
CONTACT_EMAIL = env("CONTACT_EMAIL", "jeff.aigner@gmail.com")
CONTACT_THANKS_URL = env("CONTACT_THANKS_URL", "https://vent.dog/thanks")

# --- Error tracking (Sentry SDK → self-hosted GlitchTip; no-op without a DSN) ---
# Backend-agnostic: the SDK talks to any Sentry-compatible ingest, so the DSN can point at
# GlitchTip now and Sentry SaaS later. Unset (e.g. local dev) ⇒ init never runs. DjangoIntegration
# is auto-enabled by sentry-sdk 2.x. send_default_pii stays off (don't leak user/tenant data).
SENTRY_DSN = env("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk

    # Keep Sentry for ERRORS only. When OpenTelemetry tracing is active (OTEL_EXPORTER_OTLP_ENDPOINT
    # set → Alloy/Tempo, ADR 0015) drop Sentry's own trace sampling to 0 so we don't pay for /
    # duplicate two trace pipelines; Tempo is the trace store (infra-gap-analysis.md #7).
    _otel_on = bool(env("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env("SENTRY_ENVIRONMENT", "production"),
        release=env("SENTRY_RELEASE", "") or None,
        traces_sample_rate=0.0 if _otel_on else float(env("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        send_default_pii=False,
    )

# --- Logging --- everything to stdout (containers; Alloy ships stdout → Loki). disable_existing
# _loggers=False keeps Django/uvicorn loggers intact. The dedicated `web.security` logger is the
# audit channel for security-significant events (e.g. break-glass logins, ADR 0002) — it must
# always reach stdout so Loki captures it and the Loki ruler can alert. propagate=False avoids
# double emission via root.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "web.security": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
