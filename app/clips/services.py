"""clips service layer — all media business logic. Views, tasks, and the JSON API are thin
adapters over this (CLAUDE.md: logic never lives in views).

Ingest flow:
  presign  → browser PUTs straight to R2
  finalize → create Asset(pending), enqueue process_asset
  process  → (worker) download once → dimensions + sha256 + WebP poster + Tesseract OCR
             → mark ready → upsert into Typesense
  search   → query Typesense → hydrate the Postgres rows in relevance order
"""
import hashlib
import io
import logging
import mimetypes
import os
import re
import tempfile
import uuid

import json

from django.conf import settings
from django.utils import timezone

from . import search, storage
from .models import Asset, Rendition, Template

logger = logging.getLogger(__name__)

# Static images stay images; GIFs + real video transcode to looping video (docs/architecture.md).
ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
    "video/mp4", "video/webm", "video/quicktime",
}
# Content types that become a video Asset (transcoded). GIF is an image type but we never serve it
# as a GIF — it transcodes like any video ("never serve a GIF as a GIF").
VIDEO_CONTENT_TYPES = {"image/gif", "video/mp4", "video/webm", "video/quicktime"}


def _media_type(content_type):
    return Asset.MediaType.VIDEO if content_type in VIDEO_CONTENT_TYPES else Asset.MediaType.IMAGE


def _ext_for(filename, content_type):
    """File extension for the stored object — from the upload's name, else the content type.
    We do NOT keep the original filename in the key (privacy + collisions); just the type."""
    tail = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in tail:
        ext = "." + tail.rsplit(".", 1)[-1].lower()
        if ext.isascii() and len(ext) <= 6 and ext[1:].isalnum():
            return ext
    return mimetypes.guess_extension(content_type or "") or ""


def create_presigned_upload(user, filename, content_type):
    """Issue a presigned PUT so the browser uploads straight to R2. Returns the object `key` the
    caller echoes back to finalize_asset, plus the URL/method/headers to use. The key is a random
    id + extension — the original filename is never used as the object name."""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unsupported content type: {content_type!r}")
    key = f"originals/{uuid.uuid4().hex}{_ext_for(filename, content_type)}"
    return {
        "key": key,
        "url": storage.presign_put(key, content_type),
        "method": "PUT",
        "headers": {"Content-Type": content_type},
    }


