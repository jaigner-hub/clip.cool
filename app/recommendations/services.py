"""Service layer for recommendations (ADR 0011: logic lives here; API/views/SSE are thin
adapters). ADR 0013: URL-first Tier-1 analysis over a *versioned* prompt (the champion), with the
input→output run persisted as the unlabeled corpus and accept/apply logged as the leading signal.

Async boundary: the network work (URL fetch, LLM streaming) is genuinely async so an open SSE
stream doesn't pin a worker (ADR 0004). The ORM work is plain sync functions wrapped in
`sync_to_async` — this keeps async-ORM edge cases out and the SQLite test path simple.
"""
import logging
import time

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from . import fetcher, llm
from .enums import ActionType, AnalysisStatus, Category, Effort, InteractionKind, TaskType
from .fetcher import FetchError
from .llm import LLMError
from .models import Analysis, PageSnapshot, PromptVersion, Recommendation, RecommendationInteraction

logger = logging.getLogger(__name__)

MAX_RECOMMENDATIONS = 25


class PromptUnavailable(Exception):
    """No active (champion) prompt exists for the task-type — analysis can't proceed."""


# --------------------------------------------------------------------------- prompt management

def get_champion(task_type=TaskType.URL_ANALYZE):
    """The active prompt for a task-type (ADR 0013 §2), or None."""
    return PromptVersion.objects.champion_for(task_type)


def list_versions(task_type=TaskType.URL_ANALYZE):
    return list(PromptVersion.objects.filter(task_type=task_type))


@transaction.atomic
def create_prompt_version(
    *, task_type=TaskType.URL_ANALYZE, system_prompt, model="sonnet-4.6", temperature=0.4,
    label="", notes="", created_by=None, make_champion=False,
):
    """Create the next version for a task-type. `version` is monotonic per task-type."""
    last = (
        PromptVersion.objects.filter(task_type=task_type)
        .order_by("-version").values_list("version", flat=True).first()
    )
    pv = PromptVersion.objects.create(
        task_type=task_type, version=(last or 0) + 1, system_prompt=system_prompt,
        model=model, temperature=temperature, label=label, notes=notes, created_by=created_by,
    )
    if make_champion:
        set_champion(pv)
    return pv


@transaction.atomic
def set_champion(prompt_version):
    """Promote `prompt_version` to champion, demoting any current one. The single-champion
    invariant is enforced here (transactional) so it holds on SQLite too, not only via the
    Postgres partial-unique index."""
    PromptVersion.objects.filter(
        task_type=prompt_version.task_type, is_champion=True,
    ).exclude(pk=prompt_version.pk).update(is_champion=False)
    if not prompt_version.is_champion:
        prompt_version.is_champion = True
        prompt_version.save(update_fields=["is_champion"])
    return prompt_version


# --------------------------------------------------------------------------- interactions

def record_interaction(recommendation, kind, *, actor=None, session_key="", metadata=None):
    """Log a leading accept/apply/dismiss signal (ADR 0013 §5)."""
    if kind not in InteractionKind.values:
        raise ValueError(f"Unknown interaction kind: {kind}")
    return RecommendationInteraction.objects.create(
        recommendation=recommendation, kind=kind,
        actor=_user_or_none(actor),
        session_key=session_key or "", metadata=metadata or {},
    )


def _user_or_none(actor):
    """A Django user FK accepts only a real user — a ServiceAccountPrincipal is `is_authenticated`
    but is not a User row, so it (and AnonymousUser) resolve to None."""
    if actor is None or getattr(actor, "is_service_account", False):
        return None
    return actor if getattr(actor, "is_authenticated", False) else None


# --------------------------------------------------------------------------- analysis (async)

