"""Public (unauthenticated) API endpoints — the marketing contact form (ADR 0012).

Separate from the org-scoped tenancy API: these take **no auth** and aren't tenant-scoped. The
marketing site (static, on the apex) posts a plain HTML form here; we store + email the lead and
redirect the browser back to a thank-you page. Honeypot-guarded; rate-limiting is a follow-up.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpResponseRedirect
from ninja import Form, Router, Schema

from .models import ContactMessage

logger = logging.getLogger(__name__)
router = Router(tags=["public"])


class ContactIn(Schema):
    name: str
    email: str
    company: str = ""
    message: str = ""
    website: str = ""  # honeypot — hidden in the form; humans leave it empty, bots fill it


@router.post("/contact", auth=None, summary="Marketing contact form (public)")
def contact(request, data: ContactIn = Form(...)):
    """Accept the marketing contact form, store + email the lead, then 302 back to the marketing
    thank-you page (it's a browser form navigation, so we redirect rather than return JSON)."""
    thanks = HttpResponseRedirect(settings.CONTACT_THANKS_URL)
    if data.website.strip():
        return thanks  # bot tripped the honeypot — accept silently, store nothing
    name, email = data.name.strip()[:120], data.email.strip()[:200]
    if not name or "@" not in email:
        return thanks  # invalid; don't error a browser navigation
    lead = ContactMessage.objects.create(
        name=name, email=email,
        company=data.company.strip()[:200], message=data.message.strip()[:2000],
    )
    try:
        send_mail(
            subject=f"[Keygrip] New contact from {name}",
            message=f"Name: {name}\nEmail: {email}\nCompany: {data.company.strip()}\n\n{data.message.strip()}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[settings.CONTACT_EMAIL],
            fail_silently=False,
        )
    except Exception:
        logger.warning("Contact email failed for lead %s (stored — not lost)", lead.pk, exc_info=True)
    return thanks
