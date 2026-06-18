from django.contrib import admin

from .models import Asset


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "owner", "status", "mime", "width", "height", "created_at")
    list_filter = ("status", "mime", "created_at")
    search_fields = ("id", "title", "ocr_text", "sha256", "owner__username")
    readonly_fields = (
        "id", "original_key", "poster_key", "mime", "width", "height", "bytes",
        "sha256", "ocr_text", "created_at", "updated_at",
    )
    ordering = ("-created_at",)