def _clean_crop(crop):
    """Normalize a tab-recorder crop to {x,y,w,h} fractions in [0,1] (w,h > 0, within bounds), or
    None. Defensive: a bad/partial crop is dropped rather than raising — worst case is no crop."""
    if not isinstance(crop, dict):
        return None
    try:
        x, y, w, h = (float(crop["x"]), float(crop["y"]), float(crop["w"]), float(crop["h"]))
    except (KeyError, TypeError, ValueError):
        return None
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 0.0), 1.0 - x)
    h = min(max(h, 0.0), 1.0 - y)
    if w < 0.02 or h < 0.02:   # a sliver isn't a real selection
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def _clean_trim(trim_start, trim_end):
    """Normalize a scrubber trim to (start, end) seconds, or (None, None). Defensive: drop anything
    non-numeric/degenerate (end must be at least 0.1s past start) rather than raising."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    start, end = num(trim_start), num(trim_end)
    if start is not None and start < 0:
        start = 0.0
    if end is not None and end <= 0:
        end = None
    if start is not None and end is not None and end - start < 0.1:
        return None, None   # degenerate selection — treat as no trim
    # A bare start of 0 with no end is just "the whole clip".
    if (start in (None, 0.0)) and end is None:
        return None, None
    return start, end


def finalize_asset(user, *, key, title="", content_type="", tags=None, crop=None,
                   trim_start=None, trim_end=None):
    """Record the uploaded object as an Asset(pending) and enqueue async processing."""
    if not key:
        raise ValueError("key is required")
    media_type = _media_type(content_type)
    is_video = media_type == Asset.MediaType.VIDEO
    # Crop + trim only apply to video (baked in by the transcode ffmpeg pass).
    clean_crop = _clean_crop(crop) if is_video else None
    t_start, t_end = _clean_trim(trim_start, trim_end) if is_video else (None, None)
    asset = Asset.objects.create(
        owner=user,
        original_key=key,
        mime=content_type or "",
        media_type=media_type,
        title=(title or "").strip(),   # blank unless the user named it — never the filename
        tags=list(tags or []),
        crop=clean_crop,
        trim_start=t_start,
        trim_end=t_end,
        status=Asset.Status.PENDING,
    )
    # Deferred import: tasks.py pulls in procrastinate; keep it off the import path of callers.
    # Video → transcode tier (ffmpeg); image → the lightweight poster/OCR path.
    if media_type == Asset.MediaType.VIDEO:
        from .tasks import transcode_asset
        transcode_asset.defer(asset_id=str(asset.id))
    else:
        from .tasks import process_asset
        process_asset.defer(asset_id=str(asset.id))
    logger.info("clips: finalized %s asset %s (owner=%s)", media_type, asset.id, getattr(user, "pk", None))
    return asset


def process_asset(asset_id):
    """Heavy worker step: one download → dimensions + sha256 + poster + OCR → ready → index."""
    asset = Asset.objects.filter(pk=asset_id).first()
    if asset is None:
        logger.warning("clips.process_asset: asset %s is gone", asset_id)
        return
    try:
        data = storage.download_bytes(asset.original_key)
        asset.bytes = len(data)
        asset.sha256 = hashlib.sha256(data).hexdigest()
        _derive_image(asset, data)
        asset.status = Asset.Status.READY
        asset.save()
        search.upsert(asset)
        logger.info("clips: processed asset %s (ocr=%d chars)", asset.id, len(asset.ocr_text))
        # Hand off the (optional) vision auto-describe so a slow/failed LLM call never blocks the
        # asset becoming usable. Skip enqueuing entirely when no key is configured.
        if getattr(settings, "OPENROUTER_API_KEY", ""):
            from .tasks import autodescribe_asset
            autodescribe_asset.defer(asset_id=str(asset.id))
    except Exception:
        logger.error("clips.process_asset failed for %s", asset_id, exc_info=True)
        Asset.objects.filter(pk=asset_id).update(status=Asset.Status.FAILED)


def transcode_asset(asset_id):
    """Heavy worker step (transcode queue): download original → ffmpeg AV1/VP9/H.264 + poster →
    upload to R2 + Rendition rows → ready → index + autodescribe. Best-effort per rendition."""
    asset = Asset.objects.filter(pk=asset_id).first()
    if asset is None:
        return
    from . import transcode as tc

    # Stamp updated_at as we start so the reaper can tell a long-dead encode (status stuck
    # TRANSCODING, timestamp old) from one that's actively running.
    Asset.objects.filter(pk=asset_id).update(status=Asset.Status.TRANSCODING, updated_at=timezone.now())
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            data = storage.download_bytes(asset.original_key)
            with open(src, "wb") as f:
                f.write(data)
            asset.bytes = len(data)
            asset.sha256 = hashlib.sha256(data).hexdigest()
            trim = None
            if asset.trim_start is not None or asset.trim_end is not None:
                start = asset.trim_start or 0.0
                dur = (asset.trim_end - start) if asset.trim_end is not None else None
                trim = (start, dur)
            result = tc.transcode(src, td, crop=asset.crop, trim=trim)
            for r in result["renditions"]:
                rdata = open(r["path"], "rb").read()
                rkey = "renditions/%s/%s" % (asset.id, os.path.basename(r["path"]))
                storage.upload_bytes(rkey, rdata, r["mime"])
                Rendition.objects.update_or_create(
                    asset=asset, kind=r["kind"],
                    defaults={"r2_key": rkey, "mime": r["mime"], "bytes": len(rdata)},
                )
            if result["poster"]:
                pdata = open(result["poster"], "rb").read()
                pkey = "posters/%s.webp" % asset.id
                storage.upload_bytes(pkey, pdata, "image/webp")
                asset.poster_key = pkey
            if result.get("gif"):
                gdata = open(result["gif"], "rb").read()
                gkey = "renditions/%s/preview.gif" % asset.id
                storage.upload_bytes(gkey, gdata, "image/gif")
                Rendition.objects.update_or_create(
                    asset=asset, kind=Rendition.Kind.GIF,
                    defaults={"r2_key": gkey, "mime": "image/gif", "bytes": len(gdata)},
                )
            meta = result["meta"]
            asset.width = meta.get("width") or asset.width
            asset.height = meta.get("height") or asset.height
            asset.duration = meta.get("duration")
            asset.has_audio = bool(meta.get("has_audio"))
        asset.status = Asset.Status.READY
        asset.transcode_attempts = 0   # clear the reaper counter on a clean encode
        asset.save()
        search.upsert(asset)
        if getattr(settings, "OPENROUTER_API_KEY", ""):
            from .tasks import autodescribe_asset
            autodescribe_asset.defer(asset_id=str(asset.id))
        logger.info("clips: transcoded %s (%d renditions)", asset.id, len(result["renditions"]))
    except Exception:
        logger.error("clips.transcode_asset failed for %s", asset_id, exc_info=True)
        Asset.objects.filter(pk=asset_id).update(status=Asset.Status.FAILED)


def rendition_url(asset, kind):
    """Public URL of a single rendition kind (e.g. 'gif', 'h264'), or '' if absent."""
    r = asset.renditions.filter(kind=kind).first()
    return storage.public_url(r.r2_key) if r else ""


def download_url(asset):
    """Presigned GET that force-downloads the clip, named for it — so 'save' works cross-origin (e.g.
    to re-send via Signal). Serves the CAPTIONED rendition (text burned in) when one exists, else the
    original."""
    import os

    cap = asset.renditions.filter(kind=Rendition.Kind.CAPTIONED).first()
    key = cap.r2_key if cap else asset.original_key
    ext = os.path.splitext(key or "")[1]
    base = (asset.title or "").strip() or str(asset.id)
    safe = "".join(c if (c.isalnum() or c in " -_") else "" for c in base).strip() or str(asset.id)
    return storage.presign_get(key, filename="%s%s" % (safe, ext))


def gif_download_url(asset):
    """Presigned GET that force-downloads the GIF rendition (named for the clip), or '' if none."""
    r = asset.renditions.filter(kind=Rendition.Kind.GIF).first()
    if not r:
        return ""
    base = (asset.title or "").strip() or str(asset.id)
    safe = "".join(c if (c.isalnum() or c in " -_") else "" for c in base).strip() or str(asset.id)
    return storage.presign_get(r.r2_key, filename="%s.gif" % safe)


def _store_gif(asset, gif_path):
    """Upload a GIF file as the asset's GIF rendition (overwriting the previous one)."""
    data = open(gif_path, "rb").read()
    key = "renditions/%s/preview.gif" % asset.id
    storage.upload_bytes(key, data, "image/gif")
    Rendition.objects.update_or_create(
        asset=asset, kind=Rendition.Kind.GIF,
        defaults={"r2_key": key, "mime": "image/gif", "bytes": len(data)},
    )


