"""Service layer for tenancy — views (HTML or JSON) are thin adapters over this.

Logic for "which org is this user in" and "what can they see" lives here, scoped
explicitly (no thread-local 'current tenant' magic). Superuser-first throughout.
"""
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from .models import Organization, OrganizationMembership, Project, ServiceAccount


class ServiceAccountPrincipal:
    """API principal for a machine client (no Django user). Strictly org-scoped — a service
    account is never a superuser, so it can only ever see its own org."""

    is_service_account = True
    is_superuser = False
    is_authenticated = True

    def __init__(self, organization, client_id):
        self.organization = organization
        self.client_id = client_id

    def __str__(self):
        return f"service:{self.client_id}"


def get_service_account(client_id):
    """Resolve an active ServiceAccount (Keycloak client_id → org), or None."""
    if not client_id:
        return None
    return (
        ServiceAccount.objects.select_related("organization")
        .filter(client_id=client_id, is_active=True, organization__is_active=True)
        .first()
    )


def get_membership(user):
    """The user's single org membership, or None."""
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        OrganizationMembership.objects.select_related("organization")
        .filter(user=user)
        .first()
    )


def get_user_org(user):
    """The org the user belongs to, or None (no membership yet → 'access pending')."""
    membership = get_membership(user)
    return membership.organization if membership else None


def list_projects_for(actor):
    """ACTIVE projects an actor may see — a Django user (their org, or all for a superuser) or a
    ServiceAccountPrincipal (strictly its mapped org). Archived (soft-deleted) projects are hidden;
    restoring is is_active=True again (staff/admin)."""
    if getattr(actor, "is_service_account", False):
        return Project.objects.for_org(actor.organization).active()
    return Project.objects.for_user(actor).active()


def get_project_for_actor(actor, project_id):
    """The project `project_id` this actor may act on: ANY project for a superuser; otherwise one in
    the actor's own org. None if not found / out of scope — that org filter IS the tenant-isolation
    boundary for project-scoped writes. Includes archived projects (delete is idempotent)."""
    if getattr(actor, "is_superuser", False):
        qs = Project.objects.all()
    else:
        org = org_for_actor(actor)
        if org is None:
            return None
        qs = Project.objects.for_org(org)
    return qs.filter(pk=project_id).first()


def archive_project(project):
    """Soft-delete: mark a project inactive so it drops out of listings. Reversible — the row and
    all its content stay; an admin can flip is_active back. Idempotent. Authorization (owner/admin)
    is the caller's job, mirroring create_project — the service just does the state change."""
    if project.is_active:
        project.is_active = False
        project.save(update_fields=["is_active"])
    return project


def org_for_actor(actor):
    """The single org an API actor *writes* within: a service account's mapped org, or a user's
    membership org (None if they somehow have none). Unlike list_projects_for, a superuser gets
    their own membership org here, not 'all orgs' — a create has to land in exactly one org."""
    if getattr(actor, "is_service_account", False):
        return actor.organization
    return get_user_org(actor)


# --- write operations (staff onboarding; ADR 0009 — assignment is staff-driven for now) ---

def create_organization(name, *, slug=None, is_active=True):
    return Organization.objects.create(name=name, slug=slug or "", is_active=is_active)


def add_member(organization, user, role=OrganizationMembership.Role.MEMBER):
    return OrganizationMembership.objects.create(
        organization=organization, user=user, role=role
    )


