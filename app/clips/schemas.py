"""Ninja schemas — the typed contract for the clips JSON API (ADR 0011)."""
from datetime import datetime

from ninja import Schema


class PresignIn(Schema):
    filename: str
    content_type: str


class PresignOut(Schema):
    key: str
    url: str
    method: str
    headers: dict


class FinalizeIn(Schema):
    key: str
    title: str = ""
    content_type: str = ""
    tags: list[str] = []
    crop: dict | None = None   # tab-recorder source crop {x,y,w,h} fractions; baked in at transcode


class AssetOut(Schema):
    id: str
    title: str
    status: str
    mime: str
    width: int | None = None
    height: int | None = None
    tags: list[str]
    url: str
    poster_url: str
    created_at: datetime


class SearchOut(Schema):
    q: str
    count: int
    results: list[AssetOut]
