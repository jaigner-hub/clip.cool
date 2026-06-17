"""Backfill: move each Analysis's inline fetched-page columns into a PageSnapshot (linked via
`page`), before 0005 drops those columns. Idempotent and a no-op on an empty table; runs while the
old columns still exist in the migration state. Reverse is a no-op (0005 / 0003 undo the schema)."""
from django.db import migrations


def backfill(apps, schema_editor):
    Analysis = apps.get_model("recommendations", "Analysis")
    PageSnapshot = apps.get_model("recommendations", "PageSnapshot")
    pending = Analysis.objects.filter(page__isnull=True).exclude(page_content_hash="")
    for a in pending.iterator():
        snapshot, _ = PageSnapshot.objects.get_or_create(
            content_hash=a.page_content_hash,
            defaults={
                "url": a.url,                # requested URL — the fetched URL wasn't stored separately
                "title": a.fetched_title,
                "meta": a.fetched_meta,
                "text": a.page_text,
            },
        )
        a.page = snapshot
        a.save(update_fields=["page"])


class Migration(migrations.Migration):
    dependencies = [("recommendations", "0003_pagesnapshot")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