def _unique_project_slug(organization, base):
    """A slug unique *within* `organization` (Projects are unique per-org, not globally — see
    the unique_project_slug_per_org constraint)."""
    base = slugify(base) or "project"
    slug, n = base, 2
    while Project.objects.filter(organization=organization, slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_project(organization, name, *, slug=None):
    """Create a project in `organization`. Name is required; the slug is derived from it and made
    unique within the org. Race-safe: if a concurrent create grabs the same slug first we recompute
    and retry once (mirrors ensure_personal_org's IntegrityError handling). Raises ValueError on an
    empty name (callers — the JSON API + the web form — turn that into a 400)."""
    name = (name or "").strip()[:200]
    if not name:
        raise ValueError("Project name is required.")
    for _ in range(3):
        candidate = slug or _unique_project_slug(organization, name)
        try:
            with transaction.atomic():
                return Project.objects.create(
                    organization=organization, name=name, slug=candidate
                )
        except IntegrityError:
            if slug:  # caller pinned the slug — don't silently mutate it; surface the clash
                raise
    raise IntegrityError("could not allocate a unique project slug")


# --- onboarding invariant: every user belongs to an org (ADR 0009 amendment) ---

def _unique_org_slug(base):
    """A slug unique across Organizations, derived from `base` (e.g. the user's email)."""
    base = slugify(base) or "org"
    slug, n = base, 2
    while Organization.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def ensure_personal_org(user):
    """Guarantee `user` belongs to an org: if they have none, create a personal org they OWN.

    Enforces the "no user without an org" invariant (ADR 0009 amendment), so org-scoped surfaces
    (e.g. API credentials) always resolve. **Idempotent** — a no-op once the user is a member of
    any org, so staff renames/reassignments are never undone or duplicated. Returns the membership
    (or None for an anonymous/unsaved user). The personal org is a normal org: staff can rename it
    or reassign the user elsewhere afterwards.
    """
    if not getattr(user, "is_authenticated", False) or not getattr(user, "pk", None):
        return None
    existing = get_membership(user)
    if existing:
        return existing
    email = getattr(user, "email", "") or user.get_username()
    name = (user.get_full_name() or email).strip() or email
    try:
        with transaction.atomic():
            org = create_organization(name, slug=_unique_org_slug(email))
            return add_member(org, user, role=OrganizationMembership.Role.OWNER)
    except IntegrityError:
        # Lost a race against a concurrent login (one_org_per_user); the other side won.
        return get_membership(user)


# --- self-serve API credentials (service accounts; ADR 0011) ---

def is_org_admin(user, organization):
    """Org owners/admins (or platform superusers) may manage API credentials + delete projects."""
    if getattr(user, "is_superuser", False):
        return True
    # Machine clients carry no role and are never admins; short-circuit before the membership lookup
    # (a ServiceAccountPrincipal isn't a Django user, so get_membership wouldn't apply to it).
    if getattr(user, "is_service_account", False):
        return False
    m = get_membership(user)
    return bool(
        m and organization and m.organization_id == organization.id
        and m.role in {OrganizationMembership.Role.OWNER, OrganizationMembership.Role.ADMIN}
    )


def is_org_member(user, organization):
    """Any member of `organization` (or a platform superuser). Used for self-serve features every
    member may use — e.g. API credentials. Broader than is_org_admin (owner/admin-only)."""
    if getattr(user, "is_superuser", False):
        return True
    m = get_membership(user)
    return bool(m and organization and m.organization_id == organization.id)


def list_service_accounts(organization):
    return ServiceAccount.objects.filter(organization=organization)


def create_service_account(organization, label, *, created_by=None):
    """Create the Keycloak client + the local mapping. Returns (service_account, secret).
    The secret is returned ONCE (never stored) — Keycloak holds it."""
    import secrets as _secrets

    from . import keycloak_admin

    client_id = f"kg-{organization.slug}-{_secrets.token_hex(4)}"
    kc_id, secret = keycloak_admin.create_service_account_client(client_id, label or client_id)
    sa = ServiceAccount.objects.create(
        organization=organization, client_id=client_id, label=label or "",
        keycloak_id=kc_id, created_by=created_by,
    )
    return sa, secret


def rotate_service_account_secret(service_account):
    """Generate a new secret in Keycloak; returns it ONCE."""
    from . import keycloak_admin

    return keycloak_admin.rotate_client_secret(service_account.keycloak_id)


def delete_service_account(service_account):
    from . import keycloak_admin

    if service_account.keycloak_id:
        keycloak_admin.delete_client(service_account.keycloak_id)
    service_account.delete()
