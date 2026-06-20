from django.contrib import admin
from django.templatetags.static import static
from django.urls import include, path
from django.views.generic import TemplateView
from django.views.generic.base import RedirectView

from keygrip.api import api as ninja_api
from web import views as web_views

# Brand the Django admin (lightweight; see web/static/web/admin.css + templates/admin/).
admin.site.site_header = "Keygrip admin"
admin.site.site_title = "Keygrip admin"
admin.site.index_title = "Administration"

urlpatterns = [
    # Bare /favicon.ico (browsers + link unfurlers request the root path) → the collected static file.
    path("favicon.ico", RedirectView.as_view(url=static("web/favicon.ico"), permanent=True)),
    # Shadows admin's own login URL (first match wins): the password form only exists on
    # break-glass hosts; everywhere else staff go through OIDC (ADR 0002).
    path("admin/login/", web_views.admin_login_gate),
    path("admin/", admin.site.urls),
    path("oidc/", include("mozilla_django_oidc.urls")),
    path("hijack/", include("hijack.urls")),  # staff impersonation start/release (ADR 0010)
    # Swagger's OAuth2 redirect handler — must be listed BEFORE the ninja mount (which would
    # otherwise swallow everything under /api/v1/). Covered by the /api/v1/docs relaxed CSP.
    path(
        "api/v1/docs/oauth2-redirect.html",
        TemplateView.as_view(template_name="oauth2-redirect.html"),
    ),
    path("api/v1/", ninja_api.urls),  # JSON API + Swagger docs at /api/v1/docs (ADR 0011)
    # /metrics — Prometheus scrapes it over the edge net; blocked publicly at the tunnel.
    path("", include("django_prometheus.urls")),
    path("", include("clips.urls")),
    path("", include("web.urls")),
]
