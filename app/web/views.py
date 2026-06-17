import logging

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from tenancy import services
from tenancy.models import ServiceAccount

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
    """Land the user in their org. Every login auto-provisions a personal org they OWN
    (ensure_personal_org, ADR 0009 amendment), so a missing org is now the exception — a
    defensive fallback for accounts whose membership was removed/deactivated by staff."""
    # truthiness, not `is None`: request.organization is a SimpleLazyObject that *resolves*
    # to None — it is never the None singleton itself.
    if not request.organization and not request.user.is_superuser:
        return render(request, "no_access.html")
    projects = services.list_projects_for(request.user)
    # First-run gate: a member with an org but zero projects names their first project instead of
    # landing on a dead-end empty page. Superusers see ALL orgs, so they're never gated here.
    if request.organization and not request.user.is_superuser and not projects:
        return redirect("project_create")
    return render(request, "home.html", {"projects": projects, "active_page": "home"})


@login_required
def project_create(request):
    """Self-serve project creation, and the first-run gate `home` redirects new members to.
    Acts on the caller's own org (superuser-first via is_org_member). GET shows the form; POST
    creates the project and lands the user back on home with it."""
    org = request.organization
    if not org:
        # No org to create in: orphaned member → waiting room; org-less superuser → just home.
        return render(request, "no_access.html") if not request.user.is_superuser else redirect("home")
    if not services.is_org_member(request.user, org):
        return HttpResponseForbidden("You must belong to an organization.")
    error = None
    if request.method == "POST":
        try:
            services.create_project(org, request.POST.get("name", ""))
        except ValueError:
            error = "Enter a name for your project."
        else:
            return redirect("home")
    first_run = not services.list_projects_for(request.user)
    return render(
        request,
        "project_create.html",
        {"active_page": "home", "error": error, "first_run": first_run},
    )


@login_required
def project_delete(request, project_id):
    """Archive a project (soft-delete) — owner/admin only, superuser-first. GET shows a confirm
    page; POST archives it and lands back on home. Org-scoped via get_project_for_actor, so you can
    only ever touch your own org's projects (a superuser can reach any). The JSON-API twin is
    DELETE /api/v1/projects/{id}; both go through services.archive_project, so behaviour matches."""
    project = services.get_project_for_actor(request.user, project_id)
    if project is None:
        raise Http404("Project not found.")
    if not services.is_org_admin(request.user, project.organization):
        return HttpResponseForbidden("Only owners and admins can delete projects.")
    if request.method == "POST":
        services.archive_project(project)
        return redirect("home")
    return render(request, "project_delete.html", {"project": project, "active_page": "home"})


def _require_creds_access(request):
    """Return the caller's org if they may manage its API credentials, else None.

    Self-serve credentials are open to any org **member** (not just owners/admins) — every member
    can mint/rotate/delete their org's API clients. The page always acts on the caller's own org,
    so this is effectively "the user has an org" (superuser-first via is_org_member)."""
    org = request.organization
    if org and services.is_org_member(request.user, org):
        return org
    return None


def _creds_context(org, *, new_secret=None, new_client_id=None, error=None):
    return {
        "active_page": "api_credentials",
        "service_accounts": services.list_service_accounts(org),
        "new_secret": new_secret,       # shown ONCE, never stored
        "new_client_id": new_client_id,
        "token_url": settings.OIDC_OP_TOKEN_ENDPOINT,  # for the copy-paste curl quickstart
        "error": error,
    }


def _get_org_service_account(org, pk):
    """Return (sa, gone): the org's service account for pk. Raises Http404 for another org's
    pk (don't reveal it exists); returns (None, True) if it's simply already deleted."""
    sa = ServiceAccount.objects.filter(pk=pk).first()
    if sa is None:
        return None, True
    if sa.organization_id != org.id:
        raise Http404
    return sa, False


@login_required
def api_credentials(request):
    """Self-serve API credentials for any org member (ADR 0011)."""
    org = _require_creds_access(request)
    if org is None:
        return HttpResponseForbidden("You must belong to an organization.")
    return render(request, "api_credentials.html", _creds_context(org))


@login_required
@require_POST
def api_credentials_create(request):
    org = _require_creds_access(request)
    if org is None:
        return HttpResponseForbidden("You must belong to an organization.")
    label = request.POST.get("label", "").strip()
    try:
        sa, secret = services.create_service_account(org, label, created_by=request.user)
    except Exception:
        logger.error("Service-account creation failed for org %s", org.slug, exc_info=True)
        return render(request, "api_credentials.html",
                      _creds_context(org, error="Could not create the credential. Try again."))
    return render(request, "api_credentials.html",
                  _creds_context(org, new_secret=secret, new_client_id=sa.client_id))


@login_required
@require_POST
def api_credentials_rotate(request, pk):
    org = _require_creds_access(request)
    if org is None:
        return HttpResponseForbidden("You must belong to an organization.")
    sa, gone = _get_org_service_account(org, pk)
    if gone:
        return render(request, "api_credentials.html",
                      _creds_context(org, error="That credential no longer exists."))
    try:
        secret = services.rotate_service_account_secret(sa)
    except Exception:
        logger.error("Secret rotation failed for %s", sa.client_id, exc_info=True)
        return render(request, "api_credentials.html",
                      _creds_context(org, error="Could not rotate the secret. Try again."))
    return render(request, "api_credentials.html",
                  _creds_context(org, new_secret=secret, new_client_id=sa.client_id))


@login_required
@require_POST
def api_credentials_delete(request, pk):
    org = _require_creds_access(request)
    if org is None:
        return HttpResponseForbidden("You must belong to an organization.")
    sa, gone = _get_org_service_account(org, pk)
    if gone:
        return redirect("api_credentials")  # already deleted — idempotent, no 404
    try:
        services.delete_service_account(sa)
    except Exception:
        logger.error("Service-account deletion failed for %s", sa.client_id, exc_info=True)
        return render(request, "api_credentials.html",
                      _creds_context(org, error="Could not delete the credential. Try again."))
    return redirect("api_credentials")


@login_required
def lab(request):
    """CSP smoke-test page — HTMX swap, Alpine (CSP build), nonce'd inline script."""
    return render(request, "lab.html")


@login_required
def lab_ping(request):
    """HTMX fragment target."""
    return HttpResponse(f"<strong>pong</strong> · {timezone.now():%H:%M:%S} UTC")
