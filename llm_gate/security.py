"""Security policy helpers shared by the proxy and server startup."""

from __future__ import annotations

import hmac
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)[^\s&,;]+"),
)


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


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate_server_security(
    *, host: str, token: str | None = None, allow_anonymous: bool | None = None,
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
    if addresses and any(
        address.is_private or address.is_loopback or address.is_link_local for address in addresses
    ) and host not in allowed:
        raise ValueError("upstream URL targets a private or loopback host not in the allowlist")
    return normalized


def host_is_allowed(host: str, allow_private_hosts: set[str]) -> bool:
    """Resolve a configured hostname and fail closed for private resolved addresses."""
    normalized = host.rstrip(".").lower()
    if normalized in {item.rstrip(".").lower() for item in allow_private_hosts}:
        return True
    try:
        results = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("upstream hostname could not be resolved") from exc
    for result in results:
        address = ipaddress.ip_address(result[4][0])
        if address.is_private or address.is_loopback or address.is_link_local:
            raise ValueError("upstream hostname resolves to a private or loopback address")
    return True


def bearer_matches(provided: str | None, expected: str) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(provided, expected)


__all__ = ["ServerSecurity", "bearer_matches", "host_is_allowed", "redact_text", "validate_server_security", "validate_upstream_url"]