def burn_caption_asset(asset_id):
    """Reconcile the caption-baked renditions with the asset's current caption, then clear the
    caption_burning progress flag. With a caption: bake it into the pixels for the downloadable file
    (CAPTIONED) and re-burn the shareable GIF so it carries the text too (text doesn't ride the
    player off-platform). Without one: drop the stale CAPTIONED and restore the plain GIF. Source =
    the H.264 rendition for video, the original for an image."""
    asset = Asset.objects.filter(pk=asset_id).first()
    if asset is None:
        return
    from . import transcode as tc

    is_video = asset.media_type == Asset.MediaType.VIDEO
    try:
        h264 = asset.renditions.filter(kind=Rendition.Kind.H264).first() if is_video else None
        src_key = (h264.r2_key if h264 else asset.original_key)
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            with open(src, "wb") as f:
                f.write(storage.download_bytes(src_key))

            if asset.text_layer_key:
                ov = os.path.join(td, "overlay.png")
                with open(ov, "wb") as f:
                    f.write(storage.download_bytes(asset.text_layer_key))
                out = tc.burn_caption(src, ov, td, video=is_video)
                ext, mime = ("mp4", "video/mp4") if is_video else ("png", "image/png")
                key = "renditions/%s/captioned.%s" % (asset.id, ext)
                storage.upload_bytes(key, open(out, "rb").read(), mime)
                cap_bytes = os.path.getsize(out)
                Rendition.objects.update_or_create(
                    asset=asset, kind=Rendition.Kind.CAPTIONED,
                    defaults={"r2_key": key, "mime": mime, "bytes": cap_bytes},
                )
                # Re-burn the GIF from the captioned video so the GIF link carries the caption.
                if is_video:
                    _store_gif(asset, tc.make_gif(out, os.path.join(td, "preview.gif")))
            else:
                # Caption cleared: remove the baked-in file and restore the plain (uncaptioned) GIF.
                cap = asset.renditions.filter(kind=Rendition.Kind.CAPTIONED).first()
                if cap:
                    try:
                        storage.delete(cap.r2_key)
                    except Exception:
                        logger.warning("burn_caption_asset: stale captioned delete failed", exc_info=True)
                    cap.delete()
                if is_video:
                    _store_gif(asset, tc.make_gif(src, os.path.join(td, "preview.gif")))
    except Exception:
        logger.error("clips.burn_caption_asset failed for %s", asset_id, exc_info=True)
    finally:
        Asset.objects.filter(pk=asset_id).update(caption_burning=False)


