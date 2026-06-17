"""Choice enums for the recommendations engine.

Carried (trimmed) from the zrag reference — the hard-won *domain shape*, not its code.
zrag had ~9 categories and 40+ action types accreted over time; we start with a
representative, high-value subset and add as task-types come online (ADR 0013 §3, §"cold
start"). These are plain `CharField` choices, so widening the set later is purely additive.
"""
from django.db import models


class TaskType(models.TextChoices):
    """A generation task-type. ADR 0013 §3: the loop is *per-task-type* — each owns its own
    prompt versions / exemplars / eval. We start with the single URL-analysis task."""

    URL_ANALYZE = "url_analyze", "URL analysis"


class Category(models.TextChoices):
    """What area of the page a recommendation addresses (drives grouping/badges in the UI)."""

    NEW_CONTENT = "new_content", "New content"
    EXISTING_CONTENT = "existing_content", "Existing content"
    METADATA = "metadata", "Metadata"
    TECHNICAL = "technical", "Technical"
    SCHEMA = "schema", "Schema"
    AEO_READINESS = "aeo_readiness", "AEO readiness"
    AUTHORITY = "authority", "Authority"
    ACCESSIBILITY = "accessibility", "Accessibility"


class ActionType(models.TextChoices):
    """The concrete action a recommendation proposes. In Tier 1 this is descriptive metadata
    (it tells the user *what kind* of fix this is); the one-click executor that maps action_type
    → handler is a Tier 2 follow-on (ADR 0013)."""

    GENERATE_CONTENT = "generate_content", "Generate content"
    EXPAND_CONTENT = "expand_content", "Expand content"
    REFRESH_CONTENT = "refresh_content", "Refresh content"
    GENERATE_METADATA = "generate_metadata", "Generate metadata"
    GENERATE_SCHEMA = "generate_schema", "Generate schema"
    GENERATE_FAQ = "generate_faq", "Generate FAQ"
    IMPROVE_READABILITY = "improve_readability", "Improve readability"
    FIX_TECHNICAL = "fix_technical", "Fix technical issue"
    FIX_ACCESSIBILITY = "fix_accessibility", "Fix accessibility"
    FIX_INTERNAL_LINKS = "fix_internal_links", "Fix internal links"
    OPTIMIZE_AEO = "optimize_aeo", "Optimize for answer engines"
    TARGET_SNIPPET = "target_snippet", "Target featured snippet"
    INJECT_AUTHORITY = "inject_authority", "Inject authority signals"


class Effort(models.TextChoices):
    """How much work applying the recommendation takes — the same four tiers zrag settled on."""

    ONE_CLICK = "one_click", "One click"
    REVIEW = "review", "Review"
    DEV_REQUIRED = "dev_required", "Dev required"
    EXTERNAL = "external", "External"


class AnalysisStatus(models.TextChoices):
    """Lifecycle of a single analyze run."""

    PENDING = "pending", "Pending"
    FETCHING = "fetching", "Fetching"
    GENERATING = "generating", "Generating"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"


class InteractionKind(models.TextChoices):
    """The leading accept/apply signal (ADR 0013 §5) — fast, cheap, available in Tier 1. Only
    ever a *leading* indicator, validated against the (Tier 2) lagging outcome verdict."""

    VIEWED = "viewed", "Viewed"
    ACCEPTED = "accepted", "Accepted"
    DISMISSED = "dismissed", "Dismissed"
    APPLIED = "applied", "Applied"
