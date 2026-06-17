"""Recommendations JSON API (ADR 0011 + ADR 0013) — a thin adapter over `services`.

Tier 1: `POST /analyze` analyzes any URL on demand against the champion prompt and returns the
prioritized recommendations (blocking JSON — the stable machine/partner/MCP contract; the
playground uses the SSE streaming view for the live feel). Auth is the API-wide KeycloakAuth.
"""
from asgiref.sync import sync_to_async
from ninja import Router
from ninja.errors import HttpError

from . import services
from .enums import TaskType
from .models import Analysis, PromptVersion, Recommendation
from .schemas import (
    AnalysisIn,
    AnalysisOut,
    InteractionIn,
    OkOut,
    PromptVersionIn,
    PromptVersionOut,
)

router = Router(tags=["recommendations"])


@router.post("/recommendations/analyze", response=AnalysisOut,
             summary="Analyze a URL and return recommendations")
async def analyze(request, payload: AnalysisIn):
    """Tier-1 stateless analysis of a single URL. Returns the stored run; `status` is `complete`
    on success or `failed` (with `error`) if the fetch or generation failed."""
    analysis_id = await services.analyze_url(
        payload.url, actor=request.auth, prompt_version=payload.prompt_version_id,
    )
    # The caller just created this run, so echo it back unscoped (get_analysis_for's owner
    # scoping is for the cross-request retrieval endpoint, and would wrongly 500 a service
    # account whose runs have no requested_by). None ⇒ no champion prompt configured.
    analysis = await sync_to_async(
        lambda: Analysis.objects.select_related("page").filter(pk=analysis_id).first()
    )()
    if analysis is None:
        raise HttpError(503, "No active prompt is configured for URL analysis.")
    return await sync_to_async(services.serialize_analysis)(analysis)


@router.get("/recommendations/analyses/{analysis_id}", response=AnalysisOut,
            summary="Fetch a stored analysis")
def get_analysis(request, analysis_id: int):
    analysis = services.get_analysis_for(request.auth, analysis_id)
    if analysis is None:
        raise HttpError(404, "Analysis not found.")
    return services.serialize_analysis(analysis)


@router.post("/recommendations/recommendations/{rec_id}/interactions", response=OkOut,
             summary="Log an accept/apply/dismiss signal")
def log_interaction(request, rec_id: int, payload: InteractionIn):
    rec = Recommendation.objects.filter(pk=rec_id).first()
    if rec is None:
        raise HttpError(404, "Recommendation not found.")
    try:
        interaction = services.record_interaction(
            rec, payload.kind, actor=request.auth, metadata=payload.metadata,
        )
    except ValueError as e:
        raise HttpError(422, str(e))
    return {"ok": True, "id": interaction.id}


# --- Prompt administration (superuser-only — versioned prompts, ADR 0013 §2) ---

def _require_superuser(request):
    if not getattr(request.auth, "is_superuser", False):
        raise HttpError(403, "Superuser only.")


@router.get("/recommendations/prompts", response=list[PromptVersionOut],
            summary="List prompt versions for a task-type")
def list_prompts(request, task_type: str = TaskType.URL_ANALYZE):
    _require_superuser(request)
    return services.list_versions(task_type)


@router.get("/recommendations/prompts/champion", response=PromptVersionOut,
            summary="The active (champion) prompt for a task-type")
def champion_prompt(request, task_type: str = TaskType.URL_ANALYZE):
    _require_superuser(request)
    pv = services.get_champion(task_type)
    if pv is None:
        raise HttpError(404, "No champion prompt for that task-type.")
    return pv


@router.post("/recommendations/prompts", response=PromptVersionOut,
             summary="Create a new prompt version")
def create_prompt(request, payload: PromptVersionIn):
    _require_superuser(request)
    return services.create_prompt_version(
        task_type=payload.task_type, system_prompt=payload.system_prompt, model=payload.model,
        temperature=payload.temperature, label=payload.label, notes=payload.notes,
        created_by=request.auth, make_champion=payload.make_champion,
    )


@router.post("/recommendations/prompts/{prompt_id}/promote", response=PromptVersionOut,
             summary="Promote a prompt version to champion")
def promote_prompt(request, prompt_id: int):
    _require_superuser(request)
    pv = PromptVersion.objects.filter(pk=prompt_id).first()
    if pv is None:
        raise HttpError(404, "Prompt version not found.")
    return services.set_champion(pv)
