"""A credential *name* in a diagnostic line is not a credential.

``scripts/check_dod.py`` refuses a raw-output artifact when
``verdict.security.redact_text`` would change it. Verdict's own
``credential_env_missing`` note used to print
``field=OMNIROUTE_API_KEY: provider 'omniroute' ...``. The ``key: value`` label
pattern reads that as a key named ``OMNIROUTE_API_KEY`` holding the value
``provider``, so a gate log that holds no credential was refused.

The fix is in the emitter, not in the redactor. ``NAME: <one word>`` is also
how a real key is written (YAML, ``.env`` dumps), and a long lowercase word
cannot be told apart from an English word without guessing. So the first
group of tests pins how realistic secrets in that same syntax are redacted
today, and must hold before and after the fix. The second group pins that the
lines Verdict renders pass through the redactor unchanged.

Offline only. Every value below is fake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from verdict.actions.helpers import _report_bootstrap_notes
from verdict.provider_bootstrap import BootstrapDiagnostic, resolve_provider_bootstrap
from verdict.proxy import UpstreamProxy
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


# --- Verdict's own diagnostic lines survive the redactor ---------------------

#: The ``credential_env_missing`` note, as ``verdict route`` prints it.
MISSING_KEY_LINE = (
    "verdict: credential_env_missing [configuration] field=OMNIROUTE_API_KEY - "
    "provider 'omniroute' declares api_key_env OMNIROUTE_API_KEY but it is unset in the "
    "environment and in the credential store; remediation: export OMNIROUTE_API_KEY, "
    "or run 'verdict credentials set OMNIROUTE_API_KEY'"
)


def _unset_gateway_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OMNIROUTE_API_KEY", "VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_MANAGEMENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _missing_key_notes(tmp_path: Path) -> tuple[BootstrapDiagnostic, ...]:
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )
    return result.notes()


def test_credential_env_missing_note_renders_without_a_name_colon_label(tmp_path: Path) -> None:
    note = next(n for n in _missing_key_notes(tmp_path) if n.code == "credential_env_missing")

    line = f"verdict: {note.describe()}"

    assert line == MISSING_KEY_LINE
    assert redact_text(line) == line


def test_every_bootstrap_note_line_survives_the_redactor(tmp_path: Path) -> None:
    for note in _missing_key_notes(tmp_path):
        line = note.describe()
        assert redact_text(line) == line, line


def test_route_stderr_note_survives_the_redactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The emitter ``verdict route`` uses for bootstrap notes."""
    _unset_gateway_credentials(monkeypatch)
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )

    _report_bootstrap_notes(result)

    err = capsys.readouterr().err
    assert MISSING_KEY_LINE in err.splitlines()
    assert redact_text(err) == err


def test_serve_startup_lines_survive_the_redactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``verdict serve`` prints its startup diagnostics with the same format.

    Two of them carry a credential name in the field position: the missing
    gateway key and the missing management token.
    """
    _unset_gateway_credentials(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-token-for-tests")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:20128")
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with TestClient(api.app):
        pass

    lines = [line for line in capsys.readouterr().err.splitlines() if "[configuration]" in line]
    fields = {match.group(1) for line in lines if (match := re.search(r" field=([\w.]+)", line))}
    assert {"OMNIROUTE_API_KEY", "OMNIROUTE_MANAGEMENT_TOKEN"} <= fields, lines
    for line in lines:
        assert line.startswith("verdict serve: ")
        assert redact_text(line) == line, line
