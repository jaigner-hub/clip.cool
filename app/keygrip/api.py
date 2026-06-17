"""Keygrip JSON API root (ADR 0011) — Django Ninja, mounted at /api/v1/.

- Auth: Keycloak bearer tokens (web.api_auth.KeycloakBearer), validated via JWKS.
- Docs: Swagger UI at /api/v1/docs (public, self-hosted assets), schema at
  /api/v1/openapi.json. `persistAuthorization` keeps a pasted token across reloads.
- Each bounded context contributes a router; all share the service layer.
"""
from ninja import NinjaAPI
from ninja.openapi.docs import Swagger

from web.api_auth import KeycloakAuth
from web.api import router as public_router
from tenancy.api import router as tenancy_router
from recommendations.api import router as recommendations_router

api = NinjaAPI(
    title="Keygrip API",
    version="1.0.0",
    description=(
        "JSON API for Keygrip. Click **Authorize** to log in via Keycloak (or paste a "
        "Bearer token). All endpoints are organization-scoped."
    ),
    auth=KeycloakAuth(),
    docs=Swagger(settings={"persistAuthorization": True}),
)

api.add_router("", tenancy_router)
api.add_router("", recommendations_router)
api.add_router("", public_router)  # public, unauthenticated (marketing contact form)
