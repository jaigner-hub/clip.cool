"""Seed meme templates from Imgflip's free get_memes API (no key) into R2 + the Template table.

These are the ~100 most popular blank meme formats — the building blocks the in-app builder
captions. Idempotent: existing (source, source_id) rows are skipped, so it's safe to re-run.

    ./ac ... manage.py seed_templates        # all
    ./ac ... manage.py seed_templates --limit 20
"""
import io

import httpx
from django.core.management.base import BaseCommand

from clips import storage
from clips.models import Template

GET_MEMES = "https://api.imgflip.com/get_memes"
_EXT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}


class Command(BaseCommand):
    help = "Seed meme templates from Imgflip's get_memes API into R2 + the Template table."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="Max templates to seed (0 = all).")

    def handle(self, *args, **opts):
        from PIL import Image

        resp = httpx.get(GET_MEMES, timeout=30)
        resp.raise_for_status()
        memes = ((resp.json().get("data") or {}).get("memes")) or []
        if opts["limit"]:
            memes = memes[: opts["limit"]]
        self.stdout.write(f"Imgflip returned {len(memes)} templates")

        created = skipped = failed = 0
        for m in memes:
            sid = str(m.get("id") or "")
            name = m.get("name") or "Untitled"
            if not sid:
                continue
            if Template.objects.filter(source="imgflip", source_id=sid).exists():
                skipped += 1
                continue
            try:
                img = httpx.get(m.get("url") or "", timeout=30, follow_redirects=True)
                img.raise_for_status()
                data = img.content
                im = Image.open(io.BytesIO(data))
                im.load()
                ctype = Image.MIME.get(im.format or "", "image/png")
                key = "templates/imgflip/%s%s" % (sid, _EXT.get(im.format or "", ".png"))
                storage.upload_bytes(key, data, ctype)
                Template.objects.create(
                    name=name, image_key=key, mime=ctype,
                    width=m.get("width") or im.size[0], height=m.get("height") or im.size[1],
                    source="imgflip", source_id=sid,
                )
                created += 1
                self.stdout.write("  + %s" % name)
            except Exception as e:
                failed += 1
                self.stderr.write("  ! %s: %s" % (name, e))
        self.stdout.write(self.style.SUCCESS(
            "templates: +%d created, %d existing, %d failed" % (created, skipped, failed)
        ))
