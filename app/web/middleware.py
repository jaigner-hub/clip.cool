"""Strict, nonce-based Content-Security-Policy (+ Permissions-Policy) on every response.

Self-hosted only ('self'); inline <script> must carry the per-request nonce
({{ request.csp_nonce }}). No 'unsafe-inline' / 'unsafe-eval' — which is exactly why
we use Alpine's CSP build and avoid inline `hx-on:` handlers (see CLAUDE.md).

Permissions-Policy has no Django built-in, so we emit it here from settings.PERMISSIONS_POLICY.
(nosniff / Referrer-Policy / X-Frame-Options ride Django's own SecurityMiddleware + clickjacking
middleware; HSTS is owned by Cloudflare at the edge.)
"""
import secrets

from django.conf import settings


class CSPMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        # The Swagger UI explorer (/api/v1/docs) self-hosts its JS but injects inline styles;
        # relax CSP ONLY on those paths. Every real app page stays strict (no escape hatches).
        relaxed = any(
            request.path.startswith(p) for p in getattr(settings, "CSP_RELAXED_PREFIXES", [])
        )
        # setdefault: a view may set its own policy (e.g. report-only) and win.
        response.setdefault("Content-Security-Policy", self.policy(request.csp_nonce, relaxed))
        permissions_policy = getattr(settings, "PERMISSIONS_POLICY", "")
        if permissions_policy:
            response.setdefault("Permissions-Policy", permissions_policy)
        if relaxed:
            # COOP and the Swagger OAuth popup (Django defaults COOP to same-origin):
            #  - docs page (the opener) needs `same-origin-allow-popups` so it can open the popup
            #    and keep a reference to it.
            #  - the redirect handler must be `unsafe-none`: the popup returns from Keycloak
            #    (unsafe-none), and a non-unsafe-none COOP here triggers a browsing-context-group
            #    swap that nulls window.opener (unsafe-none -> allow-popups is a mismatch).
            if request.path.endswith("/oauth2-redirect.html"):
                response["Cross-Origin-Opener-Policy"] = "unsafe-none"
            else:
                response["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        return response

    @staticmethod
    def policy(nonce, relaxed=False):
        # form-action is enforced across redirects: the logout form posts to /oidc/logout/
        # which 302s to the Keycloak end-session endpoint, so that origin must be allowed.
        form_action = " ".join(["'self'", *getattr(settings, "CSP_FORM_ACTION_EXTRA", [])])
        kc = getattr(settings, "CSP_FORM_ACTION_EXTRA", [])  # the Keycloak origin
        # Extra origins the media UI needs (clip.cool): R2 for the presigned upload/download
        # fetches (connect-src) and for serving image/poster bytes (img-src). Empty until R2 is set.
        connect_extra = getattr(settings, "CSP_CONNECT_SRC_EXTRA", [])
        img_extra = getattr(settings, "CSP_IMG_SRC_EXTRA", [])
        if relaxed:
            script_src = "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
            style_src = "style-src 'self' 'unsafe-inline'"
            # Swagger's OAuth2 token exchange fetches Keycloak's token endpoint from this page.
            connect_src = " ".join(["connect-src 'self'", *kc, *connect_extra])
        else:
            script_src = f"script-src 'self' 'nonce-{nonce}'"
            style_src = "style-src 'self'"
            connect_src = " ".join(["connect-src 'self'", *connect_extra])
        return "; ".join([
            "default-src 'self'",
            script_src,
            style_src,
            " ".join(["img-src 'self' data:", *img_extra]),
            # <video>: R2 renditions (img_extra) + blob: so the tab-recorder can preview the
            # just-captured clip (URL.createObjectURL) before it's uploaded.
            " ".join(["media-src 'self' blob:", *img_extra]),
            "font-src 'self'",
            connect_src,
            "object-src 'none'",
            "base-uri 'self'",
            "frame-ancestors 'self'",
            f"form-action {form_action}",
        ])
