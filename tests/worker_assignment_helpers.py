"""An independent signer for worker assignments, written only from the format in the spec.

It uses ``hmac``, ``hashlib``, ``base64`` and ``json`` and imports nothing from
``verdict.worker_assignment``, so a test that signs with it checks the verifier against the
written format and not against itself.  The production minting function belongs to the executor
story (#1325).

Format (``openspec/changes/serve-worker-assignment``)::

    vwa1.<payload>.<tag>
    payload = base64url(JSON {"exp", "iat", "node_id", "route_id", "run_id"})   # unpadded
    tag     = base64url(HMAC-SHA256(key, "verdict-worker-assignment/v1\\n" + payload))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

PREFIX = "vwa1"
DOMAIN = b"verdict-worker-assignment/v1\n"
#: The most seconds an assignment may live: ``exp - iat`` is at most this.
MAX_LIFETIME_SECONDS = 1800
#: 9999-12-31T23:59:59Z, the last second a UTC ``datetime`` holds.
MAX_EXP = 253402300799

#: Marks "compute a valid issued-at from ``exp``".
_DEFAULT: Any = object()


def b64url(data: bytes) -> str:
    """Unpadded URL-safe base64, as the format requires."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def tag_for(payload_text: str, key: bytes) -> str:
    return b64url(hmac.new(key, DOMAIN + payload_text.encode("ascii"), hashlib.sha256).digest())


def sign_payload_text(payload_text: str, key: bytes) -> str:
    """Sign ``payload_text`` exactly as given (it need not be valid JSON)."""
    return f"{PREFIX}.{payload_text}.{tag_for(payload_text, key)}"


def sign_object(payload: Any, key: bytes) -> str:
    """Sign any JSON value as the payload (it need not satisfy the field rules)."""
    text = b64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return sign_payload_text(text, key)


def sign_raw_json(raw_json: str, key: bytes) -> str:
    """Sign JSON text as written, so ``exp`` can be an integer Python would not emit."""
    return sign_payload_text(b64url(raw_json.encode("utf-8")), key)


def payload_fields(
    *,
    route_id: Any = "omniroute/free-route",
    run_id: Any = "run-1",
    node_id: Any = "node-1",
    exp: Any = 2_000_000_000,
    iat: Any = _DEFAULT,
) -> dict[str, Any]:
    """The five payload fields.  ``iat`` defaults to 900 seconds before ``exp`` (floored at 0)."""
    if iat is _DEFAULT:
        iat = max(exp - 900, 0) if isinstance(exp, int) and not isinstance(exp, bool) else 0
    return {"route_id": route_id, "run_id": run_id, "node_id": node_id, "exp": exp, "iat": iat}


def assignment(key: bytes, **fields: Any) -> str:
    """A correctly signed assignment; override any field to build an invalid-but-signed one."""
    return sign_object(payload_fields(**fields), key)


def payload_part(token: str) -> str:
    return token.split(".")[1]


def tag_part(token: str) -> str:
    return token.split(".")[2]


def decode_payload(token: str) -> dict[str, Any]:
    text = payload_part(token)
    decoded = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    assert isinstance(decoded, dict)
    return decoded


def retag_with_changed(token: str, **changes: Any) -> str:
    """Change fields of the payload and keep the original tag (a tampered token)."""
    payload = decode_payload(token)
    payload.update(changes)
    text = b64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{PREFIX}.{text}.{tag_part(token)}"
