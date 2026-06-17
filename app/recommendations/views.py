"""Session-authenticated app surfaces for the recommendations playground (ADR 0013).

These are thin adapters over `services` (same layer the JSON API calls). They exist separately
from the Keycloak-token JSON API because the playground is browser/session-authenticated and SSE
uses `EventSource` (a GET that can't carry an Authorization header) — matching how zrag streamed.
"""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import (
    HttpResponseForbidden,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import render
from django.views.decorators.http import require_POST

from . import services
from .enums import TaskType
from .llm import LLMClient
from .models import Recommendation

logger = logging.getLogger(__name__)


@login_required
def playground(request):
    """The 'paste a URL → see recommendations' play page. Prompt editing is superuser-only."""
    champion = services.get_champion(TaskType.URL_ANALYZE)
    return render(request, "recommendations/playground.html", {
        "active_page": "recommendations",
        "champion": champion,
        "models": list(LLMClient.MODELS.keys()),
    })


def _sse(event):
    return f"data: {json.dumps(event)}\n\n"


@login_required
async def playground_stream(request):
    """SSE endpoint driving `services.analyze_url_stream`. GET ?url=&prompt_version_id=."""
    url = (request.GET.get("url") or "").strip()
    if not url:
        return JsonResponse({"error": "url is required"}, status=400)
    pv_raw = request.GET.get("prompt_version_id")
    try:
        prompt_version = int(pv_raw) if pv_raw else None
    except ValueError:
        prompt_version = None
    user = await request.auser()
    session_key = request.session.session_key or ""

    async def events():
        try:
            async for ev in services.analyze_url_stream(
                url, actor=user, session_key=session_key, prompt_version=prompt_version,
            ):
                yield _sse(ev)
        except Exception:  # never leak a stack trace into the stream
            logger.error("Playground stream failed", exc_info=True)
            yield _sse({"type": "error", "error": "Internal error during analysis."})

    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # tell any proxy not to buffer the stream
    return resp


@login_required
@require_POST
def playground_save_prompt(request):
    """Create a new prompt version from the editor (superuser-only — prompts are platform config,
    ADR 0013 §2). Returns the new version so the page can use it for the next run."""
    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser only.")
    system_prompt = (request.POST.get("system_prompt") or "").strip()
    if not system_prompt:
        return JsonResponse({"error": "system_prompt is required"}, status=400)
    try:
        temperature = float(request.POST.get("temperature", "0.4"))
    except ValueError:
        temperature = 0.4
    pv = services.create_prompt_version(
        task_type=TaskType.URL_ANALYZE,
        system_prompt=system_prompt,
        model=request.POST.get("model", "sonnet-4.6"),
        temperature=temperature,
        label=request.POST.get("label", ""),
        created_by=request.user,
        make_champion=request.POST.get("make_champion") in ("1", "true", "on"),
    )
    return JsonResponse({
        "id": pv.id, "version": pv.version, "is_champion": pv.is_champion, "model": pv.model,
    })


@login_required
@require_POST
def playground_interaction(request, rec_id):
    """Log an accept/dismiss/apply signal from the playground (any logged-in user)."""
    rec = Recommendation.objects.filter(pk=rec_id).first()
    if rec is None:
        return JsonResponse({"error": "not found"}, status=404)
    try:
        services.record_interaction(
            rec, request.POST.get("kind", ""), actor=request.user,
            session_key=request.session.session_key or "",
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse({"ok": True})
