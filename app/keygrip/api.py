"""clip.cool JSON API root (ADR 0011) — Django Ninja, mounted at /api/v1/.

- Auth: Keycloak bearer tokens (web.api_auth.KeycloakAuth), validated via JWKS.
- Docs: Swagger UI at /api/v1/docs (public, self-hosted assets), schema at
  /api/v1/openapi.json. `persistAuthorization` keeps a pasted token across reloads.
- Each bounded context contributes a router; all share the service layer.
"""
from ninja import NinjaAPI
from ninja.openapi.docs import Swagger

from web.api_auth import KeycloakAuth
from web.api import router as public_router
from clips.api import router as clips_router

api = NinjaAPI(
    title="clip.cool API",
    version="1.0.0",
    description=(
        "JSON API for clip.cool. Click **Authorize** to log in via Keycloak (or paste a "
        "Bearer token). Media endpoints require a user token."
    ),
    auth=KeycloakAuth(),
    docs=Swagger(settings={"persistAuthorization": True}),
)

api.add_router("", clips_router)
api.add_router("", public_router)  # public, unauthenticated (contact form)
