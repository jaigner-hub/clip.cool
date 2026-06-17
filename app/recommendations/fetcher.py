"""URL fetching + content extraction for Tier-1 analysis (ADR 0013).

Fetching an arbitrary, caller-supplied URL server-side is a classic SSRF vector, so the guard
here is load-bearing: scheme allow-list, DNS resolution, and rejection of any private / loopback
/ link-local / reserved address (incl. the cloud metadata IP 169.254.169.254). Redirects are
followed manually so every hop is re-validated — an allowed page can't 302 us to an internal one.

This is the *basic* guard the ADR's Tier-1-hardening follow-up will build on (egress allow-list,
rate-limit, fetch cache, per-caller cost caps remain open).
"""
import hashlib
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = "KeygripBot/1.0 (+https://app.vent.dog/recommendations)"
MAX_BYTES = 3_000_000          # cap download size
MAX_TEXT_CHARS = 20_000        # cap extracted text fed to the model (bounds cost)
MAX_REDIRECTS = 5
TIMEOUT = 20.0


class FetchError(Exception):
    """A URL could not be fetched (invalid, blocked by the SSRF guard, or transport failure)."""


def validate_public_url(url):
    """Validate that `url` is an http(s) URL resolving only to public IPs. Raises FetchError
    otherwise. Returns the parsed (scheme, host) for reuse. Pure/sync — directly unit-testable."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("Only http and https URLs are allowed.")
    host = parts.hostname
    if not host:
        raise FetchError("URL has no host.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise FetchError(f"Could not resolve host: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            # link_local covers the 169.254.169.254 cloud metadata endpoint.
            raise FetchError(f"Refusing to fetch a non-public address ({ip}).")
    return parts.scheme, host


async def fetch_page(url):
    """Fetch a URL (SSRF-guarded, manual redirects) and extract its content.

    Returns a dict: {url (final), title, meta, text, content_hash}. Raises FetchError on failure.
    """
    current = url
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=False,
        headers={"User-Agent": USER_AGENT}, max_redirects=0,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_url(current)  # re-validate every hop
            try:
                resp = await client.get(current)
            except httpx.HTTPError as e:
                logger.warning("Fetch transport error for %s: %s", current, e)
                raise FetchError(f"Could not fetch the URL: {e}") from e
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise FetchError("Redirect with no Location header.")
                current = str(resp.url.join(location))
                continue
            if resp.status_code != 200:
                raise FetchError(f"The URL returned HTTP {resp.status_code}.")
            content = resp.content[:MAX_BYTES]
            return _extract(str(resp.url), content)
    raise FetchError("Too many redirects.")


def _extract(final_url, content_bytes):
    soup = BeautifulSoup(content_bytes, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    meta_tag = soup.find("meta", attrs={"name": "description"})
    meta = (meta_tag.get("content", "").strip() if meta_tag else "")
    text = " ".join(soup.get_text(separator=" ").split())[:MAX_TEXT_CHARS]
    return {
        "url": final_url,
        "title": title[:1000],
        "meta": meta,
        "text": text,
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
