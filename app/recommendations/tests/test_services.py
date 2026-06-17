"""Service-layer tests (LLM + fetch mocked — hermetic, no network).

WHY the behaviors matter:
- analyze must persist a reproducible (input → prompt → output) corpus row linked to the prompt
  version, because that link is the foundation of the ADR-0013 learning loop.
- a fetch/generation failure must be recorded as `failed` (not crash) so the API can report it.
- exactly one champion per task-type is the invariant the whole "active prompt" model rests on.
"""
import json

from asgiref.sync import async_to_sync
from django.test import TestCase
from unittest.mock import patch

from recommendations import services
from recommendations.enums import AnalysisStatus, Category, Effort, TaskType
from recommendations.fetcher import FetchError
from recommendations.models import Analysis, PageSnapshot, PromptVersion, Recommendation


def fake_page(**over):
    page = {
        "url": "https://example.com/p", "title": "Example", "meta": "desc",
        "text": "some page text", "content_hash": "x" * 64,
    }
    page.update(over)
    return page


class FakeClient:
    """Stands in for the OpenRouter client — yields the canned payload as one streamed run."""
    model_id = "anthropic/claude-sonnet-4.6"

    def __init__(self, payload_text):
        self._payload = payload_text

    async def stream(self, system_prompt, user_prompt, *, temperature=0.4, max_tokens=8000):
        yield {"type": "chunk", "content": self._payload[:5]}
        yield {
            "type": "done", "text": self._payload,
            "usage": {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
        }


GOOD_PAYLOAD = json.dumps({
    "recommendations": [
        {
            "category": "metadata", "action_type": "generate_metadata",
            "title": "Add a meta description", "why": "It is missing",
            "description": "Write a 150-char summary", "effort": "one_click",
            "priority_score": 90, "impact_label": "High", "effort_label": "Quick win",
        },
        {
            "category": "BOGUS", "action_type": "ALSO_BOGUS", "title": "Coerced one",
            "effort": "nonsense", "priority_score": "not-a-number",
        },
        {"title": ""},  # dropped: no title
    ]
})


async def _fake_fetch(url):
    return fake_page(url=url)


class AnalyzeUrlTests(TestCase):
    def _run(self, payload=GOOD_PAYLOAD, fetch=_fake_fetch):
        with patch("recommendations.services.fetcher.fetch_page", new=fetch), \
             patch("recommendations.services.llm.get_client", return_value=FakeClient(payload)):
            return async_to_sync(services.analyze_url)("https://example.com/p")

    def test_persists_analysis_recs_and_corpus_link(self):
        analysis_id = self._run()
        a = Analysis.objects.get(pk=analysis_id)
        self.assertEqual(a.status, AnalysisStatus.COMPLETE)
        # WHY: the prompt-version link is the corpus key for the learning loop (ADR 0013).
        self.assertIsNotNone(a.prompt_version_id)
        self.assertEqual(a.prompt_version.task_type, TaskType.URL_ANALYZE)
        self.assertEqual(a.model, "anthropic/claude-sonnet-4.6")  # actual model id recorded
        self.assertEqual(a.usage["output_tokens"], 22)            # usage captured
        # WHY: the fetched page is stored once in a PageSnapshot, linked from the run, so the
        # input context survives for the corpus without bloating the Analysis row.
        self.assertEqual(a.page.text, "some page text")
        self.assertEqual(a.page.content_hash, "x" * 64)
        # Two valid recs persisted (third dropped for empty title), ordered by position.
        recs = list(a.recommendations.order_by("position"))
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0].title, "Add a meta description")
        self.assertEqual(recs[0].effort, Effort.ONE_CLICK)

    def test_identical_content_reuses_one_snapshot(self):
        # WHY: PageSnapshot is deduped by content_hash — analyzing the same content twice stores
        # the page once and links both runs to it (ADR 0013 storage dedup; the win of the split).
        id1 = self._run()
        id2 = self._run()
        self.assertNotEqual(id1, id2)
        self.assertEqual(PageSnapshot.objects.count(), 1)
        self.assertEqual(
            Analysis.objects.get(pk=id1).page_id, Analysis.objects.get(pk=id2).page_id
        )

    def test_unknown_enums_coerced_not_crashed(self):
        # WHY: a model can emit unknown category/action/effort; we must store a safe default
        # rather than fail the whole run.
        analysis_id = self._run()
        coerced = Analysis.objects.get(pk=analysis_id).recommendations.get(title="Coerced one")
        self.assertIn(coerced.category, Category.values)
        self.assertEqual(coerced.effort, Effort.REVIEW)       # "nonsense" -> default
        self.assertEqual(coerced.priority_score, 0.0)         # "not-a-number" -> 0.0

    def test_fetch_failure_marks_failed(self):
        async def boom(url):
            raise FetchError("blocked")
        analysis_id = self._run(fetch=boom)
        a = Analysis.objects.get(pk=analysis_id)
        self.assertEqual(a.status, AnalysisStatus.FAILED)
        self.assertIn("blocked", a.error)
        self.assertEqual(a.recommendations.count(), 0)

    def test_unparseable_generation_completes_with_no_recs(self):
        # Malformed JSON is expected/transient — the run completes with zero recs, not a 500.
        analysis_id = self._run(payload="not json at all")
        a = Analysis.objects.get(pk=analysis_id)
        self.assertEqual(a.status, AnalysisStatus.COMPLETE)
        self.assertEqual(a.recommendations.count(), 0)


class PromptManagementTests(TestCase):
    def test_create_version_is_monotonic_per_task(self):
        # The migration already seeded v1 for url_analyze.
        v2 = services.create_prompt_version(system_prompt="p2")
        v3 = services.create_prompt_version(system_prompt="p3")
        self.assertEqual([v2.version, v3.version], [2, 3])

    def test_set_champion_keeps_exactly_one(self):
        v2 = services.create_prompt_version(system_prompt="p2", make_champion=True)
        champions = PromptVersion.objects.filter(
            task_type=TaskType.URL_ANALYZE, is_champion=True
        )
        self.assertEqual(champions.count(), 1)
        self.assertEqual(champions.first().pk, v2.pk)
        self.assertEqual(services.get_champion().pk, v2.pk)
