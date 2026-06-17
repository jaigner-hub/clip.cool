# Idempotent: register/refresh the Keycloak OIDC provider (django-allauth SocialApp) so GlitchTip
# authenticates via Keycloak. Run inside gt-web (`manage.py shell < setup_oidc.py`); reads GT_OIDC_*
# from the container env. GlitchTip wires named providers (gitlab/gitea/…) from env but not a
# generic OIDC one, so the Keycloak provider lives in the DB — this keeps it config-as-code.
import os

from allauth.socialaccount.models import SocialApp

# GlitchTip runs allauth sites-less (no django.contrib.sites), so SocialApp is global — don't touch
# the sites M2M.
app, created = SocialApp.objects.update_or_create(
    provider="openid_connect",
    provider_id="keycloak",
    defaults={
        "name": "Keycloak",
        "client_id": os.environ["GT_OIDC_CLIENT_ID"],
        "secret": os.environ["GT_OIDC_SECRET"],
        "settings": {"server_url": os.environ["GT_OIDC_SERVER_URL"]},
    },
)
print("OIDC SocialApp", "created" if created else "updated", "->", app.provider_id)
