"""Tenancy JSON API router (ADR 0011). A thin adapter over `services` — same service
layer the HTML views and (later) MCP tools call. Auth + org-scoping are inherited:
`request.auth` is the Keycloak-authenticated user, and the service is superuser-first
+ org-scoped, so isolation matches the web UI exactly.
"""
from ninja import Router

from . import services
from .schemas import ErrorOut, ProjectIn, ProjectOut

router = Router(tags=["projects"])


@router.get("/projects", response=list[ProjectOut], summary="List projects in your organization")
def list_projects(request):
    """Projects in the caller's organization (all organizations, for superusers)."""
    return list(services.list_projects_for(request.auth))


@router.post(
    "/projects",
    response={201: ProjectOut, 400: ErrorOut},
    summary="Create a project in your organization",
)
def create_project(request, data: ProjectIn):
    """Create a project in the caller's own organization (the same one List scopes to). Self-serve
    for any member; the slug is derived from the name and made unique within the org."""
    org = services.org_for_actor(request.auth)
    if org is None:
        return 400, {"detail": "You must belong to an organization to create a project."}
    try:
        return 201, services.create_project(org, data.name)
    except ValueError as exc:
        return 400, {"detail": str(exc)}


@router.delete(
    "/projects/{project_id}",
    response={204: None, 403: ErrorOut, 404: ErrorOut},
    summary="Archive (soft-delete) a project",
)
def delete_project(request, project_id: int):
    """Archive a project — a soft delete: it drops out of listings but its content is kept and an
    admin can restore it. **Owners and admins only** (superuser-first); a machine client gets 403.
    404 if the project isn't in your organization (no cross-org existence leak), 403 if you're a
    member without the admin role. Idempotent."""
    actor = request.auth
    project = services.get_project_for_actor(actor, project_id)
    if project is None:
        return 404, {"detail": "Project not found."}
    if not services.is_org_admin(actor, project.organization):
        return 403, {"detail": "Only owners and admins can delete projects."}
    services.archive_project(project)
    return 204, None
