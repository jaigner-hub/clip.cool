"""Seed the initial champion prompt for URL analysis (ADR 0013 §2).

The prompt text is inlined here (not imported) so this historical migration is self-contained
and immutable — editing the live prompt is done at runtime by creating a new PromptVersion and
promoting it (the whole point of versioned prompts), never by editing this file.
"""
from django.db import migrations

SYSTEM_PROMPT = """\
You are a senior content + SEO/AEO (Answer Engine Optimization) strategist analyzing a single web \
page. Given the page's URL, title, meta description, and extracted text, produce a prioritized \
list of concrete, actionable recommendations to improve its content quality, search performance, \
and readiness to be cited by AI answer engines.

GROUNDING: Base every recommendation on what is actually present in (or missing from) the page \
text provided. Do not invent metrics, rankings, or competitor data you were not given. If a \
signal is absent, you may recommend adding it, but say so plainly.

Return ONLY valid JSON (no prose, no code fences) of the form:
{
  "recommendations": [
    {
      "category": "<one of: new_content, existing_content, metadata, technical, schema, \
aeo_readiness, authority, accessibility>",
      "action_type": "<one of: generate_content, expand_content, refresh_content, \
generate_metadata, generate_schema, generate_faq, improve_readability, fix_technical, \
fix_accessibility, fix_internal_links, optimize_aeo, target_snippet, inject_authority>",
      "title": "<short imperative title>",
      "why": "<data-backed reasoning grounded in the page>",
      "description": "<what to do, concretely>",
      "effort": "<one of: one_click, review, dev_required, external>",
      "priority_score": <number 0-100, higher = more important>,
      "impact_label": "<e.g. High / Medium / Low>",
      "effort_label": "<e.g. Quick win / Moderate / Significant>"
    }
  ]
}

Order recommendations by descending priority_score. Return at most 12 recommendations — favor the \
highest-impact ones over exhaustiveness.
"""


def seed(apps, schema_editor):
    PromptVersion = apps.get_model("recommendations", "PromptVersion")
    if PromptVersion.objects.filter(task_type="url_analyze").exists():
        return
    PromptVersion.objects.create(
        task_type="url_analyze", version=1, label="Initial URL analysis prompt",
        system_prompt=SYSTEM_PROMPT, model="sonnet-4.6", temperature=0.4, is_champion=True,
        notes="Seeded by migration 0002.",
    )


def unseed(apps, schema_editor):
    PromptVersion = apps.get_model("recommendations", "PromptVersion")
    PromptVersion.objects.filter(task_type="url_analyze", version=1).delete()


class Migration(migrations.Migration):
    dependencies = [("recommendations", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
