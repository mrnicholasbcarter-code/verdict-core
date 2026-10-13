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
    "omniroute " + "oma_" + "live_" + "Xk3vQ9rT2mWp7LzN4bYc_8hJd-Qa1Ws2Ed3Rf4Tg5Y6",
    "letters only " + "oma_" + "live_" + "XkvQrTmWpLzNbYchJdQaWsEdRfTgYhUjIkOlPzXcVbNm"[:43],
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA"
    + "q1W2e3R4" * 8
    + "\n-----END RSA PRIVATE KEY-----",
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
        "token_count = 123",
        "password_hash_rounds = 12",
    ],
)
def test_worker_context_keeps_source_code_that_holds_no_secret(code: str) -> None:
    """Source code is read by workers; placeholders and lookups are not secrets."""
    assert _redact_worker_context_secrets(code) == code


@pytest.mark.parametrize(
    "text", ["token_count=123", "password_hash_rounds=12", "token_limit: 4096"]
)
def test_diagnostics_keep_counters_that_mention_tokens(text: str) -> None:
    assert redact_text(text) == text


def test_an_authorization_bearer_value_is_always_redacted() -> None:
    text = "Authorization: Bearer abcdefghijklmno"
    assert redact_text(text) != text and _redact_worker_context_secrets(text) != text


def test_pem_private_key_body_is_removed_and_text_after_it_kept() -> None:
    body = "MIIEowIBAAKCAQEA" + "q1W2e3R4" * 8
    text = f"key:\n-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\nnext line"
    assert redact_text(text) == "key:\n-----BEGIN PRIVATE KEY-----[redacted]\nnext line"
    assert body not in _redact_worker_context_secrets(text)


@pytest.mark.parametrize(
    "text",
    [
        "oma_live_session_handler_name",  # identifier, not 43 characters
        "oma_live_session_handler_name_v2",
        "-----BEGIN PRIVATE KEY-----",  # a header alone carries no key
        r"re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')",
    ],
)
def test_token_and_pem_shapes_leave_non_secrets(text: str) -> None:
    assert redact_text(text) == text