async def analyze_url_stream(
    url, *, actor=None, organization=None, project=None, session_key="", prompt_version=None,
):
    """Analyze a single URL, streaming progress. Async generator yielding events:
      {"type": "page",  "title", "url"}            once the page is fetched
      {"type": "chunk", "content"}                 per model token fragment
      {"type": "done",  "analysis_id", "recommendations", "usage", "duration_ms"}
      {"type": "error", "error"}                   on any failure (run persisted as failed)
    """
    start = time.monotonic()
    try:
        setup = await sync_to_async(_begin_analysis)(
            url, actor, organization, project, session_key, prompt_version,
        )
    except PromptUnavailable as e:
        yield {"type": "error", "error": str(e)}
        return

    analysis_id = setup["analysis_id"]
    try:
        await sync_to_async(_set_status)(analysis_id, AnalysisStatus.FETCHING)
        page = await fetcher.fetch_page(url)
        await sync_to_async(_save_page)(analysis_id, page)
        yield {"type": "page", "title": page["title"], "url": page["url"]}

        await sync_to_async(_set_status)(analysis_id, AnalysisStatus.GENERATING)
        client = llm.get_client(model=setup["model"])
        user_prompt = build_user_prompt(page)
        text, usage = "", {}
        async for ev in client.stream(
            setup["system_prompt"], user_prompt, temperature=setup["temperature"],
        ):
            if ev["type"] == "chunk":
                yield {"type": "chunk", "content": ev["content"]}
            elif ev["type"] == "done":
                text, usage = ev["text"], ev["usage"]

        recs = normalize_recommendations(llm.parse_json_response(text))
        duration_ms = int((time.monotonic() - start) * 1000)
        out = await sync_to_async(_finalize_analysis)(
            analysis_id, recs, usage, duration_ms, client.model_id,
        )
        yield {
            "type": "done", "analysis_id": analysis_id,
            "recommendations": out, "usage": usage, "duration_ms": duration_ms,
        }
    except (FetchError, LLMError) as e:
        await sync_to_async(_fail_analysis)(analysis_id, str(e))
        yield {"type": "error", "error": str(e), "analysis_id": analysis_id}


async def analyze_url(
    url, *, actor=None, organization=None, project=None, session_key="", prompt_version=None,
):
    """Non-streaming twin of `analyze_url_stream` (symmetric kwargs — CLAUDE.md streaming rule).
    Drains the stream and returns the persisted Analysis id (status conveys success/failure)."""
    analysis_id = None
    async for ev in analyze_url_stream(
        url, actor=actor, organization=organization, project=project,
        session_key=session_key, prompt_version=prompt_version,
    ):
        if ev["type"] in ("done", "error"):
            analysis_id = ev.get("analysis_id", analysis_id)
    return analysis_id


# --------------------------------------------------------------------------- sync ORM helpers

def _resolve_prompt(prompt_version):
    if isinstance(prompt_version, PromptVersion):
        return prompt_version
    if isinstance(prompt_version, int):
        pv = PromptVersion.objects.filter(pk=prompt_version).first()
        if pv is None:
            raise PromptUnavailable(f"Prompt version {prompt_version} not found.")
        return pv
    pv = get_champion(TaskType.URL_ANALYZE)
    if pv is None:
        raise PromptUnavailable("No active prompt is configured for URL analysis.")
    return pv


def _begin_analysis(url, actor, organization, project, session_key, prompt_version):
    pv = _resolve_prompt(prompt_version)
    analysis = Analysis.objects.create(
        url=url,
        organization=organization,
        project=project,
        requested_by=_user_or_none(actor),
        session_key=session_key or "",
        status=AnalysisStatus.PENDING,
        prompt_version=pv,
    )
    return {
        "analysis_id": analysis.id,
        "system_prompt": pv.system_prompt,
        "model": pv.model,
        "temperature": pv.temperature,
    }


def _set_status(analysis_id, status):
    Analysis.objects.filter(pk=analysis_id).update(status=status)


def _save_page(analysis_id, page):
    # One PageSnapshot per distinct content (sha256) — identical content is stored once and reused
    # across runs (storage dedup; ADR 0013). The fetched URL is the snapshot's, post-redirect.
    snapshot, _ = PageSnapshot.objects.get_or_create(
        content_hash=page["content_hash"],
        defaults={
            "url": page["url"], "title": page["title"],
            "meta": page["meta"], "text": page["text"],
        },
    )
    Analysis.objects.filter(pk=analysis_id).update(page=snapshot)


