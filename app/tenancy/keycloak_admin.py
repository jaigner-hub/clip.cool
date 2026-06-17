"""Thin Keycloak Admin REST API client — lets the app mint/rotate/revoke customer
service-account clients for self-serve API credentials (ADR 0011).

Auth: the app's own `keygrip-kc-admin` service account (client-credentials, scoped to
realm-management:manage-clients). Calls the INTERNAL Keycloak (edge net) to skip Cloudflare.
"""
import requests
from django.conf import settings

_TIMEOUT = 10


class KeycloakAdminError(Exception):
    pass


def _base():
    return settings.KEYCLOAK_ADMIN_BASE


def _realm():
    return settings.KEYCLOAK_REALM


def _token():
    r = requests.post(
        f"{_base()}/realms/{_realm()}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings.KC_ADMIN_CLIENT_ID,
            "client_secret": settings.KC_ADMIN_CLIENT_SECRET,
        },
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise KeycloakAdminError(f"admin token failed: {r.status_code}")
    return r.json()["access_token"]


def _headers():
    return {"Authorization": f"Bearer {_token()}"}


def create_service_account_client(client_id, name):
    """Create a confidential client with a service account; return (keycloak_id, secret)."""
    r = requests.post(
        f"{_base()}/admin/realms/{_realm()}/clients",
        json={
            "clientId": client_id,
            "name": name or client_id,
            "protocol": "openid-connect",
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
        },
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    if r.status_code != 201:
        raise KeycloakAdminError(f"create client failed: {r.status_code} {r.text[:200]}")
    kc_id = r.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
    return kc_id, get_client_secret(kc_id)


def get_client_secret(kc_id):
    r = requests.get(
        f"{_base()}/admin/realms/{_realm()}/clients/{kc_id}/client-secret",
        headers=_headers(), timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise KeycloakAdminError(f"get secret failed: {r.status_code}")
    return r.json()["value"]


def rotate_client_secret(kc_id):
    r = requests.post(
        f"{_base()}/admin/realms/{_realm()}/clients/{kc_id}/client-secret",
        headers=_headers(), timeout=_TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise KeycloakAdminError(f"rotate secret failed: {r.status_code}")
    return r.json()["value"]


def delete_client(kc_id):
    r = requests.delete(
        f"{_base()}/admin/realms/{_realm()}/clients/{kc_id}",
        headers=_headers(), timeout=_TIMEOUT,
    )
    if r.status_code not in (204, 404):  # 404 = already gone, treat as success
        raise KeycloakAdminError(f"delete client failed: {r.status_code}")
