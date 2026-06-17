"""Ninja schemas — the typed API contract for the recommendations API (ADR 0011, ADR 0013)."""
from datetime import datetime

from ninja import Schema


class AnalysisIn(Schema):
    url: str
    prompt_version_id: int | None = None  # default: the champion prompt


class RecommendationOut(Schema):
    id: int
    category: str
    action_type: str
    title: str
    why: str
    description: str
    effort: str
    priority_score: float
    impact_label: str
    effort_label: str
    position: int


class AnalysisOut(Schema):
    id: int
    url: str
    status: str
    model: str
    fetched_title: str
    usage: dict
    error: str
    duration_ms: int | None
    created_at: datetime
    recommendations: list[RecommendationOut]


class InteractionIn(Schema):
    kind: str
    metadata: dict = {}


class OkOut(Schema):
    ok: bool
    id: int | None = None


class PromptVersionIn(Schema):
    task_type: str = "url_analyze"
    system_prompt: str
    model: str = "sonnet-4.6"
    temperature: float = 0.4
    label: str = ""
    notes: str = ""
    make_champion: bool = False


class PromptVersionOut(Schema):
    id: int
    task_type: str
    version: int
    label: str
    model: str
    temperature: float
    is_champion: bool
    system_prompt: str
    created_at: datetime
