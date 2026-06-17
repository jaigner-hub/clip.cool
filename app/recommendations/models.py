"""Recommendations data model (ADR 0013).

Five tables for the Tier-1 (URL-first, stateless) cut, with seams left for Tier 2:

- `PromptVersion` — the foundational enabler: prompts are *versioned data*, not Python
  constants. One **champion** per task-type is the active prompt; the service fetches it and
  never imports a prompt constant. Runtime-editable, auditable, no deploy to change a prompt.
- `Analysis` — one row per analyze run; also the unlabeled training corpus (input→output).
  `organization`/`project` are nullable: null = Tier 1 (anonymous/prospect); populated later
  for Tier 2 (project-scoped, the only tier with the gold outcome signal).
- `PageSnapshot` — the fetched page content (title/meta/text), split off `Analysis` and deduped
  by `content_hash` so the large text blob stays off the hot run row and is stored once.
- `Recommendation` — the categorized/prioritized/executable shape carried from zrag.
- `RecommendationInteraction` — the leading accept/apply signal (ADR 0013 §5).

Out of scope for now (ADR 0013 "earned follow-on"): RecommendationOutcome (90-day verdict),
Exemplar retrieval, eval-gated prompt promotion. The schema doesn't preclude them.
"""
from django.conf import settings
from django.db import models

from .enums import (
    ActionType,
    AnalysisStatus,
    Category,
    Effort,
    InteractionKind,
    TaskType,
)


class PromptVersionQuerySet(models.QuerySet):
    def champion_for(self, task_type):
        """The active prompt for a task-type, or None. ADR 0013 §2: the service resolves the
        champion at generation time — there is no 'the prompt' constant in code."""
        return self.filter(task_type=task_type, is_champion=True).first()


class PromptVersion(models.Model):
    """A versioned, runtime-editable prompt for one task-type (ADR 0013 §2).

    Promotion (which version is champion) is a deliberate, human-gated act in this cut — there
    is no online auto-rewriting (ADR 0013 §6). `version` is monotonic per task-type for audit.
    """

    task_type = models.CharField(max_length=40, choices=TaskType.choices, db_index=True)
    version = models.PositiveIntegerField()
    label = models.CharField(max_length=200, blank=True)
    system_prompt = models.TextField(help_text="The editable instruction text sent to the model.")
    model = models.CharField(
        max_length=60, default="sonnet-4.6",
        help_text="OpenRouter model alias (see recommendations.llm.LLMClient.MODELS).",
    )
    temperature = models.FloatField(default=0.4)
    is_champion = models.BooleanField(default=False, db_index=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="prompt_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PromptVersionQuerySet.as_manager()

    class Meta:
        ordering = ["task_type", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["task_type", "version"], name="unique_prompt_version_per_task"
            ),
            # At most one champion per task-type. Postgres partial unique index; the service
            # also enforces it transactionally (set_champion) so the invariant holds on SQLite.
            models.UniqueConstraint(
                fields=["task_type"],
                condition=models.Q(is_champion=True),
                name="one_champion_per_task",
            ),
        ]

    def __str__(self):
        star = " ★" if self.is_champion else ""
        return f"{self.task_type} v{self.version}{star}"


class PageSnapshot(models.Model):
    """The fetched content of a URL, split out of `Analysis` so the (potentially large) page text
    stays out of the frequently-read `Analysis` row, and identical content is stored once.

    Keyed by `content_hash` (sha256 of the extracted text) — many analyses of the same content
    reuse one snapshot (storage dedup now; a future fetch-skip can reuse a fresh snapshot without
    re-fetching). `url` here is the *fetched* URL (post-redirect); `Analysis.url` is what was asked.
    """

    url = models.URLField(max_length=2000)
    title = models.CharField(max_length=1000, blank=True)
    meta = models.TextField(blank=True)
    text = models.TextField(blank=True)  # extracted input context (the blob kept off the hot row)
    content_hash = models.CharField(max_length=64, unique=True)  # sha256 hex — the dedup key
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fetched_at"]

    def __str__(self):
        return f"{self.url} ({self.content_hash[:8]})"


class Analysis(models.Model):
    """One analyze run over one URL. The unit the JSON API returns and the corpus row.

    The fetched page (title/meta/text) lives in a separate `PageSnapshot` (FK `page`) so this row
    stays lean and identical content is stored once; the run still references it for a reproducible
    (input → prompt → output) record for the future learning loop.
    """

    url = models.URLField(max_length=2000)
    # Nullable until Tier 2: null = stateless Tier-1 (anonymous/prospect) run.
    organization = models.ForeignKey(
        "tenancy.Organization", on_delete=models.CASCADE, null=True, blank=True,
        related_name="recommendation_analyses",
    )
    project = models.ForeignKey(
        "tenancy.Project", on_delete=models.CASCADE, null=True, blank=True,
        related_name="recommendation_analyses",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="recommendation_analyses",
    )
    session_key = models.CharField(max_length=40, blank=True)  # anonymous Tier-1 attribution

    status = models.CharField(
        max_length=20, choices=AnalysisStatus.choices, default=AnalysisStatus.PENDING,
        db_index=True,
    )
    prompt_version = models.ForeignKey(
        PromptVersion, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="analyses",
    )
    model = models.CharField(max_length=60, blank=True)  # actual OpenRouter model id used

    page = models.ForeignKey(
        PageSnapshot, on_delete=models.SET_NULL, null=True, blank=True, related_name="analyses",
    )

    usage = models.JSONField(default=dict, blank=True)  # token/cost accounting
    error = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "analyses"

    def __str__(self):
        return f"Analysis #{self.pk} {self.url} ({self.status})"


class Recommendation(models.Model):
    """One categorized, prioritized, (eventually) executable recommendation.

    Field shape carried from zrag's mature model: `why` is the data-backed reasoning, `effort`
    sets expectations, `priority_score` orders the list, `action_payload` holds the parameters a
    future one-click executor would consume (descriptive in Tier 1).
    """

    analysis = models.ForeignKey(
        Analysis, on_delete=models.CASCADE, related_name="recommendations"
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    action_type = models.CharField(max_length=30, choices=ActionType.choices)
    title = models.CharField(max_length=500)
    why = models.TextField(blank=True, help_text="Data-backed reasoning for the recommendation.")
    description = models.TextField(blank=True, help_text="What to do.")
    effort = models.CharField(max_length=20, choices=Effort.choices, default=Effort.REVIEW)
    priority_score = models.FloatField(default=0, db_index=True)
    impact_label = models.CharField(max_length=100, blank=True)
    effort_label = models.CharField(max_length=100, blank=True)
    action_payload = models.JSONField(default=dict, blank=True)
    position = models.PositiveIntegerField(default=0)  # display order as generated

    class Meta:
        ordering = ["analysis", "position"]

    def __str__(self):
        return self.title


class RecommendationInteraction(models.Model):
    """A user signal on a recommendation — the leading indicator of the ADR 0013 loop.

    Logged in Tier 1 too (no account needed); `session_key` attributes anonymous interactions.
    """

    recommendation = models.ForeignKey(
        Recommendation, on_delete=models.CASCADE, related_name="interactions"
    )
    kind = models.CharField(max_length=20, choices=InteractionKind.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="recommendation_interactions",
    )
    session_key = models.CharField(max_length=40, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} on rec #{self.recommendation_id}"