def video_sources(asset):
    """Ordered <video> sources (AV1 → VP9 → H.264) for a video asset. Queries renditions, so call
    only on the detail page, not for every search/library card."""
    order = {Rendition.Kind.AV1: 0, Rendition.Kind.VP9: 1, Rendition.Kind.H264: 2}
    rends = [r for r in asset.renditions.all() if r.kind in order]
    rends.sort(key=lambda r: order[r.kind])
    return [{"kind": r.kind, "url": storage.public_url(r.r2_key), "mime": r.mime} for r in rends]


def autodescribe_asset(asset_id, *, force_title=False):
    """Vision auto-labeling via OpenRouter (clips.llm): fills title (only if unset, unless
    force_title) + description, and merges extra tags. Best-effort — any failure leaves the
    asset's OCR/tags untouched. `force_title` is set by an explicit user "regenerate"."""
    asset = Asset.objects.filter(pk=asset_id).first()
    if asset is None:
        return
    from . import llm  # lazy: keeps httpx off the module import path

    key = asset.poster_key or asset.original_key
    ctype = "image/webp" if asset.poster_key else (asset.mime or "image/png")
    try:
        data = storage.download_bytes(key)
        meta = llm.describe_image(data, ctype, ocr_text=asset.ocr_text or "")
    except (storage.StorageNotConfigured, llm.LLMError):
        logger.warning("clips.autodescribe: skipped for %s", asset_id, exc_info=True)
        return
    except Exception:
        logger.warning("clips.autodescribe: unexpected error for %s", asset_id, exc_info=True)
        return
    changed = False
    fields = ["updated_at"]
    if meta["title"] and (force_title or not asset.title):   # don't clobber a human title at ingest
        asset.title = meta["title"]
        changed = True
        fields.append("title")
    if meta["description"]:
        asset.description = meta["description"]
        changed = True
        fields.append("description")
    # The vision model reads stylized meme text far better than Tesseract — prefer its verbatim
    # caption as the indexed text (Tesseract was only the rough hint we fed it).
    if meta.get("caption") and meta["caption"] != asset.ocr_text:
        asset.ocr_text = meta["caption"]
        changed = True
        fields.append("ocr_text")
    if meta["tags"]:
        existing = {t.lower() for t in (asset.tags or [])}
        merged = list(asset.tags or [])
        for t in meta["tags"]:
            if t.lower() not in existing:
                merged.append(t)
                existing.add(t.lower())
        if merged != (asset.tags or []):
            asset.tags = merged
            changed = True
            fields.append("tags")
    if changed:
        asset.save(update_fields=fields)
        search.upsert(asset)
        logger.info("clips: autodescribed %s (tags=%d)", asset.id, len(asset.tags or []))


