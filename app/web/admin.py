"""Read-only admin surfaces: impersonation audit (ADR 0010) + Procrastinate worker status (ADR 0008)."""
from django.apps import apps as django_apps
from django.contrib import admin
from django.utils import timezone

from .models import ContactMessage, ImpersonationEvent


@admin.register(ImpersonationEvent)
class ImpersonationEventAdmin(admin.ModelAdmin):
    list_display = ["at", "kind", "impersonator", "impersonated"]
    list_filter = ["kind"]
    search_fields = ["impersonator__email", "impersonated__email"]
    readonly_fields = ["impersonator", "impersonated", "kind", "at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # append-only audit


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    """Marketing leads from the public contact form (ADR 0012) — read-only."""
    list_display = ["created_at", "name", "email", "company"]
    search_fields = ["name", "email", "company", "message"]
    readonly_fields = ["name", "email", "company", "message", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# --- Procrastinate worker status (ADR 0008) ---
# The built-in Procrastinate admin only registers *jobs*; worker liveness lives in a separate
# `procrastinate_workers` table (one row per live worker, with a heartbeat). Surface it so the
# /admin/ monitoring view answers "is my worker alive?", not just "what happened to this job?".
# Guarded by is_installed: Procrastinate is Postgres-only, so it's absent under default SQLite dev
# (importing its models then would raise — the model's app isn't in INSTALLED_APPS).
if django_apps.is_installed("procrastinate.contrib.django"):
    from procrastinate.contrib.django.models import ProcrastinateWorker

    # Workers heartbeat every 10s; Procrastinate prunes them after 30s of silence (worker.py
    # defaults). So "no heartbeat in 30s" == effectively dead/about-to-be-pruned.
    WORKER_STALE_AFTER = 30  # seconds

    @admin.register(ProcrastinateWorker)
    class ProcrastinateWorkerAdmin(admin.ModelAdmin):
        list_display = ["id", "liveness", "last_heartbeat"]
        ordering = ["-last_heartbeat"]

        @admin.display(description="Status")
        def liveness(self, obj):
            # Emoji, not inline-styled color: the strict CSP (style-src 'self') drops inline
            # style attributes, so a colored <span> would render plain anyway.
            age = int((timezone.now() - obj.last_heartbeat).total_seconds())
            mark = "✅ alive" if age <= WORKER_STALE_AFTER else "⚠️ stale"
            return f"{mark} ({age}s since heartbeat)"

        def has_add_permission(self, request):
            return False

        def has_change_permission(self, request, obj=None):
            return False

        def has_delete_permission(self, request, obj=None):
            return False
