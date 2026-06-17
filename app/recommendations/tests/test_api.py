"""JSON API contract tests (ADR 0011/0013). Auth is patched at the same seam tenancy tests use
(`web.api_auth.decode_keycloak_token`) so no live Keycloak is needed.

WHY: the API is the flagship public surface — it must never be an open door (401 without a token),
must report a stored run's shape, and prompt administration must be superuser-only (prompts are
platform config, not tenant data).
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from unittest.mock import patch

from recommendations.tests.test_services import GOOD_PAYLOAD, FakeClient, _fake_fetch
from recommendations.models import Analysis, Recommendation
from tenancy.models import Organization, ServiceAccount

User = get_user_model()


class AnalyzeApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("u@acme.com", email="u@acme.com")
        cls.su = User.objects.create_user("su@acme.com", email="su@acme.com", is_superuser=True)

    def _as(self, email):
        return {"HTTP_AUTHORIZATION": "Bearer x"}, patch(
            "web.api_auth.decode_keycloak_token", return_value={"email": email}
        )

    def test_requires_auth(self):
        # WHY: no token -> 401. The API is never an open door.
        r = self.client.post(
            "/api/v1/recommendations/analyze",
            data=json.dumps({"url": "https://example.com"}), content_type="application/json",
        )
        self.assertEqual(r.status_code, 401)

    def test_analyze_returns_recommendations(self):
        headers, auth = self._as("u@acme.com")
        with auth, \
             patch("recommendations.services.fetcher.fetch_page", new=_fake_fetch), \
             patch("recommendations.services.llm.get_client", return_value=FakeClient(GOOD_PAYLOAD)):
            r = self.client.post(
                "/api/v1/recommendations/analyze",
                data=json.dumps({"url": "https://example.com/p"}),
                content_type="application/json", **headers,
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "complete")
        self.assertEqual(len(body["recommendations"]), 2)
        self.assertEqual(body["recommendations"][0]["title"], "Add a meta description")

    def test_analyze_works_for_service_account(self):
        # WHY: a machine (client-credentials) token has no Django user, so the just-created run
        # has requested_by=None. The analyze response must still echo it back (not 500).
        org = Organization.objects.create(name="Acme", slug="acme")
        ServiceAccount.objects.create(organization=org, client_id="svc-1", is_active=True)
        with patch("web.api_auth.decode_keycloak_token", return_value={"azp": "svc-1"}), \
             patch("recommendations.services.fetcher.fetch_page", new=_fake_fetch), \
             patch("recommendations.services.llm.get_client", return_value=FakeClient(GOOD_PAYLOAD)):
            r = self.client.post(
                "/api/v1/recommendations/analyze",
                data=json.dumps({"url": "https://example.com/p"}),
                content_type="application/json", HTTP_AUTHORIZATION="Bearer x",
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "complete")

    def test_get_analysis_is_owner_scoped(self):
        # An analysis requested by `user` is invisible to a different non-superuser.
        a = Analysis.objects.create(url="https://x.test", requested_by=self.user)
        headers, auth = self._as("su@acme.com")  # superuser sees any
        with auth:
            r = self.client.get(f"/api/v1/recommendations/analyses/{a.id}", **headers)
        self.assertEqual(r.status_code, 200)

        other = User.objects.create_user("other@acme.com", email="other@acme.com")
        headers, auth = self._as("other@acme.com")
        with auth:
            r = self.client.get(f"/api/v1/recommendations/analyses/{a.id}", **headers)
        self.assertEqual(r.status_code, 404)  # not theirs -> 404, not 403 (don't reveal existence)

    def test_log_interaction(self):
        a = Analysis.objects.create(url="https://x.test", requested_by=self.user)
        rec = Recommendation.objects.create(
            analysis=a, category="metadata", action_type="generate_metadata", title="t",
        )
        headers, auth = self._as("u@acme.com")
        with auth:
            r = self.client.post(
                f"/api/v1/recommendations/recommendations/{rec.id}/interactions",
                data=json.dumps({"kind": "accepted"}), content_type="application/json", **headers,
            )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(rec.interactions.get().kind, "accepted")

    def test_invalid_interaction_kind_rejected(self):
        a = Analysis.objects.create(url="https://x.test", requested_by=self.user)
        rec = Recommendation.objects.create(
            analysis=a, category="metadata", action_type="generate_metadata", title="t",
        )
        headers, auth = self._as("u@acme.com")
        with auth:
            r = self.client.post(
                f"/api/v1/recommendations/recommendations/{rec.id}/interactions",
                data=json.dumps({"kind": "exploded"}), content_type="application/json", **headers,
            )
        self.assertEqual(r.status_code, 422)


class PromptAdminApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("u@acme.com", email="u@acme.com")
        cls.su = User.objects.create_user("su@acme.com", email="su@acme.com", is_superuser=True)

    def _auth(self, email):
        return patch("web.api_auth.decode_keycloak_token", return_value={"email": email})

    def test_list_prompts_superuser_only(self):
        with self._auth("u@acme.com"):
            r = self.client.get("/api/v1/recommendations/prompts", HTTP_AUTHORIZATION="Bearer x")
        self.assertEqual(r.status_code, 403)

        with self._auth("su@acme.com"):
            r = self.client.get("/api/v1/recommendations/prompts", HTTP_AUTHORIZATION="Bearer x")
        self.assertEqual(r.status_code, 200)
        # The seeded champion is present.
        self.assertTrue(any(p["is_champion"] for p in r.json()))

    def test_create_and_promote(self):
        with self._auth("su@acme.com"):
            r = self.client.post(
                "/api/v1/recommendations/prompts",
                data=json.dumps({"system_prompt": "new prompt", "make_champion": True}),
                content_type="application/json", HTTP_AUTHORIZATION="Bearer x",
            )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["is_champion"])
        self.assertEqual(r.json()["version"], 2)  # seeded v1 + this
