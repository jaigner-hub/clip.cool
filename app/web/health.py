"""Liveness + readiness probes (infra-gap-analysis.md #5).

Both are unauthenticated and carry no tenant/org logic:

- /healthz — liveness. Cheap, no I/O: "is the process up and serving?". The Cloudflare Load
  Balancer points its origin health-check here — a lightweight target beats an authenticated page.
- /readyz  — readiness. Confirms the dependencies a real request needs are reachable: the database
  and Keycloak's JWKS (bearer-token validation). 200 when all pass, 503 otherwise, with a per-check
  JSON body for a human reading it during a deploy/incident. Not the LB's frequent target, so the
  live JWKS fetch it does is fine here (healthz carries the high-frequency load).
"""
import logging

from django.db import connection
from django.http import HttpResponse, JsonResponse

from .api_auth import jwks_reachable

logger = logging.getLogger(__name__)


def healthz(request):
    """Liveness: the process is up. No DB, no external calls — never let a dependency blip take a
    healthy box out of the LB pool."""
    return HttpResponse("ok\n", content_type="text/plain")


def readyz(request):
    """Readiness: DB + Keycloak JWKS reachable. 503 if any dependency is down."""
    checks = {"database": _db_ok(), "keycloak_jwks": jwks_reachable()}
    ok = all(checks.values())
    return JsonResponse(
        {"status": "ok" if ok else "unavailable", "checks": checks},
        status=200 if ok else 503,
    )


def _db_ok():
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        # warning, not error: a transient DB blip is operational, not a code bug.
        logger.warning("readyz: database check failed", exc_info=True)
        return False
