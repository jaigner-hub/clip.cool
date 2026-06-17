"""Self-serve API credentials (ADR 0011): any org member, secret shown once, org-isolated.
Keycloak Admin calls are mocked — hermetic; the real Admin API path is checked on deploy."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tenancy.models import Organization, OrganizationMembership, ServiceAccount

User = get_user_model()


class APICredentialsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Acme", slug="acme")
        cls.owner = User.objects.create_user("owner@acme.com", email="owner@acme.com")
        OrganizationMembership.objects.create(
            organization=cls.org, user=cls.owner, role=OrganizationMembership.Role.OWNER
        )
        cls.member = User.objects.create_user("member@acme.com", email="member@acme.com")
        OrganizationMembership.objects.create(
            organization=cls.org, user=cls.member, role=OrganizationMembership.Role.MEMBER
        )

    def test_member_can_view_and_manage(self):
        # WHY: self-serve credentials are open to ANY org member (not just owners/admins) — a
        # plain member both sees the page and can mint a credential for their org.
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("api_credentials")).status_code, 200)
        with patch("tenancy.keycloak_admin.create_service_account_client",
                   return_value=("kc-uuid-m", "memb3r")):
            resp = self.client.post(reverse("api_credentials_create"), {"label": "by-member"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "memb3r")
        self.assertEqual(ServiceAccount.objects.get(label="by-member").created_by, self.member)

    def test_non_member_cannot_access(self):
        # WHY: the gate is org MEMBERSHIP — a user with no membership (no org) is still blocked,
        # which is also why an org-less user must be provisioned (see EnsurePersonalOrgTests).
        outsider = User.objects.create_user("nobody@x.com", email="nobody@x.com")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("api_credentials")).status_code, 403)

    def test_owner_creates_and_secret_shown_once(self):
        self.client.force_login(self.owner)
        with patch("tenancy.keycloak_admin.create_service_account_client",
                   return_value=("kc-uuid-1", "s3cr3t")):
            resp = self.client.post(reverse("api_credentials_create"), {"label": "zapier"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "s3cr3t")  # shown once on create
        sa = ServiceAccount.objects.get(organization=self.org)
        self.assertEqual(sa.keycloak_id, "kc-uuid-1")
        self.assertTrue(sa.client_id.startswith("kg-acme-"))
        self.assertEqual(sa.created_by, self.owner)
        # copy-paste curl quickstart (with a copy-to-clipboard button) is shown
        self.assertContains(resp, "grant_type=client_credentials")
        self.assertContains(resp, 'data-copy="#kg-curl"')
        self.assertContains(resp, sa.client_id)
        # WHY: the secret is never stored, so a later page load must NOT show it.
        self.assertNotContains(self.client.get(reverse("api_credentials")), "s3cr3t")

    def test_delete_already_gone_is_idempotent(self):
        # WHY: a stale page / double-click must not 404 — already-deleted just returns to the list.
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("api_credentials_delete", args=[999999]))
        self.assertEqual(resp.status_code, 302)

    def test_rotate_shows_new_secret(self):
        self.client.force_login(self.owner)
        sa = ServiceAccount.objects.create(
            organization=self.org, client_id="kg-acme-x", keycloak_id="kc-x"
        )
        with patch("tenancy.keycloak_admin.rotate_client_secret", return_value="rotated!"):
            resp = self.client.post(reverse("api_credentials_rotate", args=[sa.pk]))
        self.assertContains(resp, "rotated!")

    def test_delete_removes_client_and_row(self):
        self.client.force_login(self.owner)
        sa = ServiceAccount.objects.create(
            organization=self.org, client_id="kg-acme-y", keycloak_id="kc-y"
        )
        with patch("tenancy.keycloak_admin.delete_client") as deleter:
            resp = self.client.post(reverse("api_credentials_delete", args=[sa.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ServiceAccount.objects.filter(pk=sa.pk).exists())
        deleter.assert_called_once_with("kc-y")

    def test_cannot_touch_another_orgs_credential(self):
        # WHY: org isolation — an Acme admin must not rotate/delete Globex's credentials.
        other = Organization.objects.create(name="Globex", slug="globex")
        sa = ServiceAccount.objects.create(
            organization=other, client_id="kg-globex-z", keycloak_id="kc-z"
        )
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(reverse("api_credentials_rotate", args=[sa.pk])).status_code, 404
        )
