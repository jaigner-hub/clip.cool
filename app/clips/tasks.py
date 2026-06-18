"""Procrastinate tasks for clips (ADR 0008) — thin adapters over the service layer.

Queues:
  thumbs    — the heavy per-upload derive (download + Pillow poster + Tesseract OCR + index);
  transcode — ffmpeg video renditions (AV1/VP9/H.264 + poster);
  index     — (re)indexing + vision auto-describe.
Served by the worker container (`--queues default,index,thumbs,transcode`, roles/clip_web compose).

Each task calls close_old_connections() first: the worker is long-lived, so a connection the DB
server has since dropped (PgBouncer/HAProxy restart, leader switchover) gets health-checked and
reconnected here instead of erroring mid-query (CONN_HEALTH_CHECKS in settings/prod.py).
"""
import functools

from django.db import close_old_connections
from procrastinate.contrib.django import app

from . import services


def _db_fresh(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        close_old_connections()
        return fn(*args, **kwargs)
    return wrapper


@app.task(queue="thumbs")
@_db_fresh
def process_asset(asset_id: str) -> None:
    services.process_asset(asset_id)


@app.task(queue="transcode")
@_db_fresh
def transcode_asset(asset_id: str) -> None:
    services.transcode_asset(asset_id)


@app.task(queue="index")
@_db_fresh
def index_asset(asset_id: str) -> None:
    services.index_asset(asset_id)


@app.task(queue="index")
@_db_fresh
def autodescribe_asset(asset_id: str, force_title: bool = False) -> None:
    services.autodescribe_asset(asset_id, force_title=force_title)
