"""Host-gated admin password login (ADR 0002). WHY: /admin is public on app.vent.dog like the
rest of the app (staff enter via their Keycloak SSO session — no domain change, no second cookie
jar), but the single local break-glass credential must stay usable ONLY over the Tailscale admin
plane. /admin/login/ is the app's only password-accepting endpoint, so these tests are the
guardrail: the form must neither render nor authenticate on a public host, while the tailnet
(break-glass) host keeps the stock form so recovery works when Keycloak is down.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

User = get_user_model()

TAILNET = "vent-keygrip.tail.example.ts.net:8447"
LOGIN = "/admin/login/"


class PublicHostTests(TestCase):
    """On a non-break-glass host the password path must be dead in every state."""

    def setUp(self):
        # The test client's host is "testserver" — NOT in the allowed list, i.e. the public side.
        self.gate = override_settings(BREAK_GLASS_LOGIN_HOSTS=[TAILNET])
        self.gate.enable()
        self.addCleanup(self.gate.disable)

    def test_anonymous_is_sent_through_oidc_not_a_password_form(self):
        resp = self.client.get(LOGIN, {"next": "/admin/web/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/oidc/authenticate/?next=/admin/web/")

    def test_password_post_never_authenticates(self):
        # The core ADR 0002 assertion: even with the CORRECT break-glass credentials, a public
        # POST must not log in — otherwise the tailnet-only property is theatre.
        User.objects.create_superuser("breakglass@vent.dog", email="breakglass@vent.dog", password="s3cret!")
        resp = self.client.post(LOGIN, {"username": "breakglass@vent.dog", "password": "s3cret!"})
        self.assertEqual(resp.status_code, 302)  # bounced to OIDC, not processed
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_evil_next_falls_back_to_admin_index(self):
        resp = self.client.get(LOGIN, {"next": "https://evil.example/phish"})
        self.assertEqual(resp["Location"], "/oidc/authenticate/?next=/admin/")

    def test_authenticated_staff_skip_straight_to_target(self):
        # An SSO-session staff user hitting the login URL shouldn't see any login at all.
        staff = User.objects.create_user("staff@vent.dog", email="staff@vent.dog", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(LOGIN, {"next": "/admin/web/"})
        self.assertRedirects(resp, "/admin/web/", fetch_redirect_response=False)

    def test_authenticated_non_staff_is_refused_not_looped(self):
        # Without this, non-staff would ping-pong /admin/ -> login -> /admin/ forever.
        user = User.objects.create_user("member@vent.dog", email="member@vent.dog")
        self.client.force_login(user)
        self.assertEqual(self.client.get(LOGIN).status_code, 403)


class BreakGlassHostTests(TestCase):
    """On the break-glass host (and in dev, where the list is empty) the stock form survives —
    it must work with Keycloak fully down, so it cannot depend on OIDC."""

    def test_allowed_host_renders_the_password_form(self):
        with override_settings(BREAK_GLASS_LOGIN_HOSTS=["testserver"], ALLOWED_HOSTS=["testserver"]):
            resp = self.client.get(LOGIN)
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'name="password"')

    def test_allowed_host_accepts_the_break_glass_password(self):
        User.objects.create_superuser("breakglass@vent.dog", email="breakglass@vent.dog", password="s3cret!")
        with override_settings(BREAK_GLASS_LOGIN_HOSTS=["testserver"], ALLOWED_HOSTS=["testserver"]):
            self.client.post(LOGIN, {"username": "breakglass@vent.dog", "password": "s3cret!", "next": "/admin/"})
            self.assertIn("_auth_user_id", self.client.session)

    def test_empty_setting_means_form_everywhere(self):
        # Dev/mc/test default: no gate configured -> behave like stock Django admin.
        resp = self.client.get(LOGIN)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="password"')
