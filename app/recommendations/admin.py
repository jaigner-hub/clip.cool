from django.contrib import admin

from .models import (
    Analysis,
    PageSnapshot,
    PromptVersion,
    Recommendation,
    RecommendationInteraction,
)


@admin.register(PromptVersion)
class PromptVersionAdmin(admin.ModelAdmin):
    list_display = ("task_type", "version", "label", "model", "is_champion", "created_at")
    list_filter = ("task_type", "is_champion", "model")
    search_fields = ("label", "notes", "system_prompt")
    readonly_fields = ("created_at",)


class RecommendationInline(admin.TabularInline):
    model = Recommendation
    extra = 0
    fields = ("position", "category", "action_type", "title", "effort", "priority_score")
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(PageSnapshot)
class PageSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "url", "content_hash", "fetched_at")
    search_fields = ("url", "title", "content_hash")
    readonly_fields = ("url", "title", "meta", "text", "content_hash", "fetched_at")


@admin.register(Analysis)
class AnalysisAdmin(admin.ModelAdmin):
    list_display = ("id", "url", "status", "organization", "model", "created_at", "completed_at")
    list_filter = ("status", "model")
    search_fields = ("url", "page__title")
    readonly_fields = ("created_at", "completed_at", "usage")
    list_select_related = ("page", "organization")
    inlines = [RecommendationInline]


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ("title", "analysis", "category", "action_type", "effort", "priority_score")
    list_filter = ("category", "action_type", "effort")
    search_fields = ("title", "why", "description")


@admin.register(RecommendationInteraction)
class RecommendationInteractionAdmin(admin.ModelAdmin):
    list_display = ("id", "recommendation", "kind", "actor", "created_at")
    list_filter = ("kind",)
