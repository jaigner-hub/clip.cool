"""Tenancy: a 2-tier `Organization › Project` model (ADR 0009).

Org is the tenant / isolation boundary; Project is the content scope (Phase 2 content
FKs to Project). Per-org roles live here in the DB (`OrganizationMembership`), NOT in
Keycloak — Keycloak owns identity + the platform tier only.
"""
from django.conf import settings
from django.db import models
from django.utils.text import slugify


class Organization(models.Model):
    """A tenant. The isolation boundary and (later) the billing unit."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class OrganizationMembership(models.Model):
    """Who belongs to an org, and their role within it.

    The `one_org_per_user` constraint enforces the single-org-per-user invariant
    (ADR 0009). Relaxing to multi-org later = dropping this constraint (additive),
    not a schema rewrite — which is exactly why membership is its own table.
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="org_membership"
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user"], name="one_org_per_user"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.organization} ({self.role})"


class ProjectQuerySet(models.QuerySet):
    """Tenant-scoped access. `for_user` is superuser-first — the one sanctioned
    cross-org reach (staff/admin tooling); everyone else is pinned to their org."""

    def for_org(self, organization):
        return self.filter(organization=organization)

    def active(self):
        """Exclude soft-deleted (archived) projects — see services.archive_project."""
        return self.filter(is_active=True)

    def for_user(self, user):
        if not getattr(user, "is_authenticated", False):
            return self.none()
        if user.is_superuser:
            return self
        from .services import get_user_org

        org = get_user_org(user)
        return self.for_org(org) if org is not None else self.none()


class Project(models.Model):
    """A workspace within an org. The unit content (Phase 2) scopes to."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="projects"
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="unique_project_slug_per_org"
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class ServiceAccount(models.Model):
    """Maps a Keycloak service-account client (machine-to-machine, ADR 0011) to an org.

    Keycloak owns the credential (client_id + secret); the org binding is OUR concern, so it
    lives here (ADR 0009: tenancy in Postgres, not Keycloak). A client-credentials token has
    no `email` — the API resolves the org from the token's `azp` (client_id) via this table.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="service_accounts"
    )
    client_id = models.CharField(max_length=255, unique=True)  # Keycloak client_id / token azp
    label = models.CharField(max_length=200, blank=True)
    keycloak_id = models.CharField(max_length=255, blank=True)  # Keycloak client UUID (rotate/delete)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="service_accounts_created",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["client_id"]

    def __str__(self):
        return f"{self.client_id} → {self.organization}"
