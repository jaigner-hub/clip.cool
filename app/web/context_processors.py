"""Expose the staff Admin link target to templates.

The admin is same-origin (`/admin/`) in every environment — staff enter via their Keycloak SSO
session; only the break-glass *password* form is tailnet-gated (ADR 0002, web.views.
admin_login_gate). `ADMIN_URL` stays env-overridable for odd topologies; this makes it available
to every template.
"""
from django.conf import settings


def admin_link(request):
    return {"admin_url": settings.ADMIN_URL}


def instance_banner(request):
    """Expose the dev-instance label so base.html can render a "this is a dev instance" banner.

    None on prod (settings.base) ⇒ no banner; dev/mc instances set it to the instance name so a
    local tab is never mistaken for production.
    """
    return {"instance_label": settings.KG_INSTANCE_LABEL}


def seo(request):
    """Expose canonical-origin + search-console bits for base.html's <head>.

    `canonical_url` pins this page to settings.SITE_URL (not request host) so app.vent.dog and any
    other dual-served host don't index as duplicate content; the query string is dropped so /?q=...
    search permutations collapse to one canonical entry.
    """
    return {
        "site_url": settings.SITE_URL,
        "canonical_url": settings.SITE_URL + request.path,
        "google_site_verification": settings.GOOGLE_SITE_VERIFICATION,
    }
