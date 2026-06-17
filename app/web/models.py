"""App-side audit. Impersonation is app-mediated (django-hijack, ADR 0010) and never
touches Keycloak, so OUR DB is its system of record — Keycloak event logging covers the
auth layer, this covers who-impersonated-whom in the app.
"""
from django.conf import settings
from django.db import models


class ImpersonationEvent(models.Model):
    """One row per impersonation start/stop (append-only audit). FKs SET_NULL so the trail
    survives user deletion."""

    class Kind(models.TextChoices):
        START = "start", "Start"
        STOP = "stop", "Stop"

    impersonator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="impersonations_made",
    )
    impersonated = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="impersonation_events",
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.impersonator} {self.kind} → {self.impersonated} @ {self.at:%Y-%m-%d %H:%M}"


class ContactMessage(models.Model):
    """A lead from the public marketing contact form (ADR 0012). Stored so a lead is never lost
    even if the notification email fails. Public, unauthenticated — guard with care (honeypot now)."""

    name = models.CharField(max_length=120)
    email = models.EmailField()
    company = models.CharField(max_length=200, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} <{self.email}> @ {self.created_at:%Y-%m-%d %H:%M}"
