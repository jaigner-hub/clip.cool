"""Change-password rides Keycloak's AIA (kc_action) — the app never touches the password.

Keycloak is the sole password authority (set_unusable_password on every OIDC user, ADR 0002),
so a password change must NOT be a Django form: it is a redirect into the normal OIDC authorize
flow with kc_action=UPDATE_PASSWORD, returning through the standard callback. These tests pin
that contract so a refactor can't quietly turn this into an app-side password handler.
"""
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class PasswordChangeRedirectTests(TestCase):
    def _redirect_params(self, query_string=""):
        resp = self.client.get(reverse("account_password") + query_string)
        self.assertEqual(resp.status_code, 302)
        url = urlsplit(resp["Location"])
        return url, parse_qs(url.query)

    def test_redirects_into_the_oidc_authorize_flow_with_kc_action(self):
        # The whole feature is this redirect: Keycloak's authorize endpoint, the normal
        # code-flow params, plus kc_action=UPDATE_PASSWORD. If kc_action is missing the user
        # just silently re-logs-in; if the endpoint/params drift the callback can't complete.
        url, params = self._redirect_params()
        from django.conf import settings
        endpoint = urlsplit(settings.OIDC_OP_AUTHORIZATION_ENDPOINT)
        self.assertEqual((url.scheme, url.netloc, url.path), (endpoint.scheme, endpoint.netloc, endpoint.path))
        self.assertEqual(params["kc_action"], ["UPDATE_PASSWORD"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["client_id"], [settings.OIDC_RP_CLIENT_ID])
        self.assertTrue(params["redirect_uri"][0].endswith(reverse("oidc_authentication_callback")))
        self.assertIn("state", params)  # CSRF binding for the round-trip

    def test_next_returns_the_user_where_they_were(self):
        # The header link passes ?next=<current page>; mozilla-django-oidc stores it in the
        # session and the callback redirects there, so changing a password doesn't dump the
        # user back at the home page.
        self._redirect_params("?next=/settings/api-credentials/")
        self.assertEqual(self.client.session["oidc_login_next"], "/settings/api-credentials/")

    def test_header_offers_the_password_link(self):
        # Discoverability is the point of the feature ("no way to change password... visibly
        # in the app") — the link must be in the account header for every signed-in user.
        user = User.objects.create_user("u@example.com", "u@example.com")
        user.is_superuser = True  # superuser home renders without an org membership
        user.save()
        self.client.force_login(user)
        resp = self.client.get(reverse("home"))
        self.assertContains(resp, reverse("account_password"))
