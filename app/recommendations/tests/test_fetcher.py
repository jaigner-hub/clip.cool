"""SSRF guard + extraction tests.

WHY: `fetch_page` retrieves an arbitrary, caller-supplied URL server-side. Without the guard it
is a server-side request forgery primitive — an attacker could point it at internal services or
the cloud metadata endpoint (169.254.169.254) to exfiltrate credentials. These tests pin the
guard so a refactor can't quietly weaken it.
"""
from django.test import SimpleTestCase

from recommendations.fetcher import FetchError, _extract, validate_public_url


class SSRFGuardTests(SimpleTestCase):
    def test_rejects_non_http_scheme(self):
        for url in ["ftp://example.com/x", "file:///etc/passwd", "gopher://x"]:
            with self.assertRaises(FetchError):
                validate_public_url(url)

    def test_rejects_loopback(self):
        # WHY: localhost / 127.0.0.1 reach services bound to the origin box itself.
        for url in ["http://127.0.0.1/", "http://localhost/", "http://[::1]/"]:
            with self.assertRaises(FetchError):
                validate_public_url(url)

    def test_rejects_private_ranges(self):
        for url in ["http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/"]:
            with self.assertRaises(FetchError):
                validate_public_url(url)

    def test_rejects_cloud_metadata_ip(self):
        # WHY: 169.254.169.254 is the cloud metadata endpoint — the classic SSRF credential leak.
        with self.assertRaises(FetchError):
            validate_public_url("http://169.254.169.254/latest/meta-data/")

    def test_allows_public_ip_literal(self):
        # A public address passes (IP literal → no network call in the test).
        scheme, host = validate_public_url("http://93.184.216.34/")
        self.assertEqual(scheme, "http")


class ExtractTests(SimpleTestCase):
    def test_strips_scripts_and_extracts_title_meta(self):
        html = (
            b"<html><head><title> Hi </title>"
            b"<meta name='description' content='A page'></head>"
            b"<body><script>alert(1)</script><p>Body text here</p></body></html>"
        )
        out = _extract("https://example.com/p", html)
        self.assertEqual(out["title"], "Hi")
        self.assertEqual(out["meta"], "A page")
        self.assertIn("Body text here", out["text"])
        self.assertNotIn("alert(1)", out["text"])  # script content removed
        self.assertEqual(len(out["content_hash"]), 64)  # sha256 hex
