import re
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class TemplateHygieneTests(TestCase):
    """Django `{# #}` comments are SINGLE-LINE only; a multi-line one isn't parsed and leaks
    as visible page text. This has bitten us three times — guard ALL templates at the source,
    not per-page, so it can never ship again (CLAUDE.md convention)."""

    def test_no_multiline_django_comments(self):
        offenders = []
        for path in (settings.BASE_DIR / "templates").rglob("*.html"):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                # an opening {# with no closing #} on the same line = a (broken) multi-line comment
                if "{#" in line and "#}" not in line[line.index("{#"):]:
                    offenders.append(f"{path.relative_to(settings.BASE_DIR)}:{n}")
        self.assertEqual(offenders, [], f"multi-line {{# #}} comments leak as text: {offenders}")


class CSPTemplateHygieneTests(TestCase):
    """Strict CSP (`style-src 'self'`; `script-src 'self' 'nonce-…'`) SILENTLY drops inline
    styles and un-nonced inline scripts — no console error in the rendered page, the rule just
    doesn't apply (e.g. a `style="margin"` that vanishes, collapsing spacing). Guard every
    template at the source so a CSP-breaking pattern can't ship. See web/middleware.py.

    Templates rendered on CSP-relaxed pages (the Swagger docs surface + its OAuth2 redirect,
    under settings.CSP_RELAXED_PREFIXES) legitimately use inline styles/scripts, so they're exempt.
    """

    RELAXED = {"ninja/swagger.html", "oauth2-redirect.html"}

    @staticmethod
    def _strip_comments(text):
        # Blank comment BODIES while preserving newlines, so a `<style>`/`nonce=` mentioned inside
        # a comment isn't flagged but reported line numbers stay accurate.
        blank = lambda m: "\n" * m.group(0).count("\n")  # noqa: E731
        text = re.sub(r"<!--.*?-->", blank, text, flags=re.DOTALL)
        text = re.sub(r"{#.*?#}", blank, text, flags=re.DOTALL)
        text = re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", blank, text, flags=re.DOTALL)
        return text

    def _scan(self, predicate):
        """Yield 'rel:lineno' for every non-comment line where predicate(line) is truthy."""
        root = settings.BASE_DIR / "templates"
        offenders = []
        for path in root.rglob("*.html"):
            rel = str(path.relative_to(root))
            if rel in self.RELAXED:
                continue
            for n, line in enumerate(self._strip_comments(path.read_text()).splitlines(), 1):
                if predicate(line):
                    offenders.append(f"{rel}:{n}")
        return offenders

    def test_no_inline_styles(self):
        # WHY: `style-src 'self'` drops `style="…"` attributes and inline <style> blocks — exactly
        # the bug that collapsed the API-credentials form spacing. Use a CSS class instead.
        offenders = self._scan(
            lambda l: bool(re.search(r'\bstyle\s*=\s*["\']', l) or re.search(r"<style[\s>]", l))
        )
        self.assertEqual(offenders, [], f"inline styles violate style-src 'self': {offenders}")

    def test_no_inline_event_handlers(self):
        # WHY: no 'unsafe-inline' in script-src ⇒ native on*= handlers and HTMX hx-on are dead.
        # (Alpine's CSP-build `x-on:`/`@` are fine — parsed by Alpine, not executed by the browser.)
        offenders = self._scan(
            lambda l: bool(re.search(r'\son[a-z]+\s*=\s*["\']', l)) or "hx-on" in l
        )
        self.assertEqual(offenders, [], f"inline JS handlers violate script-src: {offenders}")

    def test_inline_scripts_carry_a_nonce(self):
        # WHY: an inline <script> (no src=) must carry nonce="{{ request.csp_nonce }}" or it won't
        # execute under script-src 'self' 'nonce-…'. External <script src> need no nonce.
        def offends(line):
            return any(
                "src=" not in tag and "nonce=" not in tag
                for tag in re.findall(r"<script\b[^>]*>", line)
            )
        offenders = self._scan(offends)
        self.assertEqual(offenders, [], f"inline scripts missing a nonce: {offenders}")


