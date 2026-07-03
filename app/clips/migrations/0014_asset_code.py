import secrets
import string

from django.db import migrations, models

import clips.models

_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase


def gen_codes(apps, schema_editor):
    """Backfill a unique short code for every existing clip (new rows get one from the field default).
    Kept collision-free within this run so the unique constraint added next can't trip."""
    Asset = apps.get_model("clips", "Asset")
    used = set()
    for asset in Asset.objects.all().only("id", "code"):
        if asset.code:
            used.add(asset.code)
            continue
        while True:
            code = "".join(secrets.choice(_ALPHABET) for _ in range(7))
            if code not in used:
                break
        asset.code = code
        asset.save(update_fields=["code"])
        used.add(code)


class Migration(migrations.Migration):

    dependencies = [
        ("clips", "0013_asset_from_recorder_asset_remixed_from"),
    ]

    operations = [
        # 1) add the column non-unique with a blank placeholder (a callable default would write one
        #    shared value to all existing rows, violating uniqueness before the backfill runs).
        migrations.AddField(
            model_name="asset",
            name="code",
            field=models.CharField(default="", editable=False, max_length=16),
        ),
        # 2) give every existing row a distinct code.
        migrations.RunPython(gen_codes, migrations.RunPython.noop),
        # 3) enforce uniqueness + wire up the real per-insert default.
        migrations.AlterField(
            model_name="asset",
            name="code",
            field=models.CharField(default=clips.models.gen_code, editable=False, max_length=16, unique=True),
        ),
    ]
