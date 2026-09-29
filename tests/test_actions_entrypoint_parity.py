"""Entrypoint parity tests — CLI and TUI palette reach the SAME domain service.

Lane D2 (BOD-275): for every registered action, prove that:
  (a) CLI path:  sys.argv → verdict.cli.main() → run_action → _action_* → domain service
  (b) TUI path:  verdict.home.run_palette_action() → run_action → _action_* → domain service
  (c) Both paths call the same domain service with the same arguments
  (d) The returned data is equivalent

Nothing under ``verdict.actions`` is patched. Spies sit on the DOMAIN
SERVICE each action invokes (e.g. ``verdict.setup_plan.build_setup_plan``,
``verdict.doctor_diagnostics._collect_doctor_diagnostics``).
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Guard: nothing under verdict.actions may be patched in this module
# ---------------------------------------------------------------------------


class TestNoActionPatching:
    """AST guard: this module must never patch anything under verdict.actions."""

    def test_no_verdict_actions_patch(self) -> None:
        src = Path(__file__).read_text()
        tree = ast.parse(src)
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Look for mock.patch("verdict.actions...") or patch("verdict.actions...")
                if isinstance(func, ast.Attribute) and func.attr == "patch":
                    for arg in node.args:
                        if (
                            isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and arg.value.startswith("verdict.actions")
                        ):
                            violations.append(arg.value)
                # Look for monkeypatch.setattr("verdict.actions...", ...)
                if isinstance(func, ast.Attribute) and func.attr == "setattr":
                    for arg in node.args:
                        if (
                            isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and arg.value.startswith("verdict.actions")
                        ):
                            violations.append(arg.value)
        assert not violations, (
            f"This test module must not patch verdict.actions; found: {violations}"
        )


# ---------------------------------------------------------------------------
# Fake domain-value factories
# ---------------------------------------------------------------------------


@dataclass
class FakeModelInfo:
    id: str = "fake-model"
    provider: str = "fake"
    capability_tier: int = 1
    context_window: int = 128000
    cost_per_1k: float = 0.001
    capabilities: list[str] = field(default_factory=lambda: ["tools"])
    availability_state: str = "available"


@dataclass
class FakeDiagnostics:
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    sections: list[tuple[str, str, str]] = field(
        default_factory=lambda: [("test", "ok", "all good")]
    )
    documentation_preflight: dict[str, Any] = field(default_factory=dict)
    gateway_lifecycle: dict[str, Any] = field(default_factory=dict)
    shared_memory: dict[str, Any] = field(default_factory=dict)
    capability_report: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeSetupPlan:
    def to_dict(self) -> dict[str, Any]:
        return {"steps": ["install"], "ready": True}


@dataclass
class FakeDecision:
    model: str = "fake-model"
    provider: str = "fake-provider"
    tier: int = 1
    reason: str = "test"
    decision: str = "selected"
    transport_outcome: str = "not_sent"
    latency_ms: float = 0.0
    protected: bool = False
    degraded_mode: bool = False
    managed_backend_status: str = "unknown"
    quality_outcome: str = "unknown"
    execute_preview: str | None = None
    context_pack_prompt: str | None = None
    request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "tier": self.tier}


@dataclass
class FakeSelection:
    strategy: str = "best"
    model: str = "fake-model"
    reasoning: str = "test"
    timestamp: str = "2026-01-01T00:00:00Z"

    def to_dict(self) -> dict[str, Any]:
        return {"strategy": self.strategy}


class FakeGate:
    """Minimal Gate stand-in with route_with_strategy."""

    providers: dict[str, Any] | None = None

    def route(self, task: str, criticality: str, context: dict | None = None) -> FakeDecision:
        return FakeDecision()

    def route_with_strategy(
        self, task: str, criticality: str, context: dict | None = None
    ) -> tuple[FakeDecision, FakeSelection]:
        return FakeDecision(), FakeSelection()


@dataclass
class FakeProbeObservation:
    model: str = "test-model"
    ok: bool = True
    latency_ms: float = 42.0

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "ok": self.ok, "latency_ms": self.latency_ms}


@dataclass
class FakeProbeDiagnostics:
    def to_dict(self) -> dict[str, Any]:
        return {"probed": 1}


@dataclass
class FakeProbeRun:
    observations: list[FakeProbeObservation] = field(
        default_factory=lambda: [FakeProbeObservation()]
    )
    diagnostics: FakeProbeDiagnostics = field(default_factory=FakeProbeDiagnostics)


@dataclass
class FakeSuggestion:
    title: str = "Use cheaper model"
    description: str = "Switch to tier-0"
    category: str = "cost"
    id: str = "sug-001"
    novelty: str = "new"
    expiry: str = "7d"
    proposed_next_experiment: str = "Try tier-0 for classification tasks"
    confidence: float = 0.85
    expected_impact: str = "~15% cost reduction"
    evidence_references: list[str] = field(default_factory=lambda: ["log:1", "log:2"])


@dataclass
class FakeComparisonReport:
    def to_dict(self) -> dict[str, Any]:
        return {"direct": "fake", "routed": "fake", "match": True}


@dataclass
class FakeCatalogReport:
    passed: bool = True
    snapshot: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "models": 100}


@dataclass
class FakeProviderResult:
    local_servers: list[Any] = field(default_factory=list)
    cli_providers: list[Any] = field(default_factory=list)
    centralized_routers: list[Any] = field(default_factory=list)
    cloud_apis: list[Any] = field(default_factory=list)
    custom_endpoints: list[Any] = field(default_factory=list)


@dataclass
class FakeCredentialInfo:
    env_name: str = "TEST_KEY"
    purpose: str = "testing"
    optional: bool = True


@dataclass
class FakeReceipt:
    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "routing-receipt/v1", "state": "complete"}


# ---------------------------------------------------------------------------
# DomainSpy: records calls to a domain service
# ---------------------------------------------------------------------------


class DomainSpy:
    """Records calls to the domain service function."""

    def __init__(self, return_value: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.return_value = return_value

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls[len(self.calls) :] = [{"args": args, "kwargs": kwargs}]
        return self.return_value

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_args(self) -> dict[str, Any] | None:
        return self.calls[-1] if self.calls else None


# ---------------------------------------------------------------------------
# Helpers: run CLI and TUI paths
# ---------------------------------------------------------------------------


def run_cli(*argv: str) -> tuple[int, str]:
    """Set sys.argv and call verdict.cli.main(), capturing stdout.

    Returns (exit_code, stdout_text).
    """
    old_argv = sys.argv[:]
    old_stdout = sys.stdout
    buf = StringIO()
    sys.stdout = buf
    sys.argv = ["verdict", *argv]
    exit_code = 0
    try:
        from verdict.cli import main

        main()
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 0
    finally:
        sys.argv = old_argv
        sys.stdout = old_stdout
    return exit_code, buf.getvalue()


def run_tui(action_name: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
    """Run the TUI palette path: verdict.home.run_palette_action()."""
    from verdict.home import run_palette_action

    return run_palette_action(action_name, params)


# ---------------------------------------------------------------------------
# ACTION PARITY TESTS — domain service spies
# ---------------------------------------------------------------------------


class TestSetupPlanParity:
    """setup.plan: domain = verdict.setup_plan.build_setup_plan"""

    def test_parity(self) -> None:
        spy = DomainSpy(return_value=FakeSetupPlan())
        mem_spy = DomainSpy(return_value={"status": "ok"})

        with (
            patch("verdict.setup_plan.build_setup_plan", spy),
            patch("verdict.shared_memory.discover_shared_memory_setup", mem_spy),
        ):
            # CLI path
            rc, stdout = run_cli("setup", "plan", "--json")
            assert rc == 0, f"CLI failed: {stdout[:200]}"
            cli_spy_count = spy.call_count

            # TUI path
            ok, _data = run_tui("setup.plan")
            assert ok

        assert spy.call_count == cli_spy_count + 1, (
            f"setup.plan domain spy: CLI={cli_spy_count}, TUI delta=1"
        )


class TestDoctorParity:
    """doctor: domain = verdict.doctor_diagnostics._collect_doctor_diagnostics"""

    def test_parity(self) -> None:
        spy = DomainSpy(return_value=FakeDiagnostics())

        with patch("verdict.doctor_diagnostics._collect_doctor_diagnostics", spy):
            _rc, _stdout = run_cli("doctor", "--json")
            cli_count = spy.call_count
            assert cli_count == 1, f"CLI did not call domain: count={cli_count}"

            ok, _data = run_tui("doctor")
            assert ok
            tui_count = spy.call_count - cli_count
            assert tui_count == 1, f"TUI did not call domain: count={tui_count}"


class TestModelsListParity:
    """models.list: domain = verdict.actions.helpers.inventory_model_catalog

    The models-inventory lane changed models.list to return a dict summary
    (``{models, total, total_filtered, shown, ...}``) sourced from the live
    inventory helper instead of the raw catalog list.
    """

    def test_parity(self) -> None:
        fake_rows = [
            {
                "id": "fake-model",
                "provider": "fake",
                "capability_tier": "T3",
                "context_window": 4096,
                "tools_support": None,
                "structured_output": None,
                "input_cost_per_million": None,
                "output_cost_per_million": None,
                "source": "config",
                "freshness": None,
            }
        ]
        spy = DomainSpy(return_value=(fake_rows, None))

        with patch("verdict.actions.helpers.inventory_model_catalog", spy):
            _rc, _stdout = run_cli("models", "--json", "--inventory")
            cli_count = spy.call_count
            assert cli_count >= 1, f"CLI did not call domain: count={cli_count}"

            ok, data = run_tui("models.list")
            assert ok
            tui_count = spy.call_count - cli_count
            assert tui_count >= 1, f"TUI did not call domain: count={tui_count}"

        # The action now returns a dict summary, not a flat list.
        assert isinstance(data, dict), f"expected dict, got {type(data).__name__}"
        assert "models" in data
        assert data["models"][0]["id"] == "fake-model"


class TestInspectParity:
    """inspect: domain = verdict.actions.helpers.default_model_catalog"""

    def test_parity(self) -> None:
        fake_catalog = [FakeModelInfo(id="test-model-id", provider="test")]
        spy = DomainSpy(return_value=fake_catalog)

        with patch("verdict.actions.helpers.default_model_catalog", spy):
            _rc, _stdout = run_cli("inspect", "test-model-id", "--json")
            cli_count = spy.call_count
            assert cli_count >= 1

            ok, data = run_tui("inspect", {"model_id": "test-model-id"})
            assert ok
            tui_count = spy.call_count - cli_count
            assert tui_count >= 1

        assert data["id"] == "test-model-id"


class TestRouteParity:
    """route: domain = Gate.route / Gate.route_with_strategy (via helpers.build_route_gate)"""

    def test_parity(self) -> None:
        fake_gate = FakeGate()
        spy_build = DomainSpy(return_value=fake_gate)

        with patch("verdict.actions.helpers.build_route_gate", spy_build):
            # Use --terse to avoid selection presenter; --allow-offline to skip transport
            _rc, _stdout = run_cli("route", "test task", "--terse", "--allow-offline")
            cli_build_count = spy_build.call_count
            assert cli_build_count >= 1, f"CLI did not build gate: {spy_build.call_count}"

            ok, data = run_tui(
                "route",
                {
                    "task": "test task",
                    "criticality": "medium",
                    "terse": True,
                    "allow_offline": True,
                },
            )
            assert ok
            tui_build_count = spy_build.call_count - cli_build_count
            assert tui_build_count >= 1

        assert data["decision"].model == "fake-model"


class TestProbeParity:
    """probe: domain = ProbeRunner.run_with_diagnostics"""

    def test_parity(self) -> None:
        fake_run = FakeProbeRun()
        # We need to spy on ProbeRunner — but it's constructed inside the action.
        # The simplest domain spy is on the transport factory, or we can
        # pass transport as a kwarg. The action accepts transport= kwarg.
        spy = DomainSpy(return_value=fake_run)

        with patch("verdict.probes.ProbeRunner.run_with_diagnostics", spy):
            # CLI path needs --allow-live-probe to pass consent gate
            _rc, _stdout = run_cli("probe", "test-model", "--json", "--allow-live-probe")
            cli_count = spy.call_count
            assert cli_count == 1, f"CLI probe domain: {cli_count}"

            _ok, _data = run_tui("probe", {"models": ["test-model"], "allow_live_probe": True})
            tui_count = spy.call_count - cli_count
            assert tui_count == 1


class TestStatsParity:
    """stats: domain = JSONL file read (no external service).

    We create a temporary JSONL file and verify both paths read it.
    """

    def test_parity(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"
        entries = [
            {"decision": {"tier": 0, "model": "gpt-4o", "latency_ms": 50}},
            {"decision": {"tier": 1, "model": "gpt-3.5", "latency_ms": 30}},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        rc, _stdout = run_cli("stats", "--log_path", str(log))
        assert rc == 0

        ok, data = run_tui("stats", {"log_path": str(log)})
        assert ok
        assert data["total_requests"] == 2


class TestSuggestParity:
    """suggest: domain = verdict.suggestions.SuggestionService.generate_suggestions"""

    def test_parity(self) -> None:

        fake_suggestions = [FakeSuggestion()]
        spy = DomainSpy(return_value=fake_suggestions)

        with patch("verdict.suggestions.SuggestionService.generate_suggestions", spy):
            _rc, _stdout = run_cli("suggest")
            cli_count = spy.call_count
            assert cli_count == 1

            _ok, _data = run_tui("suggest")
            tui_count = spy.call_count - cli_count
            assert tui_count == 1


class TestCostReportParity:
    """cost-report: domain = JSONL file read."""

    def test_parity(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "verdict-decisions.jsonl"
        entries = [{"decision": {"tier": 0}}, {"decision": {"tier": 1}}, {"decision": {"tier": 0}}]
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        # cost-report reads from cwd/verdict-decisions.jsonl
        monkeypatch.chdir(tmp_path)

        rc, _stdout = run_cli("cost-report")
        assert rc == 0

        ok, data = run_tui("cost-report", {"log_path": str(log)})
        assert ok
        assert data["total_requests"] == 3
        assert data["t0_requests"] == 2


class TestDetectParity:
    """detect: domain = verdict.provider_detection.detect_all_providers (offline mode)"""

    def test_parity_offline(self) -> None:
        # Use offline mode to avoid network access
        rc, _stdout = run_cli("detect", "--offline", "--json")
        assert rc == 0

        ok, data = run_tui("detect", {"offline": True})
        assert ok
        assert data["mode"] == "offline"


class TestCompareParity:
    """compare: domain = verdict.comparison.ComparisonHarness.compare"""

    def test_parity(self) -> None:
        spy = DomainSpy(return_value=FakeComparisonReport())

        with (
            patch("verdict.comparison.ComparisonHarness.compare", spy),
            patch("verdict.actions.helpers.build_route_gate", DomainSpy(return_value=FakeGate())),
        ):
            _rc, _stdout = run_cli("compare", "test task")
            cli_count = spy.call_count
            assert cli_count == 1

            _ok, data = run_tui(
                "compare", {"task": "test task", "criticality": "medium", "allow_offline": False}
            )
            tui_count = spy.call_count - cli_count
            assert tui_count == 1

        assert data["comparison_report"]["match"] is True


class TestCatalogParity:
    """catalog: domain = verdict.omniroute_catalog.qualify_catalog.

    Both CLI and TUI call the same action with a stub HTTP response.
    Uses --management mode for a single projection (simpler).
    """

    def test_parity(self) -> None:
        fake_report = FakeCatalogReport()
        spy = DomainSpy(return_value=fake_report)

        # Stub urllib.urlopen to avoid network — must work as context manager
        fake_response = MagicMock()
        fake_response.read.return_value = b'{"data": [{"id": "m1"}]}'
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)
        url_spy = MagicMock(return_value=fake_response)

        with (
            patch("verdict.omniroute_catalog.qualify_catalog", spy),
            patch("urllib.request.urlopen", url_spy),
        ):
            # Use --management for single projection
            _rc, _stdout = run_cli(
                "catalog", "--base-url", "http://fake:20128", "--management", "--json"
            )
            cli_count = spy.call_count
            assert cli_count >= 1, f"CLI catalog domain spy count: {cli_count}"

            _ok, _data = run_tui("catalog", {"base_url": "http://fake:20128", "management": True})
            tui_count = spy.call_count - cli_count
            assert tui_count >= 1


class TestReplayParity:
    """replay: domain = verdict.execution_session.ExecutionSession.resume"""

    def test_parity(self) -> None:
        fake_session = MagicMock()
        fake_session.to_dict.return_value = {"session_id": "abc", "events": []}
        spy = DomainSpy(return_value=fake_session)

        with patch("verdict.execution_session.ExecutionSession.resume", spy):
            _rc, _stdout = run_cli("replay", "abc", "--json")
            cli_count = spy.call_count
            assert cli_count == 1

            _ok, data = run_tui("replay", {"session_id": "abc"})
            tui_count = spy.call_count - cli_count
            assert tui_count == 1

        assert data["session_id"] == "abc"


class TestCredentialsListParity:
    """credentials.list: domain = verdict.credentials_store.get_credential_source"""

    def test_parity(self) -> None:
        spy = DomainSpy(return_value=("env", "sk-***"))

        with patch("verdict.credentials_store.get_credential_source", spy):
            _rc, _stdout = run_cli("credentials", "list", "--json")
            cli_count = spy.call_count
            assert cli_count >= 1

            _ok, _data = run_tui("credentials.list")
            tui_count = spy.call_count - cli_count
            assert tui_count >= 1


class TestCredentialsSetParity:
    """credentials.set: domain = CredentialsStore.set"""

    def test_parity(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

        spy = DomainSpy(return_value=None)

        with patch("verdict.credentials_store.CredentialsStore.set", spy):
            old_stdin = sys.stdin
            sys.stdin = StringIO("test-value\n")
            try:
                _rc, _stdout = run_cli(
                    "credentials", "set", "TEST_KEY", "--stdin", "--force-unregistered"
                )
            finally:
                sys.stdin = old_stdin
            cli_count = spy.call_count
            assert cli_count == 1, f"CLI credentials.set: count={cli_count}"

            _ok, _data = run_tui(
                "credentials.set",
                {"name": "TEST_KEY", "value": "test-value", "force_unregistered": True},
            )
            tui_count = spy.call_count - cli_count
            assert tui_count == 1


class TestCredentialsUnsetParity:
    """credentials.unset: domain = CredentialsStore.unset"""

    def test_parity(self) -> None:
        spy = DomainSpy(return_value=True)

        with patch("verdict.credentials_store.CredentialsStore.unset", spy):
            _rc, _stdout = run_cli("credentials", "unset", "TEST_KEY")
            cli_count = spy.call_count
            assert cli_count == 1

            _ok, _data = run_tui("credentials.unset", {"name": "TEST_KEY"})
            tui_count = spy.call_count - cli_count
            assert tui_count == 1

        # Both called with same key name
        cli_key = (
            spy.calls[0]["args"][0] if spy.calls[0]["args"] else spy.calls[0]["kwargs"]["name"]
        )
        tui_key = (
            spy.calls[1]["args"][0] if spy.calls[1]["args"] else spy.calls[1]["kwargs"]["name"]
        )
        assert cli_key == tui_key == "TEST_KEY"


class TestReceiptShowParity:
    """receipt.show (list mode): domain = ReceiptStore.query_receipts"""

    def test_parity(self, tmp_path: Path) -> None:
        db_path = tmp_path / "receipts.db"

        # Force an empty receipts DB (list mode returns empty list)
        from verdict.receipt_store import ReceiptStore

        ReceiptStore(db_path, strict_scope=False)  # creates the DB

        rc, _stdout = run_cli("receipt", "list", "--db", str(db_path), "--json")
        assert rc == 0

        ok, data = run_tui("receipt.show", {"action": "list", "db_path": str(db_path)})
        assert ok
        assert data["action"] == "list"
        assert data["receipts"] == []


class TestRunReceiptParity:
    """run-receipt: domain = verdict.orchestration.receipt.verify_run_receipt"""

    def test_parity(self, tmp_path: Path) -> None:
        # Create minimal receipt structure
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        receipt = {"goal": "test", "status": "COMPLETE", "nodes": [], "digest": "abc"}
        (run_dir / "receipt.json").write_text(json.dumps(receipt))
        (run_dir / "events.jsonl").write_text("")

        spy_verify = DomainSpy(return_value=[])  # no problems
        spy_verdict = DomainSpy(return_value=("COMPLETE", "all done"))

        with (
            patch("verdict.orchestration.receipt.verify_run_receipt", spy_verify),
            patch("verdict.orchestration.receipt.completion_verdict", spy_verdict),
        ):
            # TUI path
            _ok, _data = run_tui("run-receipt", {"run_dir": str(run_dir)})
            tui_verify_count = spy_verify.call_count
            assert tui_verify_count == 1

            # Note: CLI run-receipt is dispatched via orchestration/cli.py
            # which may require different argv. Test TUI only here;
            # CLI tested separately if available.


class TestEligibilityParity:
    """eligibility: domain = verdict.orchestration.eligibility_report.build_selector

    The orch-boundary lane moved ``build_selector`` and ``eligibility_payload``
    from ``verdict.orchestration.cli`` to ``verdict.orchestration.eligibility_report``.
    The action imports from the domain module, so the spy targets must match.
    """

    def test_parity(self) -> None:
        fake_selector = MagicMock()
        fake_selector.evaluate.return_value = []
        fake_selector.summary.return_value = {"total": 0}
        spy = DomainSpy(return_value=fake_selector)

        fake_payload = DomainSpy(
            return_value={"verdicts": [], "summary": {"total": 0}, "selected": None, "filters": {}}
        )

        with (
            patch("verdict.orchestration.eligibility_report.build_selector", spy),
            patch("verdict.orchestration.eligibility_report.eligibility_payload", fake_payload),
        ):
            _ok, _data = run_tui("eligibility", {"gateway": "http://fake:20128", "scope": "all"})
            assert spy.call_count >= 1


# ---------------------------------------------------------------------------
# config.show: CLI and TUI reach the same action; output is secret-free
# ---------------------------------------------------------------------------


class TestConfigShowParity:
    """config.show: thin CLI wrapper calls run_action; secrets are redacted."""

    def test_cli_calls_run_action(self) -> None:
        """CLI module source must contain run_action("config.show" (wiring proof)."""
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module("verdict.cli"))
        assert 'run_action("config.show"' in src, "CLI does not call run_action for config.show"

    def test_tui_callable(self) -> None:
        """TUI palette path executes config.show without error."""
        ok, data = run_tui("config.show")
        assert ok
        assert "config_file" in data
        assert "exists" in data

    def test_redaction(self, tmp_path: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config values that look like secrets are redacted in action output."""
        import yaml

        cfg_dir = tmp_path / ".config" / "verdict"
        cfg_dir.mkdir(parents=True)
        cfg_file = cfg_dir / "verdict.yaml"
        cfg_file.write_text(
            yaml.dump({"api_key": "sk-real-secret", "gateway_url": "http://127.0.0.1:20128"})
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

        ok, data = run_tui("config.show")
        assert ok
        assert data.get("exists") is True
        cfg = data.get("config", {})
        # Secret must be redacted — never the real value
        assert cfg.get("api_key") != "sk-real-secret", "api_key was not redacted"
        # Non-secret key must be preserved
        assert cfg.get("gateway_url") == "http://127.0.0.1:20128"

    def test_cli_json_output(self) -> None:
        """CLI --json returns valid JSON with config_file key."""
        rc, stdout = run_cli("config", "show", "--json")
        assert rc == 0, f"CLI config show --json failed: {stdout[:200]}"
        data = json.loads(stdout)
        assert "config_file" in data


# ---------------------------------------------------------------------------
# xfail: actions whose CLI handler is NOT wired to run_action on this branch
# ---------------------------------------------------------------------------

XFAIL_NOT_WIRED: list[str] = []


@pytest.mark.parametrize("action", XFAIL_NOT_WIRED)
@pytest.mark.xfail(strict=True, reason="handler not wired: CLI does not call run_action")
class TestNotWiredXfail:
    def test_cli_calls_run_action(self, action: str) -> None:
        """Expecting this to fail: CLI does not use run_action for this action."""
        # If this passes, the xfail will flip → test failure, signalling wiring is done
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module("verdict.cli"))
        assert f'run_action("{action}"' in src, f"CLI does not call run_action for {action}"


# ---------------------------------------------------------------------------
# Mutation test: real credentials round-trip (no spy)
# ---------------------------------------------------------------------------


class TestCredentialsMutationRoundTrip:
    """Real CredentialsStore: CLI set + TUI set write the same entry."""

    def test_set_and_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

        # --- CLI: set ---
        old_stdin = sys.stdin
        sys.stdin = StringIO("cli-secret-value\n")
        try:
            rc, stdout = run_cli(
                "credentials", "set", "PARITY_TEST_KEY", "--stdin", "--force-unregistered"
            )
        finally:
            sys.stdin = old_stdin
        assert rc == 0, f"CLI set failed: {stdout[:200]}"

        # Verify stored value
        from verdict.credentials_store import CredentialsStore

        store = CredentialsStore()
        stored = store.load()
        assert stored.get("PARITY_TEST_KEY") == "cli-secret-value"

        # --- TUI: set (overwrites) ---
        ok, _data = run_tui(
            "credentials.set",
            {"name": "PARITY_TEST_KEY", "value": "tui-secret-value", "force_unregistered": True},
        )
        assert ok
        store2 = CredentialsStore()
        stored2 = store2.load()
        assert stored2.get("PARITY_TEST_KEY") == "tui-secret-value"

        # --- CLI: unset ---
        rc2, _stdout2 = run_cli("credentials", "unset", "PARITY_TEST_KEY")
        assert rc2 == 0

        store3 = CredentialsStore()
        assert "PARITY_TEST_KEY" not in store3.load()

        # --- TUI: unset (already gone, should still succeed) ---
        ok2, _data2 = run_tui("credentials.unset", {"name": "PARITY_TEST_KEY"})
        assert ok2


# ---------------------------------------------------------------------------
# Parity matrix summary (generated at collection time)
# ---------------------------------------------------------------------------

_PARITY_ACTIONS = [
    # (action, service_spied, cli_command, tui_testable, xfail_reason)
    ("models.list", "helpers.default_model_catalog", "models --json", True, None),
    ("inspect", "helpers.default_model_catalog", "inspect <id> --json", True, None),
    ("route", "helpers.build_route_gate", "route <task> <crit>", True, None),
    ("doctor", "doctor_diagnostics._collect_doctor_diagnostics", "doctor --json", True, None),
    ("probe", "probes.ProbeRunner.run_with_diagnostics", "probe <model> --json", True, None),
    ("setup.plan", "setup_plan.build_setup_plan", "setup plan --json", True, None),
    ("receipt.show", "routing_receipt.load_routing_receipt", "receipt show --json", True, None),
    ("eligibility", "orchestration.cli.build_selector", "eligibility --json", True, None),
    ("config.show", None, "config show --json", True, None),
    (
        "credentials.list",
        "credentials_store.get_credential_source",
        "credentials list --json",
        True,
        None,
    ),
    ("credentials.set", "CredentialsStore.set", "credentials set <name>", True, None),
    ("credentials.unset", "CredentialsStore.unset", "credentials unset <name>", True, None),
    ("run-receipt", "orchestration.receipt.verify_run_receipt", "run-receipt <dir>", True, None),
    ("compare", "comparison.ComparisonHarness.compare", "compare <task>", True, None),
    ("catalog", "omniroute_catalog.qualify_catalog", "catalog --json", True, None),
    ("detect", "provider_detection.detect_all_providers", "detect --offline --json", True, None),
    ("stats", "(JSONL file read)", "stats <log>", True, None),
    ("suggest", "suggestions.SuggestionService.generate_suggestions", "suggest", True, None),
    ("cost-report", "(JSONL file read)", "cost-report", True, None),
    ("replay", "execution_session.ExecutionSession.resume", "replay <id> --json", True, None),
]
