"""OpenRouter vision client — auto-describe uploaded clips (docs/architecture.md differentiator).

A thin, SYNC OpenAI-compatible chat-completions call (the wire format OpenRouter speaks), called
from the Procrastinate `autodescribe_asset` task, so no async needed. It sends the image (the R2
poster, as a base64 data URL) to a Claude vision model and asks for title/description/tags JSON.
This is NOT the Anthropic SDK. An empty OPENROUTER_API_KEY ⇒ LLMError (the asset just keeps its
OCR/tags). Pattern mirrors the former recommendations client (ADR 0013).
"""
import base64
import json
import logging
import re

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM = (
    "You label meme and GIF images for a searchable library. Look at the image and respond with "
    "STRICT JSON only, exactly these keys: "
    '{"title": "<short human label, <= 6 words>", '
    '"description": "<one or two sentences: subjects, setting, expression, and the meme '
    'format/sentiment if recognizable>", '
    '"tags": ["<5-12 lowercase search keywords: subjects, emotions, format, franchise>"]}. '
    "Do not transcribe burned-in text verbatim (it is indexed separately). Output ONLY the JSON "
    "object, no prose, no code fences."
)


class LLMError(Exception):
    """Non-recoverable describe failure (caller logs at warning; the asset is left as-is)."""


def describe_image(image_bytes, content_type="image/webp", *, model=None, timeout=60.0):
    """Return {'title','description','tags'} for the image, or raise LLMError."""
    api_key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not api_key:
        raise LLMError("OPENROUTER_API_KEY is not configured.")
    model_id = model or getattr(settings, "CLIPS_VISION_MODEL", "anthropic/claude-sonnet-4.6")
    data_url = "data:%s;base64,%s" % (
        content_type or "image/webp", base64.b64encode(image_bytes).decode("ascii")
    )
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "Label this image as instructed. Return only the JSON."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        "max_tokens": 700,
        "temperature": 0.3,
        # Prefer direct Anthropic, fall back to Bedrock — same order the old client used.
        "provider": {"order": ["anthropic", "amazon-bedrock"]},
    }
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        "HTTP-Referer": getattr(settings, "OPENROUTER_REFERER", "https://app.vent.dog"),
        "X-Title": getattr(settings, "OPENROUTER_TITLE", "clip.cool"),
    }
    try:
        resp = httpx.post(OPENROUTER_URL, headers=headers, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        logger.warning("OpenRouter vision transport error: %s", e)
        raise LLMError(str(e)) from e
    if resp.status_code != 200:
        log = logger.warning if 400 <= resp.status_code < 500 else logger.error
        log("OpenRouter vision error %s: %.300s", resp.status_code, resp.text)
        raise LLMError("OpenRouter returned %s" % resp.status_code)
    text = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
    data = _parse_json(text)
    if not isinstance(data, dict):
        raise LLMError("Vision model returned no parseable JSON.")
    return {
        "title": str(data.get("title") or "").strip()[:255],
        "description": str(data.get("description") or "").strip()[:2000],
        "tags": _clean_tags(data.get("tags")),
    }


def _clean_tags(raw):
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for t in raw:
        t = str(t).strip().lower()[:40]
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= 20:
            break
    return out


def _parse_json(text):
    """Extract a JSON object: direct → ```json fence → first {...} substring (mirrors the old
    recommendations parser). Returns None on failure (caller treats as a transient miss)."""
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
    logger.warning("Failed to parse JSON from vision response: %.200s", text)
    return None
