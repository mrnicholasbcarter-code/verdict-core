"""A credential *name* in a diagnostic line is not a credential.

``scripts/check_dod.py`` refuses a raw-output artifact when
``verdict.security.redact_text`` would change it. Verdict's own
``credential_env_missing`` note used to print
``field=OMNIROUTE_API_KEY: provider 'omniroute' ...``. The ``key: value`` label
pattern reads that as a key named ``OMNIROUTE_API_KEY`` holding the value
``provider``, so a gate log that holds no credential was refused.

The fix is in the emitter, not in the redactor. ``NAME: <one word>`` is also
how a real key is written (YAML, ``.env`` dumps), and a long lowercase word
cannot be told apart from an English word without guessing. The groups below:

* realistic secrets in that syntax stay redacted, before and after the fix;
* the lines Verdict renders pass through the redactor unchanged;
* ``describe()`` redacts secret shapes that reach its free text, which the old
  ``NAME: detail`` layout only caught by accident;
* every emitter that prints these lines, through its own seam, uses that format.

``tests/test_bootstrap_diagnostics_carry_no_credential_values.py`` holds the
structural guarantee: no detail is built from a credential value at all.

Offline only. Every value below is fake.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

import verdict.api as api
import verdict.provider_bootstrap as pb
from verdict.actions.helpers import _report_bootstrap_notes
from verdict.credentials_store import GATEWAY_KEY_ENV_NAMES
from verdict.provider_bootstrap import (
    BootstrapDiagnostic,
    BootstrapError,
    BootstrapResult,
    describe_bootstrap_failure,
    resolve_provider_bootstrap,
)
from verdict.proxy import UpstreamProxy
from verdict.security import redact_text

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test here may read the operator's credential store, key file or config."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    for name in (
        *GATEWAY_KEY_ENV_NAMES,
        "OMNIROUTE_MANAGEMENT_TOKEN",
        "OMNIROUTE_BASE_URL",
        "LLMGATE_UPSTREAM_BASE_URL",
        "LLMGATE_PRIMARY",
        "VERDICT_PRIME_TARGET_MAP",
        "PRIME_AGENT_HOME",
        "PRIME_HOME",
        "PRIME_AGENT_CODING_AGENT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


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


def _missing_key_result(tmp_path: Path) -> BootstrapResult:
    return resolve_provider_bootstrap(
        env={
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128",
            "LLMGATE_LOG_PATH": str(tmp_path / "decisions.jsonl"),
        },
        config_path=tmp_path / "absent.yaml",
        credential_store_env={},
    )


def test_credential_env_missing_note_renders_without_a_name_colon_label(tmp_path: Path) -> None:
    result = _missing_key_result(tmp_path)
    note = next(n for n in result.notes() if n.code == "credential_env_missing")

    line = f"verdict: {note.describe()}"

    assert line == MISSING_KEY_LINE
    assert redact_text(line) == line


def test_every_bootstrap_note_line_survives_the_redactor(tmp_path: Path) -> None:
    for note in _missing_key_result(tmp_path).notes():
        line = note.describe()
        assert redact_text(line) == line, line


def test_route_stderr_note_survives_the_redactor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The emitter ``verdict route`` uses for bootstrap notes."""
    _report_bootstrap_notes(_missing_key_result(tmp_path))

    err = capsys.readouterr().err
    assert MISSING_KEY_LINE in err.splitlines()
    assert redact_text(err) == err


