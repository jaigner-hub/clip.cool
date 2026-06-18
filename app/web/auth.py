"""OIDC backend that maps Keycloak keygrip-realm roles onto the Django user.

The `roles` claim is the composite-expanded realm roles (admin ⊃ developer ⊃ viewer),
emitted by the keygrip-web client's realm-roles mapper.
"""
from urllib.parse import urlencode

import jwt
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import SuspiciousOperation
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.views import OIDCAuthenticationRequestView

KEYGRIP_ROLES = {"admin", "developer", "viewer", "staging-access"}


def generate_username(email):
    """Use the email as the Django username (mozilla-django-oidc OIDC_USERNAME_ALGO)."""
    return email


def provider_logout(request):
    """RP-initiated logout URL — ends the Keycloak SSO session (not just Django's)."""
    redirect_uri = request.build_absolute_uri(settings.LOGOUT_REDIRECT_URL)
    params = {"post_logout_redirect_uri": redirect_uri, "client_id": settings.OIDC_RP_CLIENT_ID}
    id_token = request.session.get("oidc_id_token")
    if id_token:
        params["id_token_hint"] = id_token
    return f"{settings.OIDC_OP_LOGOUT_ENDPOINT}?{urlencode(params)}"


class KeycloakPasswordChangeView(OIDCAuthenticationRequestView):
    """Change-password via Keycloak's Application Initiated Action (kc_action).

    Keycloak is the sole password authority (no Django passwords), so the app never sees
    the password: this just re-runs the normal OIDC authorize redirect with
    kc_action=UPDATE_PASSWORD. With an SSO session Keycloak skips the login form, shows its
    (brand-themed) update-password screen — re-prompting for the current password if the
    session auth is stale — then returns through the standard /oidc/callback/. Also lets
    Google-brokered users set a first Keycloak password.
    """

    def get_extra_params(self, request):
        return {**super().get_extra_params(request), "kc_action": "UPDATE_PASSWORD"}


class KeygripOIDCBackend(OIDCAuthenticationBackend):
    def _verify_jws(self, payload, key):
        """Verify the ID token, tolerating small Keycloak<->app clock skew.

        Upstream (mozilla-django-oidc 5.0.2) calls jwt.decode() with no leeway, so a token
        whose `iat` is a second or two ahead of this box's clock raises ImmatureSignatureError
        and login fails intermittently. Keycloak runs on a different host (vent.dog) than the
        app, so a few seconds of skew is normal; OIDC_CLOCK_SKEW_LEEWAY gives PyJWT slack on
        iat/nbf/exp. Mirrors upstream's alg check otherwise.
        """
        header = jwt.get_unverified_header(payload)
        alg = header.get("alg")
        if not alg:
            raise SuspiciousOperation("No alg value found in header")
        if alg != self.OIDC_RP_SIGN_ALGO:
            raise SuspiciousOperation(
                f"The provider algorithm {alg!r} does not match the client's OIDC_RP_SIGN_ALGO."
            )
        return jwt.decode(
            payload,
            key,
            algorithms=alg,
            options={"verify_aud": False},
            leeway=settings.OIDC_CLOCK_SKEW_LEEWAY,
        )

    def _sync(self, user, claims):
        email = claims.get("email", "")
        if email:
            user.username = email          # also fixes pre-existing hashed usernames on next login
            user.email = email
        user.first_name = claims.get("given_name", "") or user.first_name
        user.last_name = claims.get("family_name", "") or user.last_name

        roles = set(claims.get("roles", [])) & KEYGRIP_ROLES
        # Django superuser/staff mirror the keygrip tiers (so the Django admin reflects them).
        user.is_superuser = "admin" in roles
        user.is_staff = bool(roles & {"admin", "developer"})
        user.set_unusable_password()
        user.save()

        groups = [Group.objects.get_or_create(name=r)[0] for r in roles]
        user.groups.set(groups)
        return user

    def create_user(self, claims):
        return self._sync(super().create_user(claims), claims)

    def update_user(self, user, claims):
        return self._sync(user, claims)
