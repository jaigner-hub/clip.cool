"""Tenant-isolation is the load-bearing guarantee of a multi-tenant CMS; cross-org
leakage is the classic failure. These encode the standing "two orgs, no leak" pattern
(open-gap #4) — every tenant-scoped query is checked against a second org's data.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from tenancy.models import Organization, OrganizationMembership, Project

User = get_user_model()


class TenantIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(name="Acme", slug="acme")
        cls.org_b = Organization.objects.create(name="Globex", slug="globex")
        cls.user_a = User.objects.create_user("a@acme.com", email="a@acme.com")
        cls.user_b = User.objects.create_user("b@globex.com", email="b@globex.com")
        OrganizationMembership.objects.create(organization=cls.org_a, user=cls.user_a)
        OrganizationMembership.objects.create(organization=cls.org_b, user=cls.user_b)
        cls.proj_a = Project.objects.create(organization=cls.org_a, name="A1", slug="a1")
        cls.proj_b = Project.objects.create(organization=cls.org_b, name="B1", slug="b1")

    def test_user_sees_only_their_org_projects(self):
        # WHY: the whole point — A must never see Globex's data and vice versa.
        self.assertEqual(list(Project.objects.for_user(self.user_a)), [self.proj_a])
        self.assertEqual(list(Project.objects.for_user(self.user_b)), [self.proj_b])

    def test_user_without_membership_sees_nothing(self):
        # WHY: no membership ⇒ no org ⇒ no data (fail closed, not open).
        orphan = User.objects.create_user("x@nowhere.com", email="x@nowhere.com")
        self.assertEqual(list(Project.objects.for_user(orphan)), [])

    def test_anonymous_sees_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(list(Project.objects.for_user(AnonymousUser())), [])

    def test_superuser_sees_all(self):
        # WHY: superuser-first — staff/admin tooling is the sanctioned cross-org reach.
        su = User.objects.create_superuser("root@keygrip.ai", email="root@keygrip.ai")
        self.assertCountEqual(
            Project.objects.for_user(su), [self.proj_a, self.proj_b]
        )

    def test_one_org_per_user_enforced(self):
        # WHY: the single-org invariant (ADR 0009); a second membership must be rejected
        # at the DB so relaxing later is a deliberate constraint drop, not silent drift.
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.create(
                organization=self.org_b, user=self.user_a
            )


class LandingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Acme", slug="acme")
        cls.member = User.objects.create_user("m@acme.com", email="m@acme.com")
        OrganizationMembership.objects.create(organization=cls.org, user=cls.member)
        cls.orphan = User.objects.create_user("o@nowhere.com", email="o@nowhere.com")

    def test_member_with_no_projects_is_gated_to_create_one(self):
        # WHY: first-run gate (replaces the old "land on an empty page") — a member with an org
        # but zero projects must name their first project, not hit a dead-end empty state.
        self.client.force_login(self.member)
        resp = self.client.get(reverse("home"))
        self.assertRedirects(resp, reverse("project_create"))

    def test_member_with_a_project_lands_on_home(self):
        # WHY: once they have a project the gate releases and they land on their project list.
        Project.objects.create(organization=self.org, name="Site", slug="site")
        self.client.force_login(self.member)
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme")  # org chrome
        self.assertContains(resp, "Site")  # their project

    def test_member_does_not_see_other_orgs_project(self):
        # WHY: cross-org leak check at the VIEW layer, not just the queryset.
        Project.objects.create(organization=self.org, name="Mine", slug="mine")  # un-gate them
        other = Organization.objects.create(name="Globex", slug="globex")
        Project.objects.create(organization=other, name="TopSecret", slug="topsecret")
        self.client.force_login(self.member)
        resp = self.client.get(reverse("home"))
        self.assertNotContains(resp, "TopSecret")

    def test_orphan_sees_access_pending(self):
        # WHY: a user with no org isn't an error — it's the staff-onboarding waiting room.
        self.client.force_login(self.orphan)
        resp = self.client.get(reverse("home"))
        self.assertContains(resp, "Access pending")

    def test_base_template_does_not_leak_comments(self):
        # WHY: a multi-line {# #} is NOT a valid Django comment and leaks as visible page
        # text (caught live 2026-06-07). The base shell's nonce note must never render.
        Project.objects.create(organization=self.org, name="Site", slug="site")  # so home renders
        self.client.force_login(self.member)
        resp = self.client.get(reverse("home"))
        self.assertNotContains(resp, "Frontend baseline")
        self.assertNotContains(resp, "{#")


class ProjectCreateViewTests(TestCase):
    """The self-serve create flow + first-run gate (web layer). The JSON-API twin is in
    test_api.py; both go through services.create_project, so isolation matches."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Acme", slug="acme")
        cls.member = User.objects.create_user("m@acme.com", email="m@acme.com")
        OrganizationMembership.objects.create(organization=cls.org, user=cls.member)
        cls.orphan = User.objects.create_user("o@nowhere.com", email="o@nowhere.com")

    def test_get_shows_the_form(self):
        self.client.force_login(self.member)
        resp = self.client.get(reverse("project_create"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Create project")

    def test_post_creates_project_in_own_org_and_redirects_home(self):
        # WHY: the happy path — naming a project lands it in the caller's org and releases the gate.
        self.client.force_login(self.member)
        resp = self.client.post(reverse("project_create"), {"name": "Marketing site"})
        self.assertRedirects(resp, reverse("home"))
        proj = Project.objects.get(organization=self.org, name="Marketing site")
        self.assertEqual(proj.slug, "marketing-site")  # slug derived from the name

    def test_post_empty_name_reprompts_not_creates(self):
        # WHY: an empty name is a user error, not a 500 — re-show the form, create nothing.
        self.client.force_login(self.member)
        resp = self.client.post(reverse("project_create"), {"name": "   "})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enter a name")
        self.assertEqual(Project.objects.filter(organization=self.org).count(), 0)

    def test_orphan_cannot_create(self):
        # WHY: no org ⇒ nowhere to create; the orphan sees the waiting room, not a form.
        self.client.force_login(self.orphan)
        resp = self.client.post(reverse("project_create"), {"name": "Sneaky"})
        self.assertContains(resp, "Access pending")
        self.assertFalse(Project.objects.filter(name="Sneaky").exists())


class ProjectDeleteViewTests(TestCase):
    """The archive (soft-delete) confirm flow (web layer). Owner/admin only, superuser-first; the
    JSON-API twin is in test_api.py. Both go through services.archive_project."""

    Role = OrganizationMembership.Role

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Acme", slug="acme")
        cls.admin = User.objects.create_user("admin@acme.com", email="admin@acme.com")
        OrganizationMembership.objects.create(organization=cls.org, user=cls.admin, role=cls.Role.ADMIN)
        cls.member = User.objects.create_user("m@acme.com", email="m@acme.com")
        OrganizationMembership.objects.create(organization=cls.org, user=cls.member, role=cls.Role.MEMBER)
        cls.org_b = Organization.objects.create(name="Globex", slug="globex")
        cls.proj = Project.objects.create(organization=cls.org, name="Marketing", slug="marketing")
        # A second project so archiving the first doesn't empty the org and trip home's first-run gate.
        cls.proj2 = Project.objects.create(organization=cls.org, name="Blog", slug="blog")
        cls.proj_b = Project.objects.create(organization=cls.org_b, name="Other", slug="other")

    def _url(self, project):
        return reverse("project_delete", args=[project.id])

    def test_admin_get_shows_confirmation(self):
        self.client.force_login(self.admin)
        resp = self.client.get(self._url(self.proj))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Delete project")
        self.assertContains(resp, "Marketing")

    def test_admin_post_archives_and_redirects_home(self):
        # WHY: the happy path — confirming archives the project (is_active False) and lands home.
        self.client.force_login(self.admin)
        resp = self.client.post(self._url(self.proj))
        self.assertRedirects(resp, reverse("home"))
        self.proj.refresh_from_db()
        self.assertFalse(self.proj.is_active)

    def test_archived_project_disappears_from_home(self):
        # WHY: soft-delete must actually hide it — the listing filters .active().
        self.client.force_login(self.admin)
        self.client.post(self._url(self.proj))
        resp = self.client.get(reverse("home"))
        self.assertNotContains(resp, "Marketing")

    def test_member_is_forbidden_and_project_survives(self):
        # WHY: archiving a whole project is owner/admin-only — a plain member can't, even via POST.
        self.client.force_login(self.member)
        resp = self.client.post(self._url(self.proj))
        self.assertEqual(resp.status_code, 403)
        self.proj.refresh_from_db()
        self.assertTrue(self.proj.is_active)

    def test_cross_org_delete_is_404(self):
        # WHY: an admin of one org can't even reach another org's project — 404, no leak.
        self.client.force_login(self.admin)
        resp = self.client.post(self._url(self.proj_b))
        self.assertEqual(resp.status_code, 404)
        self.proj_b.refresh_from_db()
        self.assertTrue(self.proj_b.is_active)

    def test_delete_button_shown_to_admin_only(self):
        # WHY: the UI must not dangle a delete affordance in front of a member who'll only get a 403.
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("home")), self._url(self.proj))
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse("home")), self._url(self.proj))


class CreateProjectServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(name="Acme", slug="acme")
        cls.org_b = Organization.objects.create(name="Globex", slug="globex")

    def test_slug_is_unique_within_org(self):
        # WHY: two same-named projects in one org must not collide on unique_project_slug_per_org.
        from tenancy.services import create_project
        p1 = create_project(self.org_a, "Launch")
        p2 = create_project(self.org_a, "Launch")
        self.assertEqual(p1.slug, "launch")
        self.assertNotEqual(p1.slug, p2.slug)

    def test_same_slug_allowed_across_orgs(self):
        # WHY: the constraint is per-org — each org has its own "launch".
        from tenancy.services import create_project
        a = create_project(self.org_a, "Launch")
        b = create_project(self.org_b, "Launch")
        self.assertEqual(a.slug, b.slug, "launch")

    def test_empty_name_raises(self):
        from tenancy.services import create_project
        with self.assertRaises(ValueError):
            create_project(self.org_a, "   ")


class EnsurePersonalOrgTests(TestCase):
    """Invariant (ADR 0009 amendment): every user belongs to an org. A sole/new user OWNS a
    personal org, so org-scoped surfaces (e.g. API credentials) always resolve. Idempotent, so
    staff renames/reassignments are never undone."""

    def test_orgless_user_gets_personal_owner_org(self):
        # WHY: this is the brad bug — a user with no membership couldn't reach org-scoped pages.
        from tenancy.services import ensure_personal_org, is_org_admin
        u = User.objects.create_user("brad@keygrip.ai", email="brad@keygrip.ai")
        m = ensure_personal_org(u)
        self.assertIsNotNone(m)
        self.assertEqual(m.user, u)
        self.assertEqual(m.role, OrganizationMembership.Role.OWNER)
        # Owner ⇒ may manage their own org's credentials.
        self.assertTrue(is_org_admin(u, m.organization))

    def test_idempotent_no_duplicate_org(self):
        # WHY: runs on EVERY login — a second call must not spawn a second org (and can't:
        # one_org_per_user would error). Staff edits to the existing membership must survive.
        from tenancy.services import ensure_personal_org
        u = User.objects.create_user("c@x.com", email="c@x.com")
        first = ensure_personal_org(u)
        before = Organization.objects.count()
        again = ensure_personal_org(u)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(Organization.objects.count(), before)

    def test_existing_member_untouched(self):
        # WHY: a user already placed in an org by staff (any role) must NOT get a new personal org.
        from tenancy.services import ensure_personal_org
        org = Organization.objects.create(name="Acme", slug="acme")
        u = User.objects.create_user("d@acme.com", email="d@acme.com")
        OrganizationMembership.objects.create(
            organization=org, user=u, role=OrganizationMembership.Role.MEMBER
        )
        m = ensure_personal_org(u)
        self.assertEqual(m.organization, org)
        self.assertEqual(m.role, OrganizationMembership.Role.MEMBER)  # unchanged
        self.assertEqual(Organization.objects.count(), 1)

    def test_unique_slug_for_same_display_name(self):
        # WHY: two users can share a name; the org slug is unique, so provisioning must not collide.
        from tenancy.services import ensure_personal_org
        u1 = User.objects.create_user("brad@a.com", email="brad@a.com", first_name="Brad", last_name="X")
        u2 = User.objects.create_user("brad@b.com", email="brad@b.com", first_name="Brad", last_name="X")
        s1 = ensure_personal_org(u1).organization.slug
        s2 = ensure_personal_org(u2).organization.slug
        self.assertNotEqual(s1, s2)