class CSPTests(TestCase):
    """The CSP must be strict (no escape hatches) and nonce-based, or it's theatre."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="t@example.com", email="t@example.com"
        )
        self.client.force_login(self.user)

    def test_policy_is_strict(self):
        csp = self.client.get(reverse("lab")).headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        # WHY: 'unsafe-inline'/'unsafe-eval' would defeat the point — Alpine's CSP build
        # and self-hosted scripts exist precisely so we never need them.
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertRegex(csp, r"script-src 'self' 'nonce-[\w-]+'")

    def test_nonce_is_per_request(self):
        a = self.client.get(reverse("lab")).headers["Content-Security-Policy"]
        b = self.client.get(reverse("lab")).headers["Content-Security-Policy"]
        # WHY: a reused/static nonce is as good as 'unsafe-inline'.
        self.assertNotEqual(a, b)

    def test_inline_script_carries_the_header_nonce(self):
        resp = self.client.get(reverse("lab"))
        nonce = re.search(r"'nonce-([\w-]+)'", resp.headers["Content-Security-Policy"]).group(1)
        # WHY: the rendered inline <script> must match the header nonce or the browser blocks it.
        self.assertContains(resp, f'nonce="{nonce}"')

    def test_form_action_allows_keycloak_logout(self):
        csp = self.client.get(reverse("lab")).headers["Content-Security-Policy"]
        # WHY: logout POSTs to /oidc/logout/ which 302s to Keycloak; form-action is enforced
        # across the redirect, so the Keycloak origin must be allowed or logout silently breaks.
        self.assertRegex(csp, r"form-action 'self' https://\S+")

    def test_htmx_fragment_endpoint(self):
        resp = self.client.get(reverse("lab_ping"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "pong")

    def test_home_also_gets_csp(self):
        # WHY: CSP is global middleware, not per-view — every HTML response carries it.
        resp = self.client.get(reverse("clips_search"))
        self.assertIn("Content-Security-Policy", resp.headers)


class SecurityHeaderTests(TestCase):
    """Defense-in-depth headers must mirror the static marketing/portal `_headers`.

    These ride global middleware (SecurityMiddleware + clickjacking + CSPMiddleware), so they
    must appear on every response, not just app pages. HSTS is deliberately absent — Cloudflare
    owns it at the edge — so we don't assert it here.
    """

    def test_response_carries_the_defense_in_depth_headers(self):
        resp = self.client.get(reverse("clips_search"))
        # WHY nosniff: stops content-type sniffing turning an upload into executable script.
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        # WHY Referrer-Policy: don't leak full URLs (paths, query) to cross-origin destinations.
        self.assertEqual(resp.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        # WHY X-Frame-Options: clickjacking defense alongside CSP frame-ancestors.
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        # WHY Permissions-Policy: deny powerful features (camera/mic/geo) to every origin.
        self.assertIn("camera=()", resp.headers.get("Permissions-Policy", ""))


class MetricsTests(TestCase):
    def test_metrics_open_and_prometheus_format(self):
        # WHY: Prometheus scrapes /metrics unauthenticated over the edge net (public access
        # is blocked at the tunnel), so it must serve without login in Prometheus text format.
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"# HELP", resp.content)
        self.assertIn(b"django_http_requests", resp.content)


class HealthTests(TestCase):
    """Liveness/readiness probes (infra-gap-analysis.md #5). JWKS reachability is patched at the
    seam so the suite stays hermetic (no live Keycloak in CI), mirroring decode_keycloak_token."""

    def test_healthz_is_open_and_cheap(self):
        # WHY: the CF Load Balancer health-checks this unauthenticated. It must serve without a
        # login and never depend on a backing service — a DB blip mustn't pull a live box.
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok\n")

    def test_readyz_ok_when_dependencies_up(self):
        # WHY: readiness is only meaningful if it actually probes dependencies — here DB (real,
        # the test SQLite) + Keycloak JWKS (patched up). All green ⇒ 200.
        with patch("web.health.jwks_reachable", return_value=True):
            resp = self.client.get("/readyz")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["checks"], {"database": True, "keycloak_jwks": True})

    def test_readyz_503_when_a_dependency_is_down(self):
        # WHY: a degraded dependency must surface as 503 (not a misleading 200), so deploy/incident
        # tooling and any readiness gate can tell "up" from "ready". The failing check is named.
        with patch("web.health.jwks_reachable", return_value=False):
            resp = self.client.get("/readyz")
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body["status"], "unavailable")
        self.assertFalse(body["checks"]["keycloak_jwks"])


class TracingInstrumentationTests(TestCase):
    """OTel instrumentation (infra-gap-analysis.md #7, ADR 0015) must emit spans for the surfaces #7
    cares about — an ASGI web request and an outbound httpx call (our external fan-out to OpenRouter
    / web-search / fal.ai). In-memory exporter keeps it hermetic; the real export path (OTLP → Alloy
    → Tempo) is proven by the deploy. WHY ASGI specifically: we serve on uvicorn, and the Django
    instrumentation only traces the WSGI path — so a Django-test-client span would be a false
    positive. We drive the actual ASGI server middleware instead."""

    def setUp(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def test_asgi_request_emits_a_server_span(self):
        import asyncio

        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        wrapped = OpenTelemetryMiddleware(inner, tracer_provider=self.provider)

        async def drive():
            scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": [],
                     "scheme": "http", "server": ("localhost", 8000), "query_string": b""}
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(_):
                pass
            await wrapped(scope, receive, send)

        asyncio.run(drive())
        kinds = [s.kind.name for s in self.exporter.get_finished_spans()]
        # WHY: no SERVER span on the ASGI path ⇒ requests aren't traced at all — the point of #7,
        # and the exact bug a Django-test-client test would have masked.
        self.assertIn("SERVER", kinds)

    def test_instrument_asgi_is_a_noop_without_endpoint(self):
        import os

        from keygrip.tracing import instrument_asgi

        sentinel = object()
        saved = os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        try:
            # WHY: tracing must be inert in dev/CI with no collector — same guard as SENTRY_DSN. The
            # app is returned unwrapped, so there's zero overhead and nothing to export.
            self.assertIs(instrument_asgi(sentinel), sentinel)
        finally:
            if saved is not None:
                os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = saved

    def test_httpx_call_emits_a_client_span(self):
        import httpx
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        # MockTransport keeps it hermetic (no real network). instrument_client wraps THIS client's
        # transport directly — unlike the global instrument(), which only patches httpx.HTTPTransport.
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
        with httpx.Client(transport=transport) as client:
            HTTPXClientInstrumentor.instrument_client(client, tracer_provider=self.provider)
            client.get("http://upstream.test/v1/thing")
        kinds = [s.kind.name for s in self.exporter.get_finished_spans()]
        # WHY: external-call spans are how a slow OpenRouter/fal.ai dependency becomes visible in a
        # request's trace instead of an unexplained latency gap.
        self.assertIn("CLIENT", kinds)
