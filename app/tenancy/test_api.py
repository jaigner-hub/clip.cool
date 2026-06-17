"""JSON API tests (ADR 0011). Hermetic — `decode_keycloak_token` is patched so CI never
calls live Keycloak; the real JWKS path is exercised by the live deploy check. The point
is to prove auth gating, tenant isolation through the API, and the OpenAPI contract.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tenancy.models import Organization, OrganizationMembership, Project, ServiceAccount

User = get_user_model()


class ProjectsAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(name="Acme", slug="acme")
        cls.org_b = Organization.objects.create(name="Globex", slug="globex")
        cls.user_a = User.objects.create_user("a@acme.com", email="a@acme.com")
        OrganizationMembership.objects.create(organization=cls.org_a, user=cls.user_a)
        cls.user_b = User.objects.create_user("b@globex.com", email="b@globex.com")
        OrganizationMembership.objects.create(organization=cls.org_b, user=cls.user_b)
        cls.pa = Project.objects.create(organization=cls.org_a, name="A1", slug="a1")
        cls.pb = Project.objects.create(organization=cls.org_b, name="B1", slug="b1")

    def _get_as(self, email):
        with patch("web.api_auth.decode_keycloak_token", return_value={"email": email}):
            return self.client.get("/api/v1/projects", HTTP_AUTHORIZATION="Bearer x")

    def test_requires_auth(self):
        # WHY: no token -> 401. The API is never an open door.
        self.assertEqual(self.client.get("/api/v1/projects").status_code, 401)

    def test_member_sees_only_their_org(self):
        r = self._get_as("a@acme.com")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([p["name"] for p in r.json()], ["A1"])

    def test_no_cross_org_leak_via_api(self):
        # WHY: the API must enforce the SAME tenant isolation as the web UI.
        self.assertNotIn("B1", [p["name"] for p in self._get_as("a@acme.com").json()])

    def test_valid_token_unprovisioned_user_rejected(self):
        # WHY: a real Keycloak token for someone with no Django account gets no access.
        self.assertEqual(self._get_as("ghost@nowhere.com").status_code, 401)

    def test_superuser_sees_all(self):
        User.objects.create_superuser("root@keygrip.ai", email="root@keygrip.ai")
        names = [p["name"] for p in self._get_as("root@keygrip.ai").json()]
        self.assertCountEqual(names, ["A1", "B1"])


class CreateProjectAPITests(TestCase):
    """POST /projects — the JSON-API twin of the web create form (ADR 0011). Creates in the
    caller's own org only; isolation rides the same service the web view uses."""

    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(name="Acme", slug="acme")
        cls.user_a = User.objects.create_user("a@acme.com", email="a@acme.com")
        OrganizationMembership.objects.create(organization=cls.org_a, user=cls.user_a)

    def _post_as(self, email, body):
        with patch("web.api_auth.decode_keycloak_token", return_value={"email": email}):
            return self.client.post(
                "/api/v1/projects", data=body,
                content_type="application/json", HTTP_AUTHORIZATION="Bearer x",
            )

    def test_requires_auth(self):
        # WHY: writes are never an open door either.
        r = self.client.post("/api/v1/projects", data={"name": "x"}, content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_member_creates_in_their_org(self):
        r = self._post_as("a@acme.com", {"name": "Marketing site"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["organization_id"], self.org_a.id)
        self.assertTrue(Project.objects.filter(organization=self.org_a, name="Marketing site").exists())

    def test_empty_name_is_400_not_500(self):
        r = self._post_as("a@acme.com", {"name": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertIn("detail", r.json())
        self.assertEqual(Project.objects.filter(organization=self.org_a).count(), 0)

    def test_unprovisioned_user_rejected(self):
        # WHY: a valid token for someone with no Django account gets no write access.
        self.assertEqual(self._post_as("ghost@nowhere.com", {"name": "x"}).status_code, 401)


class DeleteProjectAPITests(TestCase):
    """DELETE /projects/{id} — archive (soft-delete), owner/admin only, superuser-first. The
    JSON-API twin of the web confirm flow; both go through services.archive_project."""

    Role = OrganizationMembership.Role

    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(name="Acme", slug="acme")
        cls.org_b = Organization.objects.create(name="Globex", slug="globex")
        cls.admin = User.objects.create_user("admin@acme.com", email="admin@acme.com")
        OrganizationMembership.objects.create(organization=cls.org_a, user=cls.admin, role=cls.Role.ADMIN)
        cls.member = User.objects.create_user("member@acme.com", email="member@acme.com")
        OrganizationMembership.objects.create(organization=cls.org_a, user=cls.member, role=cls.Role.MEMBER)
        cls.pa = Project.objects.create(organization=cls.org_a, name="A1", slug="a1")
        cls.pb = Project.objects.create(organization=cls.org_b, name="B1", slug="b1")
        ServiceAccount.objects.create(organization=cls.org_a, client_id="acme-svc")

    def _delete_as(self, project_id, *, email=None, azp=None):
        token = {"email": email} if email else {"azp": azp}
        with patch("web.api_auth.decode_keycloak_token", return_value=token):
            return self.client.delete(f"/api/v1/projects/{project_id}", HTTP_AUTHORIZATION="Bearer x")

    def _active(self, project):
        project.refresh_from_db()
        return project.is_active

    def test_requires_auth(self):
        # WHY: destructive writes are never an open door.
        self.assertEqual(self.client.delete(f"/api/v1/projects/{self.pa.id}").status_code, 401)

    def test_admin_archives_and_it_drops_from_the_list(self):
        # WHY: the happy path — an admin archives; it goes inactive AND disappears from the listing.
        r = self._delete_as(self.pa.id, email="admin@acme.com")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(self._active(self.pa))
        self.assertNotIn("A1", self._list_names("admin@acme.com"))

    def _list_names(self, email):
        with patch("web.api_auth.decode_keycloak_token", return_value={"email": email}):
            return [p["name"] for p in self.client.get("/api/v1/projects", HTTP_AUTHORIZATION="Bearer x").json()]

    def test_member_cannot_delete(self):
        # WHY: archiving a whole project is owner/admin-only — a plain member is forbidden, not 404.
        r = self._delete_as(self.pa.id, email="member@acme.com")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self._active(self.pa))

    def test_cross_org_is_404_not_403(self):
        # WHY: an admin of org A must not even learn org B's project exists — 404, not a leak.
        r = self._delete_as(self.pb.id, email="admin@acme.com")
        self.assertEqual(r.status_code, 404)
        self.assertTrue(self._active(self.pb))

    def test_missing_project_is_404(self):
        self.assertEqual(self._delete_as(999999, email="admin@acme.com").status_code, 404)

    def test_service_account_cannot_delete(self):
        # WHY: machine clients can provision (POST) but not destroy — no role, so 403.
        r = self._delete_as(self.pa.id, azp="acme-svc")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self._active(self.pa))

    def test_superuser_can_archive_any_org(self):
        # WHY: superuser-first — staff tooling reaches across orgs (mirrors the list endpoint).
        User.objects.create_superuser("root@keygrip.ai", email="root@keygrip.ai")
        r = self._delete_as(self.pb.id, email="root@keygrip.ai")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(self._active(self.pb))


class APIContractTests(TestCase):
    def test_openapi_schema_is_public_and_describes_projects(self):
        # WHY: the typed OpenAPI contract is the API-first deliverable (open-gap #3),
        # and the schema/docs are intentionally public (no auth to read the contract).
        r = self.client.get("/api/v1/openapi.json")
        self.assertEqual(r.status_code, 200)
        schema = r.json()
        self.assertIn("/api/v1/projects", schema["paths"])
        self.assertIn("ProjectOut", schema["components"]["schemas"])
        # the endpoint requires auth, advertised as an OAuth2 (authorization-code) scheme so
        # Swagger's Authorize drives the Keycloak login.
        op = schema["paths"]["/api/v1/projects"]["get"]
        self.assertEqual(op["security"], [{"KeycloakAuth": []}])
        scheme = schema["components"]["securitySchemes"]["KeycloakAuth"]
        self.assertEqual(scheme["type"], "oauth2")
        self.assertIn("authorizationCode", scheme["flows"])  # interactive users
        self.assertIn("clientCredentials", scheme["flows"])  # machine-to-machine

class ServiceAccountAPITests(TestCase):
    """Machine-to-machine API access (ADR 0011): a client-credentials token has no email; the
    org is resolved from the token's azp (client_id) via ServiceAccount. Strictly org-scoped."""

    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(name="Acme", slug="acme")
        cls.org_b = Organization.objects.create(name="Globex", slug="globex")
        cls.pa = Project.objects.create(organization=cls.org_a, name="A1", slug="a1")
        cls.pb = Project.objects.create(organization=cls.org_b, name="B1", slug="b1")
        ServiceAccount.objects.create(organization=cls.org_a, client_id="acme-svc")

    def _get_as_client(self, azp):
        # client-credentials token: no email, azp = client_id
        with patch("web.api_auth.decode_keycloak_token", return_value={"azp": azp}):
            return self.client.get("/api/v1/projects", HTTP_AUTHORIZATION="Bearer x")

    def test_service_account_scoped_to_its_org(self):
        r = self._get_as_client("acme-svc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([p["name"] for p in r.json()], ["A1"])

    def test_service_account_no_cross_org_leak(self):
        # WHY: a machine token must see ONLY its mapped org — never another's data.
        self.assertNotIn("B1", [p["name"] for p in self._get_as_client("acme-svc").json()])

    def test_unknown_client_rejected(self):
        self.assertEqual(self._get_as_client("ghost-svc").status_code, 401)

    def test_inactive_service_account_rejected(self):
        ServiceAccount.objects.create(organization=self.org_b, client_id="off-svc", is_active=False)
        self.assertEqual(self._get_as_client("off-svc").status_code, 401)


class ContractAndDocsTests(TestCase):
    def test_docs_page_is_public_with_scoped_relaxed_csp(self):
        # WHY: docs are public (read the contract without auth), and the Swagger explorer
        # needs inline styles — so CSP is relaxed ONLY here. App pages stay strict
        # (asserted in web.tests.CSPTests).
        resp = self.client.get("/api/v1/docs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("unsafe-inline", resp.headers["Content-Security-Policy"])
        # OAuth popup needs the opener link preserved across the cross-origin round-trip.
        self.assertEqual(
            resp.headers.get("Cross-Origin-Opener-Policy"), "same-origin-allow-popups"
        )

    def test_oauth_redirect_handler_is_coop_unsafe_none(self):
        # WHY: the popup returns from Keycloak (unsafe-none); the handler must be unsafe-none too
        # or the COOP context-group swap nulls window.opener and the OAuth handoff fails.
        resp = self.client.get("/api/v1/docs/oauth2-redirect.html")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Cross-Origin-Opener-Policy"), "unsafe-none")

    def test_docs_and_redirect_dont_leak_template_comments(self):
        # WHY: a multi-line {# #} leaks as visible text (keeps happening). Guard both templates.
        for url in ("/api/v1/docs", "/api/v1/docs/oauth2-redirect.html"):
            body = self.client.get(url).content.decode()
            self.assertNotIn("{#", body, f"comment leak in {url}")
