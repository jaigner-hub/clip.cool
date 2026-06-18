"""Keycloak bearer-token auth for the JSON API (ADR 0011).

API clients send `Authorization: Bearer <Keycloak JWT>`. We validate the signature
against Keycloak's JWKS (+ exp + issuer), then resolve the token to the matching Django user
(the token must carry `email` — i.e. a user token). Machine/client-credentials tokens are not
accepted; the clip API is user-scoped. No custom credential store — Keycloak is sole auth.

`decode_keycloak_token` is a module-level seam so tests can patch it (hermetic — no
live Keycloak in CI; the real JWKS path is exercised by the live deploy check).
"""
import logging

import jwt
from jwt import PyJWKClient
from django.conf import settings
from django.contrib.auth import get_user_model
from ninja.security.base import AuthBase

logger = logging.getLogger(__name__)

_jwks_client = None


def _jwks():
    global _jwks_client
    if _jwks_client is None:
        # Non-urllib User-Agent so a Cloudflare-fronted JWKS URL isn't 403'd by bot protection;
        # in prod API_JWKS_URL points at internal Keycloak and skips Cloudflare entirely.
        _jwks_client = PyJWKClient(
            settings.API_JWKS_URL, headers={"User-Agent": "keygrip-api/1.0"}
        )
    return _jwks_client


def jwks_reachable():
    """True if Keycloak's JWKS endpoint is fetchable right now — the readiness signal consumed by
    /readyz (web/health.py). Forces a refresh so a previously-cached key set doesn't mask Keycloak
    being down. Module-level so tests can patch it (hermetic — no live Keycloak in CI), mirroring
    `decode_keycloak_token`."""
    try:
        _jwks().get_jwk_set(refresh=True)
        return True
    except Exception:
        # warning, not error: Keycloak being briefly unreachable is an operational/transient
        # condition, not a code bug (CLAUDE.md log-level discipline).
        logger.warning("readyz: Keycloak JWKS unreachable", exc_info=True)
        return False


def decode_keycloak_token(token):
    """Validate a Keycloak JWT and return its claims. Raises on any failure."""
    signing_key = _jwks().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=settings.KEYCLOAK_ISSUER,
        # Keycloak access-token `aud` depends on client scope mappers; off by default
        # until a proper audience mapper is configured (ADR 0011 follow-up).
        options={"verify_aud": getattr(settings, "API_VERIFY_AUD", False)},
    )


class KeycloakAuth(AuthBase):
    """Validates a Keycloak `Authorization: Bearer <JWT>`. Advertised in OpenAPI as an
    OAuth2 authorization-code (PKCE) scheme so the Swagger 'Authorize' button drives the
    Keycloak login directly — but acceptance only depends on the bearer token, however
    obtained (paste also works)."""

    openapi_type = "oauth2"
    openapi_flows = {
        "authorizationCode": {  # interactive users (Swagger Authorize)
            "authorizationUrl": settings.OIDC_OP_AUTHORIZATION_ENDPOINT,
            "tokenUrl": settings.OIDC_OP_TOKEN_ENDPOINT,
            "scopes": {"openid": "OpenID", "email": "Email", "profile": "Profile"},
        },
        "clientCredentials": {  # machine-to-machine (partner/script service accounts)
            "tokenUrl": settings.OIDC_OP_TOKEN_ENDPOINT,
            "scopes": {},
        },
    }

    def __call__(self, request):
        parts = request.headers.get("Authorization", "").split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        return self.authenticate(request, parts[1])

    def authenticate(self, request, token):
        try:
            claims = decode_keycloak_token(token)
        except Exception:
            return None  # -> 401
        # User tokens only: a valid Keycloak user JWT (carries `email`) → the matching Django user.
        # Machine (client-credentials) tokens are not accepted — the clip API is user-scoped; a
        # service-account path can be re-added when there's a consumer for it.
        email = claims.get("email")
        if not email:
            return None  # not a user token
        user = get_user_model().objects.filter(username=email, is_active=True).first()
        if user is None:
            return None  # valid token but user not provisioned (staff onboarding, ADR 0009)
        request.user = user
        return user
