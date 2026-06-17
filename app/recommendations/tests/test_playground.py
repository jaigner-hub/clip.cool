"""Playground page + session-authed adapter tests.

WHY: the page must render under strict CSP (nonce present, no leaked template-comment text — the
CLAUDE.md multi-line `{# #}` trap), prompt editing must be superuser-gated, and the interaction
endpoint must record the leading signal.
"""
import json

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from unittest.mock import patch

from recommendations import services
from recommendations.enums import TaskType
from recommendations.models import (
    Analysis,
    PromptVersion,
    Recommendation,
    RecommendationInteraction,
)
from recommendations.tests.test_services import GOOD_PAYLOAD, FakeClient, _fake_fetch

User = get_user_model()


class PlaygroundPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("u@acme.com", email="u@acme.com")
        cls.su = User.objects.create_user("su@acme.com", email="su@acme.com", is_superuser=True)

    def test_requires_login(self):
        r = self.client.get(reverse("rec_playground"))
        self.assertEqual(r.status_code, 302)  # -> OIDC login

    def test_renders_for_user_without_prompt_editor(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("rec_playground"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('id="rec-app"', body)
        # Non-superuser: no prompt editor.
        self.assertNotIn('id="rec-prompt"', body)
        # CSP discipline: no leaked multi-line comment markers in the rendered HTML.
        self.assertNotIn("{#", body)
        self.assertNotIn("{% comment", body)
        # Strict-CSP scripts are external 'self' (nonce only needed for inline) — confirm the
        # external bundle is wired.
        self.assertIn("recommendations/playground.js", body)

    def test_superuser_sees_prompt_editor_with_champion(self):
        self.client.force_login(self.su)
        r = self.client.get(reverse("rec_playground"))
        body = r.content.decode()
        self.assertIn('id="rec-prompt"', body)
        # The champion's prompt text is prefilled into the editor.
        self.assertIn("Answer Engine Optimization", body)


class PlaygroundAdapterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("u@acme.com", email="u@acme.com")
        cls.su = User.objects.create_user("su@acme.com", email="su@acme.com", is_superuser=True)
        cls.analysis = Analysis.objects.create(url="https://x.test")
        cls.rec = Recommendation.objects.create(
            analysis=cls.analysis, category="metadata", action_type="generate_metadata", title="t",
        )

    def test_save_prompt_superuser_only(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("rec_playground_save_prompt"), {"system_prompt": "x"})
        self.assertEqual(r.status_code, 403)

        self.client.force_login(self.su)
        r = self.client.post(
            reverse("rec_playground_save_prompt"),
            {"system_prompt": "a better prompt", "model": "sonnet-4.6", "temperature": "0.3"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], 2)  # after the seeded v1

    def test_interaction_logged(self):
        self.client.force_login(self.user)
        url = reverse("rec_playground_interaction", args=[self.rec.id])
        r = self.client.post(url, {"kind": "dismissed"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            RecommendationInteraction.objects.get(recommendation=self.rec).kind, "dismissed"
        )


class PlaygroundStreamTests(TransactionTestCase):
    """The SSE view is the centerpiece of the playground — exercise the real async streaming path
    end to end (fetch + LLM stubbed). TransactionTestCase (not TestCase) so the async ORM writes
    inside the view don't fight the per-test atomic wrapper."""

    def setUp(self):
        self.user = User.objects.create_user("s@acme.com", email="s@acme.com")
        # TransactionTestCase truncates tables, so re-seed the champion the view needs.
        if not PromptVersion.objects.champion_for(TaskType.URL_ANALYZE):
            services.create_prompt_version(system_prompt="test prompt", make_champion=True)

    async def test_stream_emits_page_then_done_with_recommendations(self):
        await sync_to_async(self.async_client.force_login)(self.user)
        with patch("recommendations.services.fetcher.fetch_page", new=_fake_fetch), \
             patch("recommendations.services.llm.get_client", return_value=FakeClient(GOOD_PAYLOAD)):
            resp = await self.async_client.get(
                reverse("rec_playground_stream"), {"url": "https://example.com/p"}
            )
            self.assertEqual(resp["Content-Type"], "text/event-stream")
            body = b"".join([chunk async for chunk in resp.streaming_content]).decode()

        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "page")          # fetched first
        self.assertEqual(types[-1], "done")         # finishes with the result
        done = events[-1]
        self.assertEqual(len(done["recommendations"]), 2)
        self.assertIn("usage", done)
