"""Session-authenticated app surfaces for clips — thin adapters over `services` (the same layer
the JSON API calls). Browser/session-authed (OIDC), so no bearer token: the upload page drives a
presigned direct-to-R2 PUT from JS, and search is a plain server-rendered GET.
"""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import services

logger = logging.getLogger(__name__)


@login_required
@ensure_csrf_cookie  # so clips/upload.js can read the csrftoken cookie for its POSTs
def upload_page(request):
    return render(request, "clips/upload.html", {"active_page": "clips_upload"})


@login_required
@require_POST
def presign(request):
    """JSON: {filename, content_type} → {key, url, method, headers} for a direct-to-R2 PUT."""
    data = _json(request)
    filename = (data.get("filename") or "").strip()
    content_type = (data.get("content_type") or "").strip()
    if not filename or not content_type:
        return HttpResponseBadRequest("filename and content_type are required")
    try:
        return JsonResponse(services.create_presigned_upload(request.user, filename, content_type))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=422)


@login_required
@require_POST
def finalize(request):
    """JSON: {key, title?, content_type?, tags?} → the created asset (processing is async)."""
    data = _json(request)
    key = (data.get("key") or "").strip()
    if not key:
        return HttpResponseBadRequest("key is required")
    try:
        asset = services.finalize_asset(
            request.user,
            key=key,
            title=data.get("title", ""),
            content_type=data.get("content_type", ""),
            tags=data.get("tags") or [],
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(services.serialize(asset))


@login_required
def search_page(request):
    """Server-rendered search: GET ?q= → Typesense → hydrated results (title, OCR text, tags)."""
    q = (request.GET.get("q") or "").strip()
    results = [services.serialize(a) for a in services.search_assets(request.user, q)] if q else []
    return render(request, "clips/search.html", {"active_page": "clips", "q": q, "results": results})


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return {}
