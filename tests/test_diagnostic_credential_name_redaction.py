"""A credential *name* in a diagnostic line is not a credential.

``NAME: <one word>`` is how a real key is written (YAML, ``.env`` dumps), and a
long lowercase word cannot be told apart from an English word without guessing.
These tests pin how realistic secrets in that syntax are redacted today, so a
change to how Verdict renders credential names cannot weaken redaction.

Offline only. Every value below is fake.
"""

from __future__ import annotations

import pytest

from verdict.security import redact_text

# --- secret shapes stay redacted ---------------------------------------------

FAKE_WORD = "qzxkvbnwmtrplhgfdsjy"  # one lowercase word, long and random-looking

SECRET_SHAPES = [
    pytest.param(
        "OMNIROUTE_API_KEY: sk-fake0000notreal0000example",
        "OMNIROUTE_API_KEY: [redacted]",
        id="name-colon-sk-key",
    ),
    pytest.param(
        "OMNIROUTE_API_KEY=abc123fake456notreal789example",
        "OMNIROUTE_API_KEY=[redacted]",
        id="name-equals-value",
    ),
    pytest.param(
        "api_key: Fq7ZxK2mPw9LtR4vNb6YcH3sJd8GhE5u",
        "api_key: [redacted]",
        id="api-key-long-random-token",
    ),
    pytest.param(
        'api_key: "fake quoted secret 0000 notreal"',
        "api_key: [redacted]",
        id="api-key-double-quoted",
    ),
    pytest.param(
        "OMNIROUTE_API_KEY: 'fake-quoted-0000-notreal-example'",
        "OMNIROUTE_API_KEY: [redacted]",
        id="name-single-quoted",
    ),
    pytest.param(
        "Authorization: Bearer FAKEbearer0000notreal0000example",
        "Authorization: Bearer [redacted]",
        id="authorization-bearer",
    ),
    pytest.param(
        "curl -H 'Bearer fakebearer0000notreal0000'",
        "curl -H 'Bearer [redacted]'",
        id="bare-bearer",
    ),
    pytest.param(
        f"OMNIROUTE_API_KEY: {FAKE_WORD}",
        "OMNIROUTE_API_KEY: [redacted]",
        id="name-colon-lowercase-word",
    ),
    pytest.param(f"api_key: {FAKE_WORD}", "api_key: [redacted]", id="api-key-lowercase-word"),
    # The diagnostic syntax with a secret in the value position and prose after
    # it. A redactor rule like "a value followed by prose is not a secret" would
    # let these through.
    pytest.param(
        f"field=OMNIROUTE_API_KEY: {FAKE_WORD} provider 'omniroute' declares api_key_env",
        "field=OMNIROUTE_API_KEY: [redacted] provider 'omniroute' declares api_key_env",
        id="field-label-lowercase-word-then-prose",
    ),
    pytest.param(
        "field=OMNIROUTE_API_KEY: sk-fake0000notreal0000example provider 'omniroute'",
        "field=OMNIROUTE_API_KEY: [redacted] provider 'omniroute'",
        id="field-label-sk-key-then-prose",
    ),
]


@pytest.mark.parametrize(("text", "expected"), SECRET_SHAPES)
def test_realistic_secret_shapes_stay_redacted(text: str, expected: str) -> None:
    assert redact_text(text) == expected