def _derive_image(asset, data):
    """Pillow dimensions + WebP poster + Tesseract OCR. Lazy imports keep Pillow/pytesseract off
    the web import path (they live in the worker image)."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        logger.warning("clips: %s is not a decodable image", asset.original_key, exc_info=True)
        return
    asset.width, asset.height = im.size
    if not asset.mime:
        asset.mime = Image.MIME.get(im.format or "", "")
    asset.poster_key = _make_poster(asset, im)
    asset.ocr_text = _ocr(im)


def _make_poster(asset, im, size=(640, 640)):
    try:
        poster = im.convert("RGB")
        poster.thumbnail(size)
        buf = io.BytesIO()
        poster.save(buf, "WEBP", quality=80)
        key = f"posters/{asset.id}.webp"
        storage.upload_bytes(key, buf.getvalue(), "image/webp")
        return key
    except Exception:
        logger.warning("clips: poster generation failed for %s", asset.id, exc_info=True)
        return ""


def _ocr(im):
    """Burned-in text via Tesseract (needs the `tesseract-ocr` system pkg — see Dockerfile).

    Animated GIFs caption on *some* frame, not necessarily the first — so for multi-frame images we
    sample evenly across the animation, OCR each composited frame, and union the distinct results.
    OCR'ing only frame 0 silently misses captions that appear later (the Isengard-gif bug)."""
    import pytesseract
    from PIL import ImageSequence

    def _read(frame):
        try:
            return " ".join(pytesseract.image_to_string(frame).split())
        except Exception:
            logger.warning("clips: OCR failed on a frame", exc_info=True)
            return ""

    try:
        n_frames = int(getattr(im, "n_frames", 1) or 1)
    except Exception:
        n_frames = 1
    if n_frames <= 1:
        return _read(im)

    # Sample up to MAX frames evenly (Iterator yields properly composited frames, unlike raw seek).
    MAX = 8
    stride = max(1, n_frames // MAX)
    raw = []
    for i, frame in enumerate(ImageSequence.Iterator(im)):
        if i % stride:
            continue
        t = _read(frame.convert("RGB"))
        if t:
            raw.append(t)
    return "  ".join(_dedup_captions(raw))


def _dedup_captions(texts, threshold=0.5):
    """Collapse near-duplicate OCR reads — the same caption read off different frames comes back
    with minor noise ('WHERE IS' vs 'WHEREIS' vs 'WHERE Li)'). Group by token-set overlap and keep
    the longest (usually cleanest) variant of each distinct caption; genuinely different captions
    (multi-panel) stay separate."""
    kept = []  # [(tokenset, text)]
    for t in texts:
        toks = set(re.findall(r"[a-z0-9']+", t.lower()))
        if not toks:
            continue
        for i, (ks, kt) in enumerate(kept):
            if len(toks & ks) / (len(toks | ks) or 1) >= threshold:
                if len(t) > len(kt):
                    kept[i] = (toks, t)
                break
        else:
            kept.append((toks, t))
    return [t for _, t in kept]


def index_asset(asset_id):
    """Re-index one asset (also the building block for a full rebuild)."""
    asset = Asset.objects.filter(pk=asset_id).first()
    if asset is not None:
        search.upsert(asset)


def reindex_all():
    """Rebuild the whole Typesense index from Postgres (the source of truth)."""
    count = 0
    for asset in Asset.objects.filter(status=Asset.Status.READY).iterator():
        search.upsert(asset)
        count += 1
    return count


def search_assets(user, q, *, limit=40):
    """Search by title/description/OCR text/tags. Logged-out ⇒ public catalog only; a user sees
    public + their own; superuser sees everything. Returns Asset rows in Typesense relevance order."""
    if user is not None and getattr(user, "is_authenticated", False):
        owner_id = None if user.is_superuser else user.pk
        ids = search.query(q, owner_id=owner_id, limit=limit)
    else:
        ids = search.query(q, public_only=True, limit=limit)
    by_id = {str(a.id): a for a in Asset.objects.filter(pk__in=ids)}
    return [by_id[i] for i in ids if i in by_id]  # preserve relevance order


def browse_assets(limit=30):
    """A random sample of the public catalog for the no-query browse grid. order_by('?') is fine at
    this scale; revisit if the catalog grows large."""
    return list(
        Asset.objects.filter(is_public=True, status=Asset.Status.READY).order_by("?")[:limit]
    )


def list_assets(user, *, limit=40):
    """The user's OWN clips, newest first — this backs "My clips" / the API's "List your assets", so
    it is owner-scoped even for superusers (a superuser still reaches any individual clip via
    get_asset_for, and sees everything via Browse / the Django admin)."""
    return list(Asset.objects.filter(owner=user)[:limit])


def get_public_asset(asset_id):
    """A ready, public asset for the no-login share page. None if private/missing/not ready."""
    return Asset.objects.filter(pk=asset_id, is_public=True, status=Asset.Status.READY).first()


def get_asset_for(user, asset_id):
    """One asset the user may see/edit (owner, or any for a superuser). None if not found/allowed."""
    qs = Asset.objects.all() if getattr(user, "is_superuser", False) else Asset.objects.filter(owner=user)
    return qs.filter(pk=asset_id).first()


def delete_asset(user, asset_id):
    """Permanently delete a clip the user owns (superuser: any): every R2 object (original, poster,
    text layer, all renditions), the Typesense doc, then the DB rows. R2/index deletes are
    best-effort — never block the DB delete on a storage hiccup. Returns True, or None if not
    found/allowed."""
    asset = get_asset_for(user, asset_id)
    if asset is None:
        return None
    keys = [asset.original_key, asset.poster_key, asset.text_layer_key]
    keys += list(asset.renditions.values_list("r2_key", flat=True))
    for key in filter(None, keys):
        try:
            storage.delete(key)
        except Exception:
            logger.warning("delete_asset: R2 delete failed for %s", key, exc_info=True)
    try:
        search.remove(asset.id)
    except Exception:
        logger.warning("delete_asset: search remove failed for %s", asset.id, exc_info=True)
    asset.delete()  # cascades renditions
    return True


def update_asset(user, asset_id, *, title=None, description=None, tags=None, is_public=None):
    """Apply a user's manual edits and re-index. Returns the asset, or None if not found/allowed.
    Fields left None are unchanged; auto-describe only runs at ingest, so edits aren't clobbered."""
    asset = get_asset_for(user, asset_id)
    if asset is None:
        return None
    fields = ["updated_at"]
    if is_public is not None:
        asset.is_public = bool(is_public)
        fields.append("is_public")
    if title is not None:
        asset.title = title.strip()[:255]
        fields.append("title")
    if description is not None:
        asset.description = description.strip()[:2000]
        fields.append("description")
    if tags is not None:
        cleaned, seen = [], set()
        for t in tags:
            t = t.strip()[:40]
            if t and t.lower() not in seen:
                seen.add(t.lower())
                cleaned.append(t)
        asset.tags = cleaned[:30]
        fields.append("tags")
    asset.save(update_fields=fields)
    search.upsert(asset)
    return asset


def list_templates(*, limit=300):
    return list(Template.objects.all()[:limit])


def get_template(template_id):
    return Template.objects.filter(pk=template_id).first()


def template_image_bytes(template):
    """Raw template image bytes (served same-origin by the builder so the canvas isn't tainted)."""
    return storage.download_bytes(template.image_key)


def save_caption(user, asset_id, *, text_key, layers):
    """Store the caption overlay (editable layers + rendered transparent PNG) and re-index the
    typed text. Returns the asset, or None if not found/allowed. The player overlays text_layer_key
    over the clip; layers are the re-openable source of truth (docs/phase2-video-captioning.md)."""
    asset = get_asset_for(user, asset_id)
    if asset is None:
        return None
    layers = layers or []
    asset.text_layer_key = (text_key or "").strip()
    asset.caption_layers = layers
    # We know the exact typed caption — index it (better than OCR'ing the rendered overlay).
    caption = " ".join(
        str(l.get("text", "")).strip() for l in layers if str(l.get("text", "")).strip()
    )
    fields = ["text_layer_key", "caption_layers", "updated_at"]
    if caption:
        asset.ocr_text = caption
        fields.append("ocr_text")
        if not asset.title:
            asset.title = caption[:255]
            fields.append("title")
    asset.save(update_fields=fields)
    search.upsert(asset)
    # Reconcile the baked renditions (download + GIF) with the new caption off the heavy queue. The
    # flag drives the detail-page "baking…" progress; burn_caption_asset clears it when done. This
    # runs whether a caption was added, edited, or cleared (the worker handles each case).
    if asset.media_type == Asset.MediaType.VIDEO:
        Asset.objects.filter(pk=asset.id).update(caption_burning=True)
        asset.caption_burning = True
    from .tasks import burn_caption_asset
    burn_caption_asset.defer(asset_id=str(asset.id))
    return asset


def reap_stuck_assets(heartbeat_grace=60, max_attempts=3):
    """Recover jobs orphaned by a dead worker — e.g. a deploy recreated worker-transcode and a
    `doing` job was left with no worker, so the asset sits in TRANSCODING / caption_burning forever.

    Detection is HEARTBEAT-based, not time-based: Procrastinate workers heartbeat every ~1-2s, so a
    `doing` job is only "orphaned" if its worker's heartbeat is gone/older than heartbeat_grace. That
    means a long-but-LIVE encode (a big AV1 can run minutes) is never falsely reaped. A genuinely
    un-encodable clip never reaches here either — transcode_asset catches ffmpeg errors and marks the
    asset FAILED (job succeeds) — and transcode retries are bounded (→ FAILED after max_attempts) so a
    pathological input can't loop. Returns the count recovered."""
    from django.db import connection
    from .tasks import transcode_asset, burn_caption_asset, process_asset
    with connection.cursor() as c:
        c.execute(
            "SELECT j.id, j.task_name, j.args FROM procrastinate_jobs j "
            "LEFT JOIN procrastinate_workers w ON w.id = j.worker_id "
            "WHERE j.status = 'doing' AND (j.worker_id IS NULL OR w.id IS NULL "
            "  OR w.last_heartbeat < now() - make_interval(secs => %s))",
            [heartbeat_grace],
        )
        stalled = c.fetchall()
    n = 0
    for job_id, task_name, args in stalled:
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = {}
        aid = (args or {}).get("asset_id")
        # Clear the orphaned job (its worker is dead) so it doesn't linger as a zombie `doing`.
        with connection.cursor() as c:
            c.execute("UPDATE procrastinate_jobs SET status='failed' WHERE id=%s AND status='doing'", [job_id])
        if not aid:
            continue
        asset = Asset.objects.filter(pk=aid).first()
        if asset is None:
            continue
        short = (task_name or "").rsplit(".", 1)[-1]
        if short == "transcode_asset":
            if asset.transcode_attempts >= max_attempts:
                Asset.objects.filter(pk=aid).update(status=Asset.Status.FAILED)
                logger.error("reap: %s stuck after %d attempts → FAILED", aid, asset.transcode_attempts)
                continue
            Asset.objects.filter(pk=aid).update(
                status=Asset.Status.PENDING, transcode_attempts=asset.transcode_attempts + 1, updated_at=timezone.now())
            transcode_asset.defer(asset_id=str(aid))
        elif short == "burn_caption_asset":
            burn_caption_asset.defer(asset_id=str(aid))
        elif short == "process_asset":
            Asset.objects.filter(pk=aid).update(status=Asset.Status.PENDING, updated_at=timezone.now())
            process_asset.defer(asset_id=str(aid))
        else:
            continue
        logger.warning("reap: recovered orphaned %s for asset %s", short, aid)
        n += 1
    if n:
        logger.info("reap: recovered %d orphaned job(s)", n)
    return n


def regenerate_asset(user, asset_id):
    """Re-run vision auto-describe on demand (owner/superuser). Overwrites title + description and
    re-merges tags. Returns the asset, or None if not found/allowed."""
    asset = get_asset_for(user, asset_id)
    if asset is None:
        return None
    from .tasks import autodescribe_asset
    autodescribe_asset.defer(asset_id=str(asset.id), force_title=True)
    return asset


def serialize(asset):
    """Asset → dict for the JSON API and the templates."""
    return {
        "id": str(asset.id),
        "title": asset.title or "",
        "description": asset.description or "",
        "status": asset.status,
        "media_type": asset.media_type,
        "mime": asset.mime or "",
        "width": asset.width,
        "height": asset.height,
        "tags": list(asset.tags or []),
        "is_public": asset.is_public,
        "url": storage.public_url(asset.original_key),
        # Poster: the generated WebP when present. Before it exists, fall back to the original ONLY for
        # images (a renderable thumb); for a video the original is a .webm that can't be an <img>, so
        # return "" and let the grid/detail show a "transcoding…" placeholder instead of a broken img.
        "poster_url": storage.public_url(
            asset.poster_key or (asset.original_key if asset.media_type == Asset.MediaType.IMAGE else "")
        ) if (asset.poster_key or asset.media_type == Asset.MediaType.IMAGE) else "",
        "text_layer_url": storage.public_url(asset.text_layer_key) if asset.text_layer_key else "",
        "created_at": asset.created_at,
    }