def test_serve_startup_lines_survive_the_redactor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``verdict serve`` prints its startup diagnostics with the same format.

    Two of them carry a credential name in the field position: the missing
    gateway key and the missing management token.
    """
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


# --- describe() redacts secret shapes in its free text -----------------------

FAKE_SK = "sk-fake0000notreal0000example"
FAKE_BEARER = "FAKEbearer0000notreal0000example"
FAKE_TOKEN = "Fq7ZxK2mPw9LtR4vNb6YcH3sJd8GhE5u"
LEAKED = (FAKE_SK, FAKE_BEARER, FAKE_TOKEN)


def _leaky_diagnostic(*, fatal: bool = True) -> BootstrapDiagnostic:
    """A diagnostic whose free text holds secret shapes, as a careless detail could."""
    return BootstrapDiagnostic(
        code="gateway_unreachable",
        diagnostic_class="gateway_health",
        field=f"providers.{FAKE_SK}",
        source="environment",
        detail=f"gateway answered Authorization: Bearer {FAKE_BEARER} for key {FAKE_SK}",
        remediation=f"retry with token={FAKE_TOKEN}",
        fatal=fatal,
    )


def test_describe_redacts_secret_shapes_in_field_detail_and_remediation() -> None:
    """Under the old ``field=NAME: detail`` layout the label pattern could catch a
    value at the start of a detail by accident. ``describe()`` now redacts its
    free text itself, part by part, so that is a guarantee instead of luck."""
    line = _leaky_diagnostic().describe()

    assert line == (
        "gateway_unreachable [gateway_health] field=providers.[redacted] source=environment - "
        "gateway answered Authorization: Bearer [redacted] for key [redacted]; "
        "remediation: retry with token=[redacted]"
    )
    assert redact_text(line) == line


def test_describe_keeps_the_documented_userinfo_marker() -> None:
    """URL userinfo renders as ``***:***@`` (docs/CONFIGURATION.md), raw or pre-redacted."""
    line = BootstrapDiagnostic(
        code="precedence_conflict",
        diagnostic_class="configuration",
        field="providers.omniroute.base_url",
        detail=(
            "config file sets 'http://***:***@127.0.0.1:20128/v1' while OMNIROUTE_BASE_URL "
            f"sets 'http://fakeuser:{FAKE_WORD}@127.0.0.1:20129/v1'"
        ),
        remediation="align OMNIROUTE_BASE_URL with the config file",
    ).describe()

    assert FAKE_WORD not in line
    assert line.count("***:***@") == 2
    assert "[redacted]@" not in line


@pytest.mark.parametrize("value", [FAKE_WORD, FAKE_TOKEN], ids=["lowercase-word", "mixed-case"])
def test_limitation_an_unlabelled_value_with_no_known_shape_is_not_caught(value: str) -> None:
    """Limitation, pinned so it stays visible: redaction needs a label or a known
    prefix. A bare random string in a detail is printed as is. The guarantee for
    such values is structural: no detail is built from a credential value
    (``tests/test_bootstrap_diagnostics_carry_no_credential_values.py``)."""
    line = BootstrapDiagnostic(
        code="gateway_unreachable",
        diagnostic_class="gateway_health",
        field="gateway_url",
        detail=f"gateway answered {value}",
        remediation="start the gateway",
    ).describe()

    assert value in line


# --- every emitter, through its own seam --------------------------------------

#: A refusal that names a credential in the field position. No public path
#: refuses on a credential name today, so the emitters are fed one directly.
CREDENTIAL_NAME_DIAGNOSTIC = BootstrapDiagnostic(
    code="credential_env_missing",
    diagnostic_class="configuration",
    field="OMNIROUTE_API_KEY",
    detail=(
        "provider 'omniroute' declares api_key_env OMNIROUTE_API_KEY but it is unset in the "
        "environment and in the credential store"
    ),
    remediation="export OMNIROUTE_API_KEY, or run 'verdict credentials set OMNIROUTE_API_KEY'",
)


def _refusal() -> BootstrapError:
    return BootstrapError(
        "bootstrap_configuration_invalid", (CREDENTIAL_NAME_DIAGNOSTIC, _leaky_diagnostic())
    )


def _result_with_notes(tmp_path: Path) -> BootstrapResult:
    result = _missing_key_result(tmp_path)
    assert "credential_env_missing" in {note.code for note in result.notes()}
    return dataclasses.replace(
        result, diagnostics=(*result.diagnostics, _leaky_diagnostic(fatal=False))
    )


def _resolver_returns(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    """Every emitter imports the resolver at call time, so this reaches all of them."""

    def fake_resolver(**_: object) -> object:
        if isinstance(outcome, BootstrapError):
            raise outcome
        return outcome

    monkeypatch.setattr(pb, "resolve_provider_bootstrap", fake_resolver)


def _supervisor_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "prime_supervisor_redaction", ROOT / "scripts" / "prime_supervisor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Emitter = Callable[[Path, pytest.MonkeyPatch, pytest.CaptureFixture[str]], str]


def _describe_bootstrap_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    return describe_bootstrap_failure(_refusal())


def _bootstrap_error_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    return str(_refusal())


def _route_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    from verdict.cli import _build_route_gate

    _resolver_returns(monkeypatch, _refusal())
    with pytest.raises(SystemExit):
        _build_route_gate(allow_offline=True)
    return capsys.readouterr().err


def _route_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    from verdict.cli import _build_route_gate

    _resolver_returns(monkeypatch, _result_with_notes(tmp_path))
    _build_route_gate(allow_offline=True)
    return capsys.readouterr().err


def _library_gate_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    from verdict.gate import resolve_default_providers

    _resolver_returns(monkeypatch, _result_with_notes(tmp_path))
    resolve_default_providers(allow_offline=True)
    return capsys.readouterr().err


def _boot_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-token-for-tests")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:20128")
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))
    with TestClient(api.app):
        pass


def _serve_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    _resolver_returns(monkeypatch, _refusal())
    _boot_serve(monkeypatch)
    return capsys.readouterr().err


def _serve_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    _resolver_returns(monkeypatch, _result_with_notes(tmp_path))
    _boot_serve(monkeypatch)
    return capsys.readouterr().err


def _serve_precedence_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    real = api._serve_field_resolution

    def with_conflicts(bootstrap: object) -> tuple[dict[str, str], list[BootstrapDiagnostic]]:
        values, _ = real(bootstrap)
        notes = [
            dataclasses.replace(CREDENTIAL_NAME_DIAGNOSTIC, fatal=False),
            _leaky_diagnostic(fatal=False),
        ]
        return values, notes

    monkeypatch.setattr(api, "_serve_field_resolution", with_conflicts)
    api._build_intelligence()
    return capsys.readouterr().err


def _supervisor_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    supervisor = _supervisor_module()
    _resolver_returns(monkeypatch, _refusal())
    with pytest.raises(supervisor.ControllerLaunchError) as excinfo:
        supervisor._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)
    return f"{excinfo.value.detail}\n{excinfo.value}"


def _supervisor_live_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    from verdict.admission import AdmissionUnavailableError

    supervisor = _supervisor_module()
    _resolver_returns(monkeypatch, _refusal())
    with pytest.raises(AdmissionUnavailableError) as excinfo:
        supervisor._live_admission_gateway()
    return f"{excinfo.value.detail}\n{excinfo.value}"


EMITTERS: list[Emitter] = [
    _describe_bootstrap_failure,
    _bootstrap_error_message,
    _route_refusal,
    _route_notes,
    _library_gate_notes,
    _serve_refusal,
    _serve_notes,
    _serve_precedence_conflicts,
    _supervisor_factory,
    _supervisor_live_admission,
]


@pytest.mark.parametrize("emitter", EMITTERS, ids=lambda emitter: emitter.__name__.strip("_"))
def test_every_emitter_renders_names_safely_and_redacts_free_text(
    emitter: Emitter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = emitter(tmp_path, monkeypatch, capsys)

    assert "field=OMNIROUTE_API_KEY - " in text, text
    assert "field=OMNIROUTE_API_KEY:" not in text
    for secret in LEAKED:
        assert secret not in text, secret
    assert redact_text(text) == text
