"""Cloudflare R2 (S3-compatible) object storage for clip.cool media (docs/architecture.md).

`boto3` is imported lazily inside `_client()` so this module — and `manage.py check` — load
without the dependency or R2 credentials present. Only the actual storage calls need them.

Config comes from settings (env, ultimately stash clip/web/*): R2_S3_API_ENDPOINT,
R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, and optional R2_PUBLIC_BASE.
"""
import io
import logging
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)


class StorageNotConfigured(RuntimeError):
    """Raised when an R2 operation is attempted without the credentials configured."""


@lru_cache(maxsize=1)
def _client():
    if not (settings.R2_S3_API_ENDPOINT and settings.R2_ACCESS_KEY_ID and settings.R2_BUCKET_NAME):
        raise StorageNotConfigured(
            "R2 is not configured (need R2_S3_API_ENDPOINT, R2_ACCESS_KEY_ID, R2_BUCKET_NAME)."
        )
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.R2_S3_API_ENDPOINT,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",  # R2 ignores region, but boto3 requires one
        config=Config(signature_version="s3v4"),
    )


def presign_put(key, content_type, expires=3600):
    """Presigned PUT URL — the browser uploads bytes straight to the bucket (the app never streams
    the file). The client MUST send the same Content-Type it was signed with."""
    return _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.R2_BUCKET_NAME, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def presign_get(key, expires=3600, *, filename=None):
    """Presigned GET. With `filename`, forces a download (Content-Disposition: attachment, named
    that) — needed to *save* a cross-origin R2 object, since the HTML `download` attr is ignored
    across origins."""
    params = {"Bucket": settings.R2_BUCKET_NAME, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = 'attachment; filename="%s"' % filename
    return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires)


def public_url(key):
    """Delivery URL for an object. Uses the public R2_PUBLIC_BASE (r2.dev / CDN domain) when set,
    otherwise a short-lived presigned GET so dev works without a public bucket."""
    if not key:
        return ""
    if settings.R2_PUBLIC_BASE:
        return f"{settings.R2_PUBLIC_BASE}/{key}"
    return presign_get(key)


def download_bytes(key):
    buf = io.BytesIO()
    _client().download_fileobj(settings.R2_BUCKET_NAME, key, buf)
    return buf.getvalue()


def upload_bytes(key, data, content_type):
    _client().put_object(
        Bucket=settings.R2_BUCKET_NAME, Key=key, Body=data, ContentType=content_type
    )


def copy(src_key, dst_key):
    """Server-side copy within the bucket (the bytes never stream through the app) — used to clone a
    template's video into a new owned object for a remix (clips.services.create_remix)."""
    _client().copy_object(
        Bucket=settings.R2_BUCKET_NAME,
        CopySource={"Bucket": settings.R2_BUCKET_NAME, "Key": src_key},
        Key=dst_key,
    )


def delete(key):
    """Delete one object. Idempotent (S3 delete of a missing key succeeds)."""
    if not key:
        return
    _client().delete_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
