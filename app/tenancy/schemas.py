"""Pydantic (Ninja) schemas — the typed API contract (ADR 0011), replacing zrag's
hand-rolled dicts. These drive request/response validation AND the OpenAPI schema.
"""
from ninja import Schema


class ProjectOut(Schema):
    id: int
    name: str
    slug: str
    organization_id: int
    is_active: bool


class ProjectIn(Schema):
    name: str


class ErrorOut(Schema):
    detail: str
