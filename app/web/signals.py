"""App audit receivers.

- Impersonation start/stop → ImpersonationEvent (ADR 0010); django-hijack fires these with
  (hijacker, hijacked, request) — hijacker is the real staff user, hijacked is the impersonated one.
- Break-glass login alerting (ADR 0002): any local-password (ModelBackend) login is, by policy,
  the single break-glass account. We log it at WARNING on the dedicated `web.security` channel so
  it reaches stdout → Loki, where the Loki ruler alerts → Alertmanager (email + ntfy push).
"""
import logging

from django.contrib.auth import BACKEND_SESSION_KEY
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from hijack.signals import hijack_ended, hijack_started

from .models import ImpersonationEvent

security_log = logging.getLogger("web.security")

# The grep marker the Loki alert rule keys on — keep in lockstep with the ruler rule
# (roles/observability/templates/loki-rules.yml.j2).
BREAK_GLASS_MARKER = "BREAK_GLASS_LOGIN"


@receiver(user_logged_in)
def _alert_on_break_glass_login(sender, request, user, **kwargs):
    """Fire the break-glass audit log on a local-password login.

    OIDC users carry an unusable password (web.auth), so they authenticate via the OIDC backend;
    only the break-glass account has a usable password and logs in via ModelBackend. We key off
    the backend recorded on the session — not the username — so a renamed/rotated account is still
    caught.
    """
    backend = request.session.get(BACKEND_SESSION_KEY, "") if request is not None else ""
    if not backend.endswith("ModelBackend"):
        return  # normal SSO/OIDC login — not break-glass
    ip = ""
    if request is not None:
        ip = request.META.get("HTTP_CF_CONNECTING_IP") or request.META.get("REMOTE_ADDR", "")
    security_log.warning(
        "%s user=%s ip=%s backend=%s", BREAK_GLASS_MARKER, user.get_username(), ip, backend
    )


@receiver(hijack_started)
def _on_hijack_started(sender, hijacker, hijacked, request, **kwargs):
    ImpersonationEvent.objects.create(
        impersonator=hijacker, impersonated=hijacked, kind=ImpersonationEvent.Kind.START
    )


@receiver(hijack_ended)
def _on_hijack_ended(sender, hijacker, hijacked, request, **kwargs):
    ImpersonationEvent.objects.create(
        impersonator=hijacker, impersonated=hijacked, kind=ImpersonationEvent.Kind.STOP
    )
