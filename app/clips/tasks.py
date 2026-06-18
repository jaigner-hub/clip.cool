"""Procrastinate tasks for clips (ADR 0008) — thin adapters over the service layer.

Queues:
  thumbs — the heavy per-upload derive (download + Pillow poster + Tesseract OCR + index);
  index  — (re)indexing an existing asset into Typesense.
Both are served by the worker container (`--queues …,index,thumbs` in roles/clip_web compose).
"""
from procrastinate.contrib.django import app

from . import services


@app.task(queue="thumbs")
def process_asset(asset_id: str) -> None:
    services.process_asset(asset_id)


@app.task(queue="index")
def index_asset(asset_id: str) -> None:
    services.index_asset(asset_id)
