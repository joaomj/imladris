"""Validate saved URLs and reject local/private targets (SSRF guard)."""

import ipaddress
import socket
from urllib.parse import urlparse

SAVED_URL_EXCLUSION_REASON = "X/Twitter and Reddit content is excluded from webpage collection"


def is_excluded_saved_url(url: str) -> bool:
    """Identify excluded social hosts without matching lookalike domains."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(
        host == domain or host.endswith("." + domain)
        for domain in ("x.com", "twitter.com", "reddit.com", "redd.it")
    )


class SavedUrlError(ValueError):
    """Raised when a saved URL is not a fetchable public http(s) URL."""


MAX_URL_CHARS = 8192


def validate_saved_url(url: str) -> str:
    """Return the stripped URL when it is a well-formed http(s) URL.

    Only scheme and host presence are checked here. Reachability and
    private-network guards run in :func:`assert_public_url`.
    """
    text = (url or "").strip()
    if not text:
        raise SavedUrlError("saved URL is empty")
    if len(text) > MAX_URL_CHARS:
        raise SavedUrlError("saved URL is too long")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise SavedUrlError(f"unsupported URL scheme: {parsed.scheme or '(missing)'}")
    if not parsed.hostname:
        raise SavedUrlError("saved URL has no host")
    if parsed.username or parsed.password:
        raise SavedUrlError("saved URL must not contain credentials")
    return text


def _reject_private_ip(address: str, url: str) -> None:
    """Raise when one resolved IP is not a public unicast address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as error:
        raise SavedUrlError(f"unparseable DNS address for saved URL: {address}") from error
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise SavedUrlError(f"saved URL resolves to a non-public address: {url}")


def assert_public_url(url: str) -> str:
    """Validate syntax and reject DNS targets that are not public.

    Donsetch applies its own redirect guards internally; this check covers
    the initial URL and every hop observed on the requests probe path.
    """
    text = validate_saved_url(url)
    host = urlparse(text).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise SavedUrlError(f"saved URL host does not resolve: {host}") from error
    if not infos:
        raise SavedUrlError(f"saved URL host does not resolve: {host}")
    seen: set[str] = set()
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.add(address)
            _reject_private_ip(address, text)
    return text


def assert_public_redirect_chain(urls: list[str]) -> None:
    """Validate every observed redirect hop, including the final URL."""
    for hop in urls:
        assert_public_url(hop)
