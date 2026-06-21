"""Session-authenticated app surfaces for clips — thin adapters over `services` (the same layer
the JSON API calls). Browser/session-authed (OIDC), so no bearer token: the upload page drives a
presigned direct-to-R2 PUT from JS, and search is a plain server-rendered GET.
"""
import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import services
from .models import Asset

logger = logging.getLogger(__name__)


@login_required
def library(request):
    """The signed-in user's own clips, newest first (owner-scoped even for superusers)."""
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


def asset_status(request, asset_id):
    """Tiny JSON poll target for the detail page so it can wait in the background instead of
    meta-refreshing the whole page (which restarts a playing clip). Same visibility rules as the
    detail view: public+ready ⇒ anyone; otherwise owner/superuser."""
    asset = services.get_public_asset(asset_id)
    if asset is None and request.user.is_authenticated:
        asset = services.get_asset_for(request.user, asset_id)
    if asset is None:
        raise Http404("Clip not found.")
    return JsonResponse({
        "status": asset.status,
        "caption_burning": bool(asset.caption_burning),
        "ready": asset.status == Asset.Status.READY and not asset.caption_burning,
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


def clip_download_gif(request, asset_id):
    """Force-download the GIF rendition (Content-Disposition: attachment) — for saving the actual .gif
    to share where only a real GIF autoplays (Discord/Signal). Same visibility as clip_download."""
    asset = services.get_public_asset(asset_id)
    if asset is None and request.user.is_authenticated:
        asset = services.get_asset_for(request.user, asset_id)
    if asset is None:
        raise Http404("Clip not found.")
    url = services.gif_download_url(asset)
    if not url:
        raise Http404("No GIF rendition.")
    return redirect(url)


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
@ensure_csrf_cookie  # so clips/record.js can read the csrftoken cookie for its POSTs
def record_page(request):
    """In-browser tab recorder: share a tab (getDisplayMedia) → record the moment → upload the
    captured webm through the same presign/finalize path as a file upload. No backend ingest
    changes — MediaRecorder emits video/webm, which finalize routes to the transcode queue."""
    return render(request, "clips/record.html", {
        "active_page": "clips_record",
        "meta_title": "GIF Maker — Make a GIF From Any Video · clip.cool",
        "meta_description": "Make a GIF from any video in your browser: share a tab, record the "
        "moment, then crop, trim, and caption it into a fast looping GIF. No plugin, no download.",
    })


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
    """JSON: {key, title?, content_type?, tags?, from_recorder?} → the created asset (async)."""
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
            trim_start=data.get("trim_start"),
            trim_end=data.get("trim_end"),
            # The in-browser recorder flags its clips so they join the template library.
            from_recorder=bool(data.get("from_recorder")),
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(services.serialize(asset))


def search_page(request):
    """Public server-rendered search: GET ?q= → Typesense → hydrated results. Logged out ⇒ public
    clips only; a signed-in user also sees their own. This is the root / front door — its title +
    description are the homepage's primary SEO copy. With a query, the title reflects it (e.g.
    "cat GIFs") for a more relevant tab/snippet (the canonical still collapses to root)."""
    q = (request.GET.get("q") or "").strip()
    results = [services.serialize(a) for a in services.search_assets(request.user, q)] if q else []
    if q:
        meta_title = "%s GIFs · clip.cool" % q
        meta_description = ("Search GIFs and memes for “%s” on clip.cool — fast looping clips, or "
                            "make your own from any video." % q)
    else:
        meta_title = "GIF Search & Maker — Make GIFs From Any Video · clip.cool"
        meta_description = ("Search thousands of GIFs and memes, or make your own from any video — "
                            "clip a moment, crop, caption, and share a fast looping GIF. Free on clip.cool.")
    return render(request, "clips/search.html", {
        "active_page": "clips", "q": q, "results": results,
        "meta_title": meta_title, "meta_description": meta_description,
    })


def browse_page(request):
    """Public no-query discovery grid: a random sample of the public catalog."""
    clips = [services.serialize(a) for a in services.browse_assets()]
    return render(request, "clips/browse.html", {
        "active_page": "clips_browse", "clips": clips,
        "meta_title": "Browse Trending GIFs & Memes · clip.cool",
        "meta_description": "Browse the newest looping GIFs and memes on clip.cool — every clip is "
        "made from a video and served as fast autoplay video, never a clunky GIF file.",
    })


def about_page(request):
    """Public static page explaining what clip.cool is: a GIF repository where you turn videos
    from other sites into looping clips, no plugin or download."""
    return render(request, "clips/about.html", {
        "active_page": "clips_about",
        "meta_title": "About clip.cool — GIF Search & Maker",
        "meta_description": "clip.cool is a GIF search engine and maker: search GIFs and memes, or "
        "make your own — share a browser tab, clip a moment from any video, then crop, caption, and publish.",
    })


def template_gallery(request):
    """Public template library: recorded clips anyone can browse and remix into a new GIF. No login
    to look; remixing one (clips_remix) requires signing in."""
    clips = [services.serialize(a) for a in services.list_template_clips()]
    return render(request, "clips/templates.html", {
        "active_page": "clips_templates", "clips": clips,
        "meta_title": "GIF Templates to Remix · clip.cool",
        "meta_description": "Remix ready-made GIF templates into your own meme — re-trim, re-crop, "
        "and caption any clip into a new looping GIF on clip.cool.",
    })


def robots_txt(request):
    """robots.txt — allow the public surfaces, keep crawlers out of auth/admin/API/per-user actions,
    and advertise the sitemap. Paths are absolute so they're host-independent."""
    sitemap = settings.SITE_URL + reverse("clips_sitemap")
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /api/",
        "Disallow: /oidc/",
        "Disallow: /hijack/",
        "Disallow: /clips/",  # per-user library + upload/edit/caption/remix actions
        "Allow: /clips/browse/",
        "Allow: /clips/templates/",
        "",
        f"Sitemap: {sitemap}",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def sitemap_xml(request):
    """Hand-rolled XML sitemap (avoids pulling in django.contrib.sites): the static public pages plus
    every indexable public clip. URLs are pinned to SITE_URL so they match the canonical host."""
    base = settings.SITE_URL
    urls = [
        {"loc": base + reverse("clips_search"), "priority": "1.0"},
        {"loc": base + reverse("clips_browse"), "priority": "0.8"},
        {"loc": base + reverse("clips_templates"), "priority": "0.7"},
        {"loc": base + reverse("clips_about"), "priority": "0.5"},
    ]
    for asset_id, updated_at in services.public_clip_sitemap_entries():
        urls.append({
            "loc": base + reverse("clips_asset", args=[asset_id]),
            "lastmod": updated_at.date().isoformat(),
            "priority": "0.6",
        })
    return render(request, "clips/sitemap.xml", {"urls": urls}, content_type="application/xml")


@login_required
@ensure_csrf_cookie  # so clips/remix.js can read the csrftoken cookie for its POST
def remix_page(request, asset_id):
    """Remix editor: load a template clip, re-crop/re-trim it, then create a NEW clip the user owns.
    Same source eligibility as create_remix (public + ready + video) — 404 otherwise."""
    asset = services.get_public_asset(asset_id)
    if asset is None or asset.media_type != Asset.MediaType.VIDEO:
        raise Http404("Template not found.")
    sources = services.video_sources(asset)
    src_url = next((s["url"] for s in sources if s["kind"] == "h264"), "")
    if not src_url:
        raise Http404("Template not ready.")
    return render(request, "clips/remix.html", {
        "active_page": "clips_templates", "asset": asset, "a": services.serialize(asset),
        "src_url": src_url,
    })


@login_required
@require_POST
def remix_create(request, asset_id):
    """JSON: {crop?, trim_start?, trim_end?, title?, tags?} → clone the template into a new owned
    Asset (async transcode). Returns the created asset so remix.js can open its detail page."""
    data = _json(request)
    asset = services.create_remix(
        request.user, asset_id,
        crop=data.get("crop"),
        trim_start=data.get("trim_start"),
        trim_end=data.get("trim_end"),
        title=data.get("title", ""),
        tags=data.get("tags") or [],
    )
    if asset is None:
        raise Http404("Template not found.")
    return JsonResponse(services.serialize(asset))


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return {}
