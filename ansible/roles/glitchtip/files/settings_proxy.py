# GlitchTip settings override (DJANGO_SETTINGS_MODULE=glitchtip.settings_proxy). Mounted read-only.
# GlitchTip runs on Granian, which can't forward the proxy proto, and GlitchTip doesn't set
# SECURE_PROXY_SSL_HEADER — so behind Tailscale Serve (TLS-terminating proxy → plain HTTP origin)
# Django would always think it's HTTP, breaking the OIDC redirect_uri and secure-cookie/CSRF flow.
from glitchtip.settings import *  # noqa: F401,F403

# Trust the proxy's X-Forwarded-Proto (Tailscale Serve sets it) → request.is_secure() == True.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"

# SSO account linking: a Keycloak login whose verified email matches an existing account logs into
# (and connects to) that account — so the first SSO sign-in adopts the bootstrap user instead of
# colliding on the email.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

# Relax GlitchTip's OWN style CSP. Its Angular SPA applies inline styles at runtime; GlitchTip's
# style-src is `'self' <nonce>`, and a present nonce makes the browser IGNORE 'unsafe-inline', so
# those styles are blocked and the UI renders broken. This is an internal, tailnet-only tool, so we
# drop the nonce from style-src (letting 'unsafe-inline' apply). script-src keeps its nonce. This
# touches only GlitchTip's pages, never the Keygrip app's strict CSP.
from django.utils.csp import CSP as _CSP  # noqa: E402

for _csp_name in ("SECURE_CSP", "SECURE_CSP_REPORT_ONLY"):
    _csp = globals().get(_csp_name)
    if isinstance(_csp, dict) and "style-src" in _csp:
        globals()[_csp_name] = {**_csp, "style-src": [_CSP.SELF, _CSP.UNSAFE_INLINE]}
