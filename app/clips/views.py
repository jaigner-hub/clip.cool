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
from .models import Asset

logger = logging.getLogger(__name__)


@login_required
def library(request):
    """All of the user's clips, newest first (superuser sees everyone's)."""
    assets = [services.serialize(a) for a in services.list_assets(request.user, limit=200)]
    return render(request, "clips/library.html", {"active_page": "clips_library", "assets": assets})


def asset_detail(request, asset_id):
    """Canonical clip page (clip.cool/<id>). Public+ready ⇒ anyone (logged out included) — carries
    OG/Twitter meta so the link unfurls (and autoplays) in chat/social; private/unready ⇒
    owner/superuser only. Owner-only controls render when can_edit."""
    asset = services.get_public_asset(asset_id)   # public + ready ⇒ anyone
    if asset is None and request.user.is_authenticated:
        asset = services.get_asset_for(request.user, asset_id)   # else owner/superuser (incl. private/unready)
    if asset is None:
        raise Http404("Clip not found.")
    u = request.user
    can_edit = u.is_authenticated and (u.is_superuser or asset.owner_id == u.id)
    a = services.serialize(asset)
    sources = services.video_sources(asset) if asset.media_type == Asset.MediaType.VIDEO else []
    mp4_url = next((s["url"] for s in sources if s["kind"] == "h264"), a.get("url"))
    return render(request, "clips/detail.html", {
        "active_page": "clips_library", "asset": asset, "a": a, "sources": sources,
        "can_edit": can_edit, "mp4_url": mp4_url, "page_url": request.build_absolute_uri(),
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
@require_POST
def asset_delete(request, asset_id):
    """Permanently delete a clip (owner/superuser) — R2 objects + index + DB. → My clips."""
    if services.delete_asset(request.user, asset_id) is None:
        raise Http404("Clip not found.")
    return redirect("clips_library")


def public_clip_mp4(request, asset_id):
    """Clean direct-video link → 302 to the H.264 rendition. Pasted in Discord/Slack/etc. it
    embeds as an autoplaying muted loop (GIF-style), unlike the HTML page (poster + play button)."""
    asset = services.get_public_asset(asset_id)
    if asset is None:
        raise Http404("Clip not found.")
    sources = services.video_sources(asset) if asset.media_type == Asset.MediaType.VIDEO else []
    url = next((s["url"] for s in sources if s["kind"] == "h264"), None)
    if not url:
        raise Http404("No video rendition.")
    return redirect(url)


def public_clip_gif(request, asset_id):
    """Clean link → 302 to the optimized GIF rendition. Pasted in Discord/Slack it autoplays +
    loops inline (the only format chat reliably auto-loops for arbitrary sites)."""
    asset = services.get_public_asset(asset_id)
    if asset is None:
        raise Http404("Clip not found.")
    url = services.rendition_url(asset, "gif")
    if not url:
        raise Http404("No GIF rendition.")
    return redirect(url)


def clip_download(request, asset_id):
    """Force-download the original file (Content-Disposition: attachment) — for saving the GIF to
    re-send via Signal etc. Public for public clips; owner/superuser for private/unready."""
    asset = services.get_public_asset(asset_id)
    if asset is None and request.user.is_authenticated:
        asset = services.get_asset_for(request.user, asset_id)
    if asset is None:
        raise Http404("Clip not found.")
    return redirect(services.download_url(asset))


@login_required
def caption_builder(request, asset_id):
    """Caption an existing clip (overlay mode): add text over the video/image; saves editable
    layers + a transparent text PNG the player overlays. Reopens prefilled for re-edit."""
    asset = services.get_asset_for(request.user, asset_id)
    if asset is None:
        raise Http404("Clip not found.")
    sources = services.video_sources(asset) if asset.media_type == Asset.MediaType.VIDEO else []
    return render(request, "clips/caption.html", {
        "active_page": "clips_library", "asset": asset, "a": services.serialize(asset),
        "sources": sources, "layers_json": json.dumps(asset.caption_layers or []),
    })


@login_required
@require_POST
def caption_save(request, asset_id):
    data = _json(request)
    asset = services.save_caption(
        request.user, asset_id,
        text_key=(data.get("text_key") or "").strip(),
        layers=data.get("layers") or [],
    )
    if asset is None:
        raise Http404("Clip not found.")
    return JsonResponse({"ok": True})


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
@ensure_csrf_cookie  # so clips/record.js can read the csrftoken cookie for its POSTs
def record_page(request):
    """In-browser tab recorder: share a tab (getDisplayMedia) → record the moment → upload the
    captured webm through the same presign/finalize path as a file upload. No backend ingest
    changes — MediaRecorder emits video/webm, which finalize routes to the transcode queue."""
    return render(request, "clips/record.html", {"active_page": "clips_record"})


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
            crop=data.get("crop"),
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(services.serialize(asset))


def search_page(request):
    """Public server-rendered search: GET ?q= → Typesense → hydrated results. Logged out ⇒ public
    clips only; a signed-in user also sees their own."""
    q = (request.GET.get("q") or "").strip()
    results = [services.serialize(a) for a in services.search_assets(request.user, q)] if q else []
    return render(request, "clips/search.html", {"active_page": "clips", "q": q, "results": results})


def browse_page(request):
    """Public no-query discovery grid: a random sample of the public catalog."""
    clips = [services.serialize(a) for a in services.browse_assets()]
    return render(request, "clips/browse.html", {"active_page": "clips_browse", "clips": clips})


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return {}
