"""Attach the caller's organization to the request (lazily).

`request.organization` is the org the logged-in user belongs to, or None. It's a
SimpleLazyObject so requests that never touch it (e.g. /metrics) pay no query —
mirroring how Django resolves request.user.
"""
from django.utils.functional import SimpleLazyObject

from .services import get_user_org


class OrganizationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = SimpleLazyObject(
            lambda: get_user_org(getattr(request, "user", None))
        )
        return self.get_response(request)
