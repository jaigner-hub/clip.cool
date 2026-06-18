"""Typesense search index for clip.cool assets (docs/architecture.md).

Postgres is the source of truth; this index is a rebuildable projection (clips.services.reindex_all
rebuilds it). The `typesense` client is imported lazily so this module loads without the dependency
for `manage.py check`. Config from settings: TYPESENSE_HOST / _PORT / _PROTOCOL / _API_KEY.
"""
import logging
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)

COLLECTION = "assets"

# Searchable text = title + OCR'd burned-in text + tags. owner_id/mime are filter/facet fields;
# created_at is the numeric default-sort field (Typesense requires one).
_SCHEMA = {
    "name": COLLECTION,
    "fields": [
        {"name": "title", "type": "string"},
        {"name": "description", "type": "string"},   # AI vision caption
        {"name": "ocr_text", "type": "string"},
        {"name": "tags", "type": "string[]", "facet": True},
        {"name": "mime", "type": "string", "facet": True, "optional": True},
        {"name": "owner_id", "type": "int64", "facet": True},
        {"name": "created_at", "type": "int64"},
    ],
    "default_sorting_field": "created_at",
}


@lru_cache(maxsize=1)
def _client():
    import typesense

    return typesense.Client({
        "nodes": [{
            "host": settings.TYPESENSE_HOST,
            "port": str(settings.TYPESENSE_PORT),
            "protocol": settings.TYPESENSE_PROTOCOL,
        }],
        "api_key": settings.TYPESENSE_API_KEY,
        "connection_timeout_seconds": 5,
    })


def ensure_collection():
    """Create the `assets` collection if absent. Idempotent — safe to call before every write."""
    import typesense

    client = _client()
    try:
        client.collections[COLLECTION].retrieve()
    except typesense.exceptions.ObjectNotFound:
        client.collections.create(_SCHEMA)


def recreate_collection():
    """Drop + recreate the collection to pick up a schema change (e.g. a new field). The index is
    a rebuildable projection of Postgres, so callers follow this with services.reindex_all()."""
    import typesense

    client = _client()
    try:
        client.collections[COLLECTION].delete()
    except typesense.exceptions.ObjectNotFound:
        pass
    client.collections.create(_SCHEMA)


def upsert(asset):
    ensure_collection()
    _client().collections[COLLECTION].documents.upsert(_doc(asset))


def remove(asset_id):
    import typesense

    try:
        _client().collections[COLLECTION].documents[str(asset_id)].delete()
    except typesense.exceptions.ObjectNotFound:
        pass


def query(q, *, owner_id=None, limit=40):
    """Return matching asset-id strings, best match first (typo-tolerant). Empty q ⇒ newest first."""
    ensure_collection()
    params = {
        "q": q or "*",
        "query_by": "title,description,ocr_text,tags",
        "per_page": min(max(limit, 1), 250),
        "sort_by": "_text_match:desc,created_at:desc",
    }
    if owner_id is not None:
        params["filter_by"] = f"owner_id:={int(owner_id)}"
    res = _client().collections[COLLECTION].documents.search(params)
    return [hit["document"]["id"] for hit in res.get("hits", [])]


def _doc(asset):
    return {
        "id": str(asset.id),
        "title": asset.title or "",
        "description": asset.description or "",
        "ocr_text": asset.ocr_text or "",
        "tags": list(asset.tags or []),
        "mime": asset.mime or "",
        "owner_id": int(asset.owner_id),
        "created_at": int(asset.created_at.timestamp()) if asset.created_at else 0,
    }
