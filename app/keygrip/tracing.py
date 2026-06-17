"""OpenTelemetry tracing wiring (infra-gap-analysis.md #7; ADR 0015).

`configure_tracing()` is called once from `web.apps.WebConfig.ready()`, so the SAME hook covers both
processes that serve the app: the ASGI web workers (gunicorn/uvicorn) and the Procrastinate worker
(ADR 0008). It is a **no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set** — mirroring the `SENTRY_DSN`
guard in settings — so local dev and the SQLite test suite are completely untouched, and traces only
flow where an OTLP collector (Alloy → Tempo, ADR 0015) actually exists.

Spans captured: web requests (server), psycopg queries, and httpx calls — the latter being our
external fan-out to OpenRouter / web-search / fal.ai, exactly the cross-process latency #7 is about.
Export is OTLP/gRPC to Alloy, which batches and forwards to Tempo.

`configure_tracing()` sets the provider and instruments psycopg + httpx (covers BOTH the web and the
worker process). Web *request* spans are added separately via `instrument_asgi()` from
`keygrip/asgi.py`: we serve on uvicorn (ASGI), and the Django instrumentation's middleware only
traces the WSGI path — verified empirically that ASGI requests produced no spans — so we wrap the
ASGI app with OTel's own ASGI server middleware instead.

`service.name` (keygrip-web vs keygrip-worker) and the other resource attributes
(deployment.environment, service.version) come from `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES`
in the container env — the SDK's default resource detector reads them, so they aren't restated here.
"""
import logging
import os

logger = logging.getLogger(__name__)

_configured = False


def configure_tracing():
    """Idempotent. No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    global _configured
    if _configured or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    # Parent-based ratio sampler: keep a child span iff its parent was sampled, else sample roots at
    # this rate — so a trace is all-or-nothing across processes. Default 0.1 mirrors the rate the
    # Sentry SDK already used (settings/base.py). The exporter reads its endpoint from the env.
    sample_rate = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "0.1"))
    provider = TracerProvider(
        resource=Resource.create(),
        sampler=ParentBased(TraceIdRatioBased(sample_rate)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    # Instrument AFTER set_tracer_provider so the instrumentors bind to our provider. psycopg + httpx
    # cover DB queries and external calls in BOTH the web and worker processes; web request spans are
    # added by instrument_asgi() (the ASGI server path, see module docstring).
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    HTTPXClientInstrumentor().instrument()
    PsycopgInstrumentor().instrument()

    _configured = True
    logger.info("OpenTelemetry tracing configured (service=%s)", os.environ.get("OTEL_SERVICE_NAME"))


def instrument_asgi(app):
    """Wrap an ASGI app with OTel's server middleware so every HTTP request gets a SERVER span.
    No-op (returns `app` unchanged) unless OTEL_EXPORTER_OTLP_ENDPOINT is set. Called from
    keygrip/asgi.py after configure_tracing() has set the provider."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return app
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    from opentelemetry.util.http import get_excluded_urls

    # Don't trace health/metrics/scrape noise — otherwise at high sample rates (dev runs 100%)
    # Prometheus's /metrics scrapes + LB health checks bury the requests you actually care about.
    # Regex list from OTEL_PYTHON_ASGI_EXCLUDED_URLS; empty ⇒ trace everything.
    return OpenTelemetryMiddleware(app, excluded_urls=get_excluded_urls("ASGI"))
