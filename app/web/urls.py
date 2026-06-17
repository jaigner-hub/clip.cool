from django.urls import path

from . import health, views
from .auth import KeycloakPasswordChangeView

urlpatterns = [
    # Infra probes (no trailing slash, like /metrics): liveness for the CF LB origin health-check,
    # readiness for deploys/incidents (infra-gap-analysis.md #5).
    path("healthz", health.healthz, name="healthz"),
    path("readyz", health.readyz, name="readyz"),
    path("", views.home, name="home"),
    path("projects/new/", views.project_create, name="project_create"),
    path("projects/<int:project_id>/delete/", views.project_delete, name="project_delete"),
    path("account/password/", KeycloakPasswordChangeView.as_view(), name="account_password"),
    path("settings/api-credentials/", views.api_credentials, name="api_credentials"),
    path("settings/api-credentials/create/", views.api_credentials_create, name="api_credentials_create"),
    path("settings/api-credentials/<int:pk>/rotate/", views.api_credentials_rotate, name="api_credentials_rotate"),
    path("settings/api-credentials/<int:pk>/delete/", views.api_credentials_delete, name="api_credentials_delete"),
    path("lab/", views.lab, name="lab"),
    path("lab/ping/", views.lab_ping, name="lab_ping"),
]
