from django.urls import path

from . import health, views
from .auth import KeycloakPasswordChangeView

urlpatterns = [
    # Infra probes (no trailing slash, like /metrics): liveness for the CF LB origin health-check,
    # readiness for deploys/incidents (infra-gap-analysis.md #5).
    path("healthz", health.healthz, name="healthz"),
    path("readyz", health.readyz, name="readyz"),
    # Root "/" is the clips search surface (served directly by clips.urls) — no redirect.
    path("account/password/", KeycloakPasswordChangeView.as_view(), name="account_password"),
    path("lab/", views.lab, name="lab"),
    path("lab/ping/", views.lab_ping, name="lab_ping"),
]