def _finalize_analysis(analysis_id, recs, usage, duration_ms, model_id):
    with transaction.atomic():
        Recommendation.objects.bulk_create([
            Recommendation(analysis_id=analysis_id, position=i, **rec)
            for i, rec in enumerate(recs)
        ])
        Analysis.objects.filter(pk=analysis_id).update(
            status=AnalysisStatus.COMPLETE, usage=usage, duration_ms=duration_ms,
            model=model_id, completed_at=timezone.now(),
        )
    return [
        serialize_recommendation(r)
        for r in Recommendation.objects.filter(analysis_id=analysis_id).order_by("position")
    ]


def _fail_analysis(analysis_id, error):
    Analysis.objects.filter(pk=analysis_id).update(
        status=AnalysisStatus.FAILED, error=error, completed_at=timezone.now(),
    )


def serialize_analysis(analysis):
    """Serialize an Analysis (+ its recommendations) to the API/SSE shape."""
    return {
        "id": analysis.id,
        "url": analysis.url,
        "status": analysis.status,
        "model": analysis.model,
        "fetched_title": analysis.page.title if analysis.page_id else "",
        "usage": analysis.usage,
        "error": analysis.error,
        "duration_ms": analysis.duration_ms,
        "created_at": analysis.created_at,
        "recommendations": [
            serialize_recommendation(r) for r in analysis.recommendations.order_by("position")
        ],
    }


def get_analysis_for(actor, analysis_id):
    """Fetch an analysis the actor may see — superuser-first; otherwise only the caller's own
    runs (corpus rows aren't public). Returns the Analysis or None."""
    qs = Analysis.objects.select_related("page")
    if not getattr(actor, "is_superuser", False):
        user = _user_or_none(actor)
        if user is None:
            return None
        qs = qs.filter(requested_by=user)
    return qs.filter(pk=analysis_id).first()


def serialize_recommendation(rec):
    return {
        "id": rec.id,
        "category": rec.category,
        "action_type": rec.action_type,
        "title": rec.title,
        "why": rec.why,
        "description": rec.description,
        "effort": rec.effort,
        "priority_score": rec.priority_score,
        "impact_label": rec.impact_label,
        "effort_label": rec.effort_label,
        "position": rec.position,
    }


# --------------------------------------------------------------------------- prompt construction

def build_user_prompt(page):
    """The per-request (dynamic) message: the page under analysis. The stable instruction prefix
    is the champion's system_prompt (cached separately by the LLM client)."""
    return (
        f"URL: {page['url']}\n"
        f"TITLE: {page['title'] or '(none)'}\n"
        f"META DESCRIPTION: {page['meta'] or '(none)'}\n\n"
        f"PAGE TEXT (truncated):\n{page['text']}"
    )


def normalize_recommendations(parsed):
    """Coerce the model's JSON into safe, storable Recommendation kwargs.

    Defensive on purpose: a generation can return unknown enum values, missing fields, or junk.
    Unknown category/action_type/effort fall back to safe defaults rather than failing the run;
    items without a title are dropped; the list is capped.
    """
    if not parsed:
        return []
    items = parsed.get("recommendations") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:MAX_RECOMMENDATIONS]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "category": _coerce(item.get("category"), Category, Category.EXISTING_CONTENT),
            "action_type": _coerce(item.get("action_type"), ActionType, ActionType.FIX_TECHNICAL),
            "title": title[:500],
            "why": str(item.get("why") or "").strip(),
            "description": str(item.get("description") or "").strip(),
            "effort": _coerce(item.get("effort"), Effort, Effort.REVIEW),
            "priority_score": _to_float(item.get("priority_score")),
            "impact_label": str(item.get("impact_label") or "").strip()[:100],
            "effort_label": str(item.get("effort_label") or "").strip()[:100],
            "action_payload": item.get("action_payload") if isinstance(item.get("action_payload"), dict) else {},
        })
    return out


def _coerce(value, choices, default):
    return value if value in choices.values else default


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
