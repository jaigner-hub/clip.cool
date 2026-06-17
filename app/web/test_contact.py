"""Public marketing contact endpoint (ADR 0012) — /api/v1/contact.

It's a browser FORM post (not JSON) from the static marketing site, so it takes no auth and
redirects back to the thank-you page. Django forces the locmem email backend in tests, so we can
assert on mail.outbox.
"""
from django.core import mail
from django.test import TestCase

from web.models import ContactMessage

URL = "/api/v1/contact"


class ContactEndpointTests(TestCase):
    def test_valid_submission_stores_and_emails_and_redirects(self):
        # WHY: a real lead must be persisted (never lost) AND a notification sent; the browser is
        # mid-form-navigation, so the response must be a redirect, not JSON.
        resp = self.client.post(URL, {"name": "Dana", "email": "dana@acme.com",
                                       "company": "Acme", "message": "Interested in AEO."})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/thanks", resp["Location"])
        lead = ContactMessage.objects.get()
        self.assertEqual((lead.name, lead.email, lead.company), ("Dana", "dana@acme.com", "Acme"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("dana@acme.com", mail.outbox[0].body)

    def test_honeypot_drops_silently(self):
        # WHY: bots fill the hidden `website` field. We accept (redirect) but store/send nothing,
        # so the honeypot doesn't reveal itself.
        resp = self.client.post(URL, {"name": "Bot", "email": "bot@x.com", "website": "http://spam"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ContactMessage.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_invalid_email_is_ignored(self):
        # WHY: a malformed submission shouldn't 500 a browser navigation; drop it quietly.
        resp = self.client.post(URL, {"name": "X", "email": "not-an-email"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_no_auth_required(self):
        # WHY: it's a PUBLIC form — the global Keycloak auth must NOT gate it (no 401).
        self.assertNotEqual(self.client.post(URL, {"name": "A", "email": "a@b.com"}).status_code, 401)
