"""Session-authenticated app surfaces for clips — thin adapters over `services` (the same layer
the JSON API calls). Browser/session-authed (OIDC), so no bearer token: the upload page drives a
presigned direct-to-R2 PUT from JS, and search is a plain server-rendered GET.
"""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import services

logger = logging.getLogger(__name__)


@login_required
def library(request):
    """All of the user's clips, newest first (superuser sees everyone's)."""
    assets = [services.serialize(a) for a in services.list_assets(request.user, limit=200)]
    return render(request, "clips/library.html", {"active_page": "clips_library", "assets": assets})


@login_required
def asset_detail(request, asset_id):
    """One clip + its (possibly still-generating) metadata. The template auto-refreshes while the
    status is pending, so this is where the upload flow lands to watch describe/OCR complete."""
    asset = services.get_asset_for(request.user, asset_id)
    if asset is None:
        raise Http404("Clip not found.")
    return render(request, "clips/detail.html", {
        "active_page": "clips_library", "asset": asset, "a": services.serialize(asset),
    })


@login_required
def asset_edit(request, asset_id):
    asset = services.get_asset_for(request.user, asset_id)
    if asset is None:
        raise Http404("Clip not found.")
    if request.method == "POST":
        tags = (request.POST.get("tags") or "").split(",")
        services.update_asset(
            request.user, asset_id,
            title=request.POST.get("title", ""),
            description=request.POST.get("description", ""),
            tags=tags,
            is_public="is_public" in request.POST,   # checkbox: present ⇒ public
        )
        return redirect("clips_asset", asset_id=asset_id)
    return render(request, "clips/edit.html", {
        "active_page": "clips_library", "asset": asset, "a": services.serialize(asset),
        "tags_str": ", ".join(asset.tags or []),
    })


@login_required
@require_POST
def asset_regenerate(request, asset_id):
    if services.regenerate_asset(request.user, asset_id) is None:
        raise Http404("Clip not found.")
    return redirect("clips_asset", asset_id=asset_id)


@login_required
def create_gallery(request):
    """Pick a template to caption (the in-app meme builder)."""
    return render(request, "clips/create.html", {
        "active_page": "clips_create", "templates": services.list_templates(),
    })


@login_required
def builder(request, template_id):
    template = services.get_template(template_id)
    if template is None:
        raise Http404("Template not found.")
    return render(request, "clips/builder.html", {"active_page": "clips_create", "template": template})


@login_required
def template_image(request, template_id):
    """Same-origin proxy of a template's image so the builder canvas can export without tainting."""
    template = services.get_template(template_id)
    if template is None:
        raise Http404("Template not found.")
    try:
        data = services.template_image_bytes(template)
    except Exception:
        raise Http404("Template image unavailable.")
    resp = HttpResponse(data, content_type=template.mime or "image/png")
    resp["Cache-Control"] = "public, max-age=86400"
    return resp


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
