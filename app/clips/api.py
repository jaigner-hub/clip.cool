"""clips JSON API (ADR 0011) — a thin adapter over `services`, Keycloak-bearer auth.

The browser UI uses the session views in clips/views.py (no bearer token); this router is the
programmatic/MCP contract. Assets are owned by a Django user, so every endpoint requires a user
access token (machine/client-credentials tokens are rejected for the media endpoints).
"""
from ninja import Router
from ninja.errors import HttpError

from . import services
from .schemas import AssetOut, FinalizeIn, PresignIn, PresignOut, SearchOut

router = Router(tags=["clips"])


def _require_user(request):
    """Resolve the bearer principal to a Django user; reject machine tokens (no owner row)."""
    user = request.auth
    if not getattr(user, "pk", None):
        raise HttpError(403, "A user access token is required for the media endpoints.")
    return user


@router.post("/clips/uploads/presign", response=PresignOut,
             summary="Presign a direct-to-R2 upload")
def presign(request, payload: PresignIn):
    user = _require_user(request)
    try:
        return services.create_presigned_upload(user, payload.filename, payload.content_type)
    except ValueError as e:
        raise HttpError(422, str(e))


@router.post("/clips/assets", response=AssetOut,
             summary="Finalize an uploaded object into an asset")
def create_asset(request, payload: FinalizeIn):
    user = _require_user(request)
    try:
        asset = services.finalize_asset(
            user, key=payload.key, title=payload.title,
            content_type=payload.content_type, tags=payload.tags,
        )
    except ValueError as e:
        raise HttpError(422, str(e))
    return services.serialize(asset)


@router.get("/clips/assets", response=list[AssetOut], summary="List your assets")
def list_assets(request, limit: int = 40):
    user = _require_user(request)
    return [services.serialize(a) for a in services.list_assets(user, limit=limit)]


@router.get("/clips/search", response=SearchOut,
            summary="Search assets by title, OCR text, and tags")
def search_assets(request, q: str = "", limit: int = 40):
    user = _require_user(request)
    results = services.search_assets(user, q, limit=limit)
    return {"q": q, "count": len(results), "results": [services.serialize(a) for a in results]}
