"""OpenRouter LLM client (ADR 0013).

Same general approach as the zrag reference (raw HTTP to OpenRouter's OpenAI-compatible
chat-completions endpoint, `OPENROUTER_API_KEY`, the `HTTP-Referer`/`X-Title` headers, an
`anthropic/claude-*` model-alias map, `provider` failover order, ephemeral prompt caching),
adapted to **async `httpx`** rather than zrag's sync `requests` — keygrip serves on ASGI
specifically so an open SSE stream doesn't pin a worker (ADR 0004).

The client is intentionally thin and provider-shaped (OpenAI chat-completions wire format, which
is what OpenRouter speaks); it is *not* the Anthropic SDK.
"""
import json
import logging
import re

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    """Raised on a non-recoverable generation failure (surfaced to the caller as a failed run)."""


class LLMClient:
    """Async OpenRouter client. One instance per request is fine (cheap)."""

    # Friendly alias -> OpenRouter model id. Mirrors zrag; default tracks the current Sonnet.
    MODELS = {
        "haiku-4.5": "anthropic/claude-haiku-4.5",
        "sonnet-4.5": "anthropic/claude-sonnet-4.5",
        "sonnet-4.6": "anthropic/claude-sonnet-4.6",
        "opus-4.6": "anthropic/claude-opus-4.6",
    }
    DEFAULT_MODEL = "sonnet-4.6"

    def __init__(self, model=None, *, api_key=None, timeout=180.0):
        self.api_key = api_key or getattr(settings, "OPENROUTER_API_KEY", "")
        alias = model or self.DEFAULT_MODEL
        self.model_alias = alias if alias in self.MODELS else self.DEFAULT_MODEL
        self.model_id = self.MODELS[self.model_alias]
        self.timeout = timeout

    def _headers(self):
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY is not configured.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (app identity), configurable per environment.
            "HTTP-Referer": getattr(settings, "OPENROUTER_REFERER", "https://app.vent.dog"),
            "X-Title": getattr(settings, "OPENROUTER_TITLE", "Keygrip Recommendations"),
        }

    def _payload(self, system_prompt, user_prompt, *, temperature, max_tokens, stream):
        return {
            "model": self.model_id,
            "messages": [
                # Stable instruction prefix marked cacheable (ephemeral): big savings when the
                # same prompt version analyzes many URLs. Anthropic prompt caching via OpenRouter.
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
            # Prefer direct Anthropic, fall back to Bedrock — same order zrag settled on.
            "provider": {"order": ["anthropic", "amazon-bedrock"]},
        }

    async def stream(self, system_prompt, user_prompt, *, temperature=0.4, max_tokens=8000):
        """Async generator over Server-Sent chunks. Yields:
          {"type": "chunk", "content": "..."}  per token fragment
          {"type": "done", "text": "...", "usage": {...}}  once, at the end
        Raises LLMError on transport/HTTP failure (the service turns that into a failed run).
        """
        payload = self._payload(
            system_prompt, user_prompt,
            temperature=temperature, max_tokens=max_tokens, stream=True,
        )
        full = []
        usage = {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST", OPENROUTER_URL, headers=self._headers(), json=payload
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")[:500]
                        self._log_http_error(resp.status_code, body)
                        raise LLMError(f"OpenRouter returned {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if choices:
                            piece = choices[0].get("delta", {}).get("content") or ""
                            if piece:
                                full.append(piece)
                                yield {"type": "chunk", "content": piece}
                        if chunk.get("usage"):
                            usage = self._normalize_usage(chunk["usage"])
        except httpx.HTTPError as e:
            # Transport/timeout: transient and caller-recoverable -> warning, not error.
            logger.warning("OpenRouter stream transport error: %s", e)
            raise LLMError(str(e)) from e
        yield {"type": "done", "text": "".join(full), "usage": usage}

    async def complete(self, system_prompt, user_prompt, *, temperature=0.4, max_tokens=8000):
        """Non-streaming twin of `stream` (symmetric kwargs — CLAUDE.md streaming rule).
        Returns {"text": "...", "usage": {...}}."""
        payload = self._payload(
            system_prompt, user_prompt,
            temperature=temperature, max_tokens=max_tokens, stream=False,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(OPENROUTER_URL, headers=self._headers(), json=payload)
        except httpx.HTTPError as e:
            logger.warning("OpenRouter request transport error: %s", e)
            raise LLMError(str(e)) from e
        if resp.status_code != 200:
            self._log_http_error(resp.status_code, resp.text[:500])
            raise LLMError(f"OpenRouter returned {resp.status_code}")
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return {"text": text, "usage": self._normalize_usage(data.get("usage") or {})}

    @staticmethod
    def _log_http_error(status, body):
        # 4xx is caller-recoverable (bad model, oversized payload) -> warning; 5xx is upstream.
        log = logger.warning if 400 <= status < 500 else logger.error
        log("OpenRouter API error %s: %s", status, body)

    @staticmethod
    def _normalize_usage(usage):
        if not usage:
            return {}
        details = usage.get("prompt_tokens_details", {}) or {}
        return {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "cache_read_input_tokens": (
                usage.get("cache_read_input_tokens") or details.get("cached_tokens", 0) or 0
            ),
        }


def get_client(model=None):
    """Factory seam — tests patch `recommendations.services.llm.get_client` to inject a fake."""
    return LLMClient(model=model)


def parse_json_response(text):
    """Extract a JSON object from a model response (handles ```json fences and prose wrap).

    Tries: direct parse -> fenced code block -> first {...} substring. Returns None on failure
    (the caller logs at warning — a malformed generation is expected/transient, not a code bug).
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            pass
    logger.warning("Failed to parse JSON from LLM response: %.200s", text)
    return None
