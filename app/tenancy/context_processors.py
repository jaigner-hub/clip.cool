"""Expose org-capability flags to templates so the nav can show org links without each view
passing them. `is_org_member` gates self-serve features open to any member (API credentials);
`is_org_admin` stays available for owner/admin-only surfaces."""
from . import services


def org_admin(request):
    user = getattr(request, "user", None)
    org = getattr(request, "organization", None)
    authed = bool(org and user and getattr(user, "is_authenticated", False))
    return {
        "is_org_member": authed and services.is_org_member(user, org),
        "is_org_admin": authed and services.is_org_admin(user, org),
    }
