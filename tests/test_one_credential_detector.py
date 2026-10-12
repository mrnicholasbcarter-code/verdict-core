"""Both redactors catch the same credentials (release review S3)."""

from __future__ import annotations

import pytest

from verdict.orchestration.worker_context import _redact_worker_context_secrets
from verdict.security import redact_text

SECRETS = [
    'key = "sk-ant-api03-AbCdEf0123456789xyzXYZ"',
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc123def456",
    "Bearer abcdefghijklmnopqrst",
    "curl https://user:pa55word@example.com/x",
    "export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123",
    "aws AKIAABCDEFGHIJKLMNOP",
    "google AIzaSyA1234567890abcdefghijklmnopqrstu",
    "slack xoxb-1234567890-abcdefghij",
    "access_token: 'q8Zr2LpX9vT4mN7kB1cY'",
    "refresh_token=9f8e7d6c5b4a3210fedcba98",
    'password = "hunter2hunter2hunter2"',
    'client_secret: "Zx81Kp20Lq99Mm77Nn55"',
    "x-api-key: 0123456789abcdef0123456789abcdef",
    "secret_key_base: 3f9a8b7c6d5e4f30a1b2c3d4e5f60718",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
]


@pytest.mark.parametrize("text", SECRETS)
def test_both_redactors_catch_the_same_credentials(text: str) -> None:
    assert redact_text(text) != text
    assert _redact_worker_context_secrets(text) != text


@pytest.mark.parametrize(
    "code",
    [
        "token: str = None",
        "api_key = os.environ['OPENAI_API_KEY']",
        'password = "changeme"',
        "max_tokens = 4096",
        "token_budget = estimate_tokens(text)",
        "num_tokens = 12345678901234567",
    ],
)
def test_worker_context_keeps_source_code_that_holds_no_secret(code: str) -> None:
    """Source code is read by workers; placeholders and lookups are not secrets."""
    assert _redact_worker_context_secrets(code) == code
