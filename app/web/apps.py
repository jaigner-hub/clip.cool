from django.apps import AppConfig


class WebConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "web"

    def ready(self):
        from . import signals  # noqa: F401  (connect hijack audit receivers)

        # Single init point for OpenTelemetry — ready() fires for BOTH the ASGI web workers and the
        # Procrastinate worker (ADR 0008), and it's a no-op without OTEL_EXPORTER_OTLP_ENDPOINT, so
        # dev + the SQLite test suite are untouched (infra-gap-analysis.md #7, ADR 0015).
        from keygrip.tracing import configure_tracing

        configure_tracing()
