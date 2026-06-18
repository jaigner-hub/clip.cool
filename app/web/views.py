import logging

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

logger = logging.getLogger(__name__)


def admin_login_gate(request):
    """Host-gated /admin/login/ (ADR 0002). WHY: /admin is served on the public app host like the
    rest of the app, but the local break-glass password must never be acceptable from the public
    internet. This is the only password-accepting endpoint in the app, so the gate IS the
    guardrail: on a break-glass host (the Tailscale Serve hostname) it is the stock admin login
    (password form, works when Keycloak is down); on any other host the form neither renders nor
    processes a POST — anonymous users are routed through the normal OIDC login instead, and land
    back in the admin via their SSO session.
    """
    # Hostname-only match (no port): Tailscale Serve doesn't reliably put :8447 in the Host header
    # (see USE_X_FORWARDED_PORT in settings/prod.py). Safe in this topology — a public request
    # can't spoof the tailnet hostname, because the tunnel routes by hostname and 404s unknown ones.
    allowed = {h.rsplit(":", 1)[0] for h in settings.BREAK_GLASS_LOGIN_HOSTS}
    if not allowed or request.get_host().rsplit(":", 1)[0] in allowed:
        return admin.site.login(request)
    next_url = request.GET.get(REDIRECT_FIELD_NAME, "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = reverse("admin:index")
    if request.user.is_authenticated:
        if request.user.is_staff:
            return redirect(next_url)
        return HttpResponseForbidden("Your account is not authorized for the admin.")
    return redirect_to_login(next_url, settings.LOGIN_URL, REDIRECT_FIELD_NAME)


@login_required
def home(request):
    """Root: send users to the clip search surface (the discovery front door)."""
    return redirect("clips_search")


@login_required
def lab(request):
    """CSP smoke-test page — HTMX swap, Alpine (CSP build), nonce'd inline script."""
    return render(request, "lab.html")


@login_required
def lab_ping(request):
    """HTMX fragment target."""
    return HttpResponse(f"<strong>pong</strong> · {timezone.now():%H:%M:%S} UTC")
