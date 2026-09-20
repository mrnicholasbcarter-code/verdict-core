"""Security policy helpers shared by the proxy and server startup."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(
        r'''(?ix)
        ((?:["']?)(?:api[_-]?key|token|password|secret)(?:["']?)\s*[=:]\s*)
        (?:"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*'|[^\s&,;]+)
        '''
    ),
)


def _address_is_restricted(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not address.is_global or address.is_multicast


@dataclass(frozen=True)
class ServerSecurity:
    token: str | None
    anonymous: bool
    unix_socket: str | None


def redact_text(value: object) -> str:
    """Return diagnostic text with credentials and bearer tokens removed."""
    text = str(value)
    text = re.sub(r"(?i)(https?://)([^/@:]+)(?::[^/@]*)?@", r"\1[redacted]@", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[redacted]", text)
    return text


def fingerprint_text(value: object, *, length: int = 32) -> str:
    """Return a stable sha256 fingerprint for privacy-safe logging and learning."""
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    if length > 0:
        digest = digest[:length]
    return f"sha256:{digest}"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate_server_security(
    *,
    host: str,
    token: str | None = None,
    allow_anonymous: bool | None = None,
    unix_socket: str | None = None,
) -> ServerSecurity:
    """Validate that startup cannot create an unintentionally public anonymous server."""
    if token is None:
        import os

        token = os.getenv("LLMGATE_AUTH_TOKEN") or None
    if allow_anonymous is None:
        import os

        allow_anonymous = _truthy(os.getenv("LLMGATE_ALLOW_ANONYMOUS"))
    if unix_socket and token:
        raise ValueError("configure either bearer authentication or Unix-socket mode, not both")
    if unix_socket:
        raise ValueError("Unix-socket authentication is not supported")
    if not token and not unix_socket and not allow_anonymous:
        raise ValueError("production server requires LLMGATE_AUTH_TOKEN or Unix-socket mode")
    if allow_anonymous and not unix_socket:
        try:
            address = ipaddress.ip_address(host)
            loopback = address.is_loopback
        except ValueError:
            loopback = host.lower().rstrip(".") in {"localhost", "ip6-localhost"}
        if not loopback:
            raise ValueError("anonymous development mode is loopback-only")
    return ServerSecurity(token=token, anonymous=allow_anonymous, unix_socket=unix_socket)


def validate_upstream_url(base_url: str, *, allow_private_hosts: set[str] | None = None) -> str:
    """Validate an upstream URL before any request can be made."""
    normalized = base_url.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("upstream URL scheme must be http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("upstream URL must have a valid host without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("upstream URL must not contain a query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("upstream URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("upstream URL has an invalid port")
    host = parsed.hostname.rstrip(".").lower()
    allowed = {item.rstrip(".").lower() for item in (allow_private_hosts or set())}
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        addresses = set()
    if (
        addresses
        and any(_address_is_restricted(address) for address in addresses)
        and host not in allowed
    ):
        raise ValueError("upstream URL targets a private or non-public host not in the allowlist")
    return normalized


def host_is_allowed(host: str, allow_private_hosts: set[str]) -> tuple[str, ...]:
    """Resolve a hostname once and return every validated transport address."""
    normalized = host.rstrip(".").lower()
    if normalized in {item.rstrip(".").lower() for item in allow_private_hosts}:
        return (normalized,)
    try:
        results = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("upstream hostname could not be resolved") from exc
    addresses = tuple(sorted({str(result[4][0]) for result in results}))
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address)
        if _address_is_restricted(address):
            raise ValueError("upstream hostname resolves to a private or non-public address")
    return addresses


def pin_upstream_url(
    url: str, allow_private_hosts: set[str]
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Resolve and pin a request URL while preserving its HTTP/TLS identity."""
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise ValueError("upstream URL must have a host")
    addresses = host_is_allowed(host, allow_private_hosts)
    address = addresses[0]
    authority_host = f"[{address}]" if ":" in address else address
    pinned_netloc = f"{authority_host}:{parsed.port}" if parsed.port else authority_host
    default_port = 443 if parsed.scheme == "https" else 80
    original_authority = host if parsed.port in {None, default_port} else f"{host}:{parsed.port}"
    extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}
    return (
        urlunsplit((parsed.scheme, pinned_netloc, parsed.path, parsed.query, parsed.fragment)),
        {"host": original_authority},
        extensions,
    )


def bearer_matches(provided: str | None, expected: str) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(provided, expected)


__all__ = [
    "ServerSecurity",
    "bearer_matches",
    "fingerprint_text",
    "host_is_allowed",
    "redact_text",
    "validate_server_security",
    "validate_upstream_url",
]
