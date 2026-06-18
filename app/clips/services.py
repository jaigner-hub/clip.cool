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
import re
import uuid

from django.conf import settings

from . import search, storage
from .models import Asset, Template

logger = logging.getLogger(__name__)

# The image slice. Video (.mp4/.webm and GIF→video) arrives with the transcode tier later.
ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
}


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


def finalize_asset(user, *, key, title="", content_type="", tags=None):
    """Record the uploaded object as an Asset(pending) and enqueue async processing."""
    if not key:
        raise ValueError("key is required")
    asset = Asset.objects.create(
        owner=user,
        original_key=key,
        mime=content_type or "",
        title=(title or "").strip(),   # blank unless the user named it — never the filename
        tags=list(tags or []),
        status=Asset.Status.PENDING,
    )
    # Deferred import: tasks.py pulls in procrastinate; keep it off the import path of callers
    # (e.g. the web request) until actually needed.
    from .tasks import process_asset

    process_asset.defer(asset_id=str(asset.id))
    logger.info("clips: finalized asset %s (owner=%s)", asset.id, getattr(user, "pk", None))
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
    """Search a user's assets by title/OCR text/tags. Superuser sees everything (superuser-first,
    CLAUDE.md). Returns Asset rows in Typesense relevance order."""
    owner_id = None if getattr(user, "is_superuser", False) else user.pk
    ids = search.query(q, owner_id=owner_id, limit=limit)
    by_id = {str(a.id): a for a in Asset.objects.filter(pk__in=ids)}
    return [by_id[i] for i in ids if i in by_id]  # preserve relevance order


def list_assets(user, *, limit=40):
    qs = Asset.objects.all() if getattr(user, "is_superuser", False) else Asset.objects.filter(owner=user)
    return list(qs[:limit])


def get_asset_for(user, asset_id):
    """One asset the user may see/edit (owner, or any for a superuser). None if not found/allowed."""
    qs = Asset.objects.all() if getattr(user, "is_superuser", False) else Asset.objects.filter(owner=user)
    return qs.filter(pk=asset_id).first()


def update_asset(user, asset_id, *, title=None, description=None, tags=None):
    """Apply a user's manual edits and re-index. Returns the asset, or None if not found/allowed.
    Fields left None are unchanged; auto-describe only runs at ingest, so edits aren't clobbered."""
    asset = get_asset_for(user, asset_id)
    if asset is None:
        return None
    fields = ["updated_at"]
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
        "mime": asset.mime or "",
        "width": asset.width,
        "height": asset.height,
        "tags": list(asset.tags or []),
        "url": storage.public_url(asset.original_key),
        "poster_url": storage.public_url(asset.poster_key or asset.original_key),
        "created_at": asset.created_at,
    }
