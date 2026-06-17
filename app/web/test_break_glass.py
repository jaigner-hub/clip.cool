"""Break-glass login alerting (ADR 0002). WHY: the single local superuser is the deliberate,
audited escape hatch to "Keycloak sole auth". Every use MUST surface — a break-glass login that
fires no audit log is the failure mode this guards against. We assert that a local-password
(ModelBackend) login emits the WARNING marker the Loki ruler alerts on, and that a normal OIDC
login does NOT (so the alert isn't desensitised by routine SSO logins).
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in
from django.test import RequestFactory, TestCase

from web.signals import BREAK_GLASS_MARKER

User = get_user_model()


class BreakGlassAlertTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_superuser("breakglass@vent.dog", email="breakglass@vent.dog")

    def _fire(self, backend):
        """Simulate user_logged_in with the given backend recorded on the session."""
        request = self.factory.get("/admin/login/")
        request.session = {"_auth_user_backend": backend}
        request.META["HTTP_CF_CONNECTING_IP"] = "203.0.113.7"
        user_logged_in.send(sender=self.user.__class__, request=request, user=self.user)

    def test_local_password_login_emits_marker(self):
        with self.assertLogs("web.security", level="WARNING") as cm:
            self._fire("django.contrib.auth.backends.ModelBackend")
        line = "\n".join(cm.output)
        self.assertIn(BREAK_GLASS_MARKER, line)
        self.assertIn("breakglass@vent.dog", line)
        self.assertIn("203.0.113.7", line)  # client IP captured for forensics

    def test_oidc_login_is_silent(self):
        # No WARNING on web.security for a normal SSO login (assertNoLogs raises if any emitted).
        with self.assertNoLogs("web.security", level="WARNING"):
            self._fire("web.auth.KeygripOIDCBackend")
