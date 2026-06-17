"""ID-token validation must tolerate small Keycloak<->app clock skew.

WHY: Keycloak (vent.dog) and the app run on separate hosts, so a token's `iat` can land a
couple seconds in this box's future. mozilla-django-oidc passes no leeway to PyJWT (leeway=0),
so without OIDC_CLOCK_SKEW_LEEWAY a perfectly normal few-seconds skew raises
ImmatureSignatureError and login fails intermittently (the bug this guards against). Leeway must
absorb that — but a token dated far in the future must still be rejected, so the slack can't be
unbounded. This also pins the _verify_jws override against a future mozilla-django-oidc bump.
"""
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, override_settings

from web.auth import KeygripOIDCBackend


@override_settings(OIDC_RP_SIGN_ALGO="RS256", OIDC_CLOCK_SKEW_LEEWAY=30)
class OIDCClockSkewTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def _token(self, iat_offset):
        now = int(time.time())
        return jwt.encode(
            {"iat": now + iat_offset, "exp": now + 3600, "sub": "alice"},
            self.private_key,
            algorithm="RS256",
        )

    def test_iat_within_leeway_is_accepted(self):
        # Keycloak's clock 10s ahead of ours — within the 30s leeway, login should work.
        payload = KeygripOIDCBackend()._verify_jws(self._token(10), self.public_key)
        self.assertEqual(payload["sub"], "alice")

    def test_iat_far_in_future_is_still_rejected(self):
        # 5 minutes ahead is not clock skew; leeway must not wave it through.
        with self.assertRaises(jwt.ImmatureSignatureError):
            KeygripOIDCBackend()._verify_jws(self._token(300), self.public_key)
