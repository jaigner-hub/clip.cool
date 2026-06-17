"""Staff impersonation (django-hijack, ADR 0010): superuser-only, scoping follows the
impersonated user, app-side audit, banner. WHY: impersonation is account-takeover — it must
be gated to superusers and every start/stop recorded.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tenancy.models import Organization, OrganizationMembership, Project
from web.models import ImpersonationEvent

User = get_user_model()


class ImpersonationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Acme", slug="acme")
        cls.member = User.objects.create_user("m@acme.com", email="m@acme.com")
        OrganizationMembership.objects.create(organization=cls.org, user=cls.member)
        cls.project = Project.objects.create(organization=cls.org, name="A1", slug="a1")
        cls.staff = User.objects.create_superuser("root@keygrip.ai", email="root@keygrip.ai")

    def _acquire(self, pk):
        return self.client.post(reverse("hijack:acquire"), {"user_pk": pk})

    def test_superuser_can_impersonate_and_scoping_follows(self):
        self.client.force_login(self.staff)
        self._acquire(self.member.pk)
        resp = self.client.get("/")
        # WHY: while impersonating, the app must scope to the IMPERSONATED user's org.
        self.assertContains(resp, "Acme")
        self.assertContains(resp, "A1")
        self.assertContains(resp, "Impersonating")  # banner shown
        self.assertTrue(
            ImpersonationEvent.objects.filter(
                impersonator=self.staff, impersonated=self.member, kind="start"
            ).exists()
        )

    def test_release_restores_and_audits(self):
        self.client.force_login(self.staff)
        self._acquire(self.member.pk)
        self.client.post(reverse("hijack:release"))
        resp = self.client.get("/")
        self.assertNotContains(resp, "Impersonating")  # banner gone
        self.assertTrue(
            ImpersonationEvent.objects.filter(
                impersonator=self.staff, impersonated=self.member, kind="stop"
            ).exists()
        )

    def test_non_superuser_cannot_impersonate(self):
        # WHY: only platform superusers may impersonate (not org members/admins).
        self.client.force_login(self.member)
        other = User.objects.create_user("x@acme.com", email="x@acme.com")
        resp = self._acquire(other.pk)
        self.assertIn(resp.status_code, (403, 302))  # denied (raise_exception -> 403)
        self.assertFalse(ImpersonationEvent.objects.filter(impersonated=other).exists())
