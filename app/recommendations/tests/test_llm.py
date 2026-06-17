"""JSON parsing tests.

WHY: models return JSON wrapped in prose or ```json fences unpredictably. A brittle parser drops
otherwise-valid recommendations, so the fallbacks (fence, substring) must hold.
"""
from django.test import SimpleTestCase

from recommendations.llm import LLMClient, parse_json_response


class ParseJsonTests(SimpleTestCase):
    def test_direct_json(self):
        self.assertEqual(parse_json_response('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        text = "Here you go:\n```json\n{\"a\": 1}\n```\nthanks"
        self.assertEqual(parse_json_response(text), {"a": 1})

    def test_substring_fallback(self):
        text = "blah blah {\"a\": [1, 2]} trailing"
        self.assertEqual(parse_json_response(text), {"a": [1, 2]})

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_json_response("no json here"))
        self.assertIsNone(parse_json_response(""))
        self.assertIsNone(parse_json_response(None))


class ModelAliasTests(SimpleTestCase):
    def test_unknown_alias_falls_back_to_default(self):
        c = LLMClient(model="does-not-exist")
        self.assertEqual(c.model_alias, LLMClient.DEFAULT_MODEL)
        self.assertTrue(c.model_id.startswith("anthropic/"))
