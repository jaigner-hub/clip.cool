"""Media assets for clip.cool (docs/architecture.md).

An Asset is one uploaded piece of media. The bytes live in Cloudflare R2 (content under
`original_key`); Postgres holds the metadata and is the source of truth. Typesense indexes the
searchable fields (title, OCR'd text, tags) — see clips/search.py. The image slice fills width/
height/mime/poster/ocr_text in the worker (clips.services.process_asset); video renditions come
later with the transcode tier.
"""
import uuid

from django.conf import settings
from django.db import models


class Asset(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"   # uploaded, awaiting processing
        READY = "ready", "Ready"         # processed + indexed
        FAILED = "failed", "Failed"

    # UUID primary key: it doubles as the public id and the basis of the R2 object keys, so it must
    # be unguessable and stable (not a sequential int).
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assets"
    )

    # R2 object keys (bucket-relative). poster = the WebP thumbnail derived in the worker.
    original_key = models.CharField(max_length=512)
    poster_key = models.CharField(max_length=512, blank=True)

    mime = models.CharField(max_length=128, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    bytes = models.PositiveBigIntegerField(null=True, blank=True)
    # sha256 of the original bytes — exact-duplicate detection (perceptual pHash dedup is a later
    # follow-up, docs/architecture.md). Indexed so a future collapse can look up by hash.
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    # Searchable surface (mirrored into Typesense).
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)   # AI vision caption (clips.llm), searchable
    ocr_text = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    # Public clips are searchable by everyone (the shared catalog); private = owner-only.
    # Default public — clip.cool is a shared meme host.
    is_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "-created_at"])]

    def __str__(self):
        return self.title or str(self.id)


class Template(models.Model):
    """A blank meme template the in-app builder captions (docs/architecture.md, Phase 1). Seeded
    from Imgflip's get_memes (clips.management.commands.seed_templates). The image bytes live in R2;
    this is metadata only. Distinct from Asset — templates are building blocks, not searchable clips."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    image_key = models.CharField(max_length=512)   # R2 object key
    mime = models.CharField(max_length=128, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=32, default="imgflip")
    source_id = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["source", "source_id"], name="uniq_template_source"),
        ]

    def __str__(self):
        return self.name
