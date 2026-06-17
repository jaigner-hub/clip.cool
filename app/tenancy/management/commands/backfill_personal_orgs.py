"""One-shot backfill of the "every user has an org" invariant (ADR 0009 amendment).

Provisions a personal owner-org for any existing user who has no membership — the same thing
login now does automatically (web.auth), but without waiting for each user to log in again.
Idempotent: users who already belong to an org are skipped, so it's safe to re-run.

    python manage.py backfill_personal_orgs [--dry-run]
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from tenancy.services import ensure_personal_org, get_membership


class Command(BaseCommand):
    help = "Create a personal owner-org for any user without an org membership (ADR 0009)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report, don't write.")

    def handle(self, *args, **opts):
        User = get_user_model()
        orgless = [u for u in User.objects.all() if get_membership(u) is None]
        if not orgless:
            self.stdout.write(self.style.SUCCESS("All users already belong to an org. Nothing to do."))
            return
        for u in orgless:
            if opts["dry_run"]:
                self.stdout.write(f"  [dry-run] would provision {u.email or u.get_username()}")
                continue
            m = ensure_personal_org(u)
            self.stdout.write(f"  provisioned {u.email or u.get_username()} -> {m.organization.name} ({m.role})")
        verb = "would provision" if opts["dry_run"] else "provisioned"
        self.stdout.write(self.style.SUCCESS(f"Done — {verb} {len(orgless)} user(s)."))
