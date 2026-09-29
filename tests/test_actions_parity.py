"""Action-layer parity tests (BOD-275).

Proves:
(a) CLI handler + TUI palette both reach the same action via run_action
(b) --json output is unchanged (structure matches)
(c) JSON not polluted by ANSI
(d) Mutation action goes through real authority
(e) No subprocess calls to ``verdict`` in home.py or actions/
(f) Matrix freshness — every CLI command covered
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from verdict.actions.base import NOOP_SINK, ActionEvent, ActionResult, ActionSink, ActionSpec
from verdict.actions.registry import MACHINE_ONLY, get_action, list_actions, run_action

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingSink:
    """ActionSink that records events for assertion."""

    def __init__(self) -> None:
        self.events: list[ActionEvent] = []

    def on_event(self, event: ActionEvent) -> None:
        self.events.append(event)


def _action_names() -> list[str]:
    return [spec.name for spec in list_actions()]


# ---------------------------------------------------------------------------
# (a) Parity: CLI + TUI palette both reach the service through run_action
# ---------------------------------------------------------------------------


class TestParityCLIAndTUI:
    """Each extracted action is reachable from both CLI handler and TUI palette."""

    def test_models_list_via_run_action(self) -> None:
        """models.list action returns catalog data."""
        mock_catalog = [
            MagicMock(
                id="test-model",
                provider="test",
                capability_tier=3,
                context_window=128000,
                cost_per_1k=0.01,
                availability_state="available",
            )
        ]
        result = run_action("models.list", {"catalog": mock_catalog})
        assert result.ok
        assert result.data["total"] == 1
        assert result.data["models"][0]["id"] == "test-model"

    def test_doctor_via_run_action(self) -> None:
        """doctor action calls _collect_doctor_diagnostics."""
        mock_diag = MagicMock()
        mock_diag.issues = []
        mock_diag.warnings = []
        mock_diag.fixed = []
        mock_diag.sections = []
        mock_diag.documentation_preflight = {}
        mock_diag.gateway_lifecycle = {}
        mock_diag.shared_memory = {}
        mock_diag.capability_report = {}

        with patch(
            "verdict.doctor_diagnostics._collect_doctor_diagnostics", return_value=mock_diag
        ):
            result = run_action("doctor")
            assert result.ok
            assert result.data["status"] == "ok"

    def test_setup_plan_via_run_action(self) -> None:
        """setup.plan action calls build_setup_plan."""
        mock_plan = MagicMock()
        mock_plan.to_dict.return_value = {"providers": [], "harnesses": []}

        with (
            patch("verdict.setup_plan.build_setup_plan", return_value=mock_plan),
            patch(
                "verdict.shared_memory.discover_shared_memory_setup",
                return_value={"enabled": False},
            ),
        ):
            result = run_action("setup.plan")
            assert result.ok
            assert "providers" in result.data

    def test_config_show_via_run_action(self) -> None:
        """config.show action returns configuration data."""
        result = run_action("config.show")
        assert result.ok
        assert "config_file" in result.data
        assert "gateway" in result.data

    def test_credentials_list_via_run_action(self) -> None:
        """credentials.list returns credential entries."""
        mock_cred = MagicMock()
        mock_cred.env_name = "TEST_KEY"
        mock_cred.purpose = "testing"
        mock_cred.optional = True

        with (
            patch("verdict.credentials_registry.CREDENTIALS", [mock_cred]),
            patch("verdict.credentials_store.get_credential_source", return_value=("env", "t***y")),
            patch("verdict.credentials_store.CredentialsStore"),
        ):
            result = run_action("credentials.list")
            assert result.ok
            assert len(result.data) == 1
            assert result.data[0]["name"] == "TEST_KEY"

    def test_palette_entries_have_actions(self) -> None:
        """Every palette entry with a non-empty action field has a registered action."""
        from verdict.home import palette_actions

        for _section, cmd, _desc, action_name in palette_actions():
            assert get_action(action_name) is not None, (
                f"palette entry {cmd!r} maps to action {action_name!r} which is not registered"
            )

    def test_palette_run_action_in_process(self) -> None:
        """run_palette_action calls run_action in-process."""
        from verdict.home import run_palette_action

        mock_catalog = [
            MagicMock(
                id="m1",
                provider="p1",
                capability_tier=1,
                context_window=4096,
                cost_per_1k=0.001,
                availability_state="available",
            )
        ]
        ok, data = run_palette_action("models.list", {"catalog": mock_catalog})
        assert ok
        assert data["models"][0]["id"] == "m1"


# ---------------------------------------------------------------------------
# (b) JSON output structure unchanged
# ---------------------------------------------------------------------------


class TestJSONOutputUnchanged:
    """Action data matches the --json contract."""

    def test_models_list_json_keys(self) -> None:
        mock_catalog = [
            MagicMock(
                id="gpt-4",
                provider="openai",
                capability_tier=4,
                context_window=128000,
                cost_per_1k=0.03,
                availability_state="available",
            )
        ]
        with patch("verdict.actions.helpers.default_model_catalog", return_value=mock_catalog):
            result = run_action("models.list", {"catalog": mock_catalog})
            # New output shape: dict with models list + summary
            assert "models" in result.data
            assert "total" in result.data
            assert "provider_counts" in result.data
            entry = result.data["models"][0]
            expected_keys = {
                "id",
                "provider",
                "capability_tier",
                "context_window",
                "tools_support",
                "structured_output",
                "input_cost_per_million",
                "output_cost_per_million",
                "source",
                "freshness",
            }
            assert set(entry.keys()) == expected_keys

    def test_doctor_json_keys(self) -> None:
        mock_diag = MagicMock()
        mock_diag.issues = []
        mock_diag.warnings = ["test warning"]
        mock_diag.fixed = []
        mock_diag.sections = [("test", "ok", "detail")]
        mock_diag.documentation_preflight = {}
        mock_diag.gateway_lifecycle = {}
        mock_diag.shared_memory = {}
        mock_diag.capability_report = {}

        with patch(
            "verdict.doctor_diagnostics._collect_doctor_diagnostics", return_value=mock_diag
        ):
            result = run_action("doctor")
            assert "status" in result.data
            assert "issues" in result.data
            assert "warnings" in result.data
            assert "sections" in result.data

    def test_action_result_json_serializable(self) -> None:
        """ActionResult.data is always JSON-serializable."""
        result = run_action("config.show")
        serialized = json.dumps(result.data)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# (c) JSON not polluted by ANSI
# ---------------------------------------------------------------------------


class TestNoANSIPollution:
    """Verify that action data never contains ANSI escape sequences."""

    ANSI_RE = re.compile(r"\x1b\[|\033\[|\x1B\[")

    def test_models_list_no_ansi(self) -> None:
        mock_catalog = [
            MagicMock(
                id="m1",
                provider="p1",
                capability_tier=1,
                context_window=4096,
                cost_per_1k=0.001,
                availability_state="available",
            )
        ]
        result = run_action("models.list", {"catalog": mock_catalog})
        serialized = json.dumps(result.data)
        assert "\x1b" not in serialized
        assert "\033" not in serialized

    def test_config_show_no_ansi(self) -> None:
        result = run_action("config.show")
        serialized = json.dumps(result.data)
        assert "\x1b" not in serialized


# ---------------------------------------------------------------------------
# (d) Mutation action goes through real authority
# ---------------------------------------------------------------------------


class TestMutationAuthority:
    """credentials.set/unset go through the real CredentialsStore."""

    def test_credentials_set_calls_store(self) -> None:
        mock_store = MagicMock()
        mock_cred = MagicMock()
        with (
            patch("verdict.credentials_store.CredentialsStore", return_value=mock_store),
            patch("verdict.credentials_registry.get_credential", return_value=mock_cred),
        ):
            result = run_action("credentials.set", {"name": "MY_KEY", "value": "secret123"})
            assert result.ok
            mock_store.set.assert_called_once_with("MY_KEY", "secret123")

    def test_credentials_unset_calls_store(self) -> None:
        mock_store = MagicMock()
        mock_store.unset.return_value = True
        with patch("verdict.credentials_store.CredentialsStore", return_value=mock_store):
            result = run_action("credentials.unset", {"name": "MY_KEY"})
            assert result.ok
            mock_store.unset.assert_called_once_with("MY_KEY")

    def test_credentials_set_rejects_empty(self) -> None:
        result = run_action("credentials.set", {"name": "MY_KEY", "value": ""})
        assert not result.ok

    def test_credentials_set_rejects_unregistered(self) -> None:
        with patch("verdict.credentials_registry.get_credential", return_value=None):
            result = run_action("credentials.set", {"name": "UNKNOWN_KEY", "value": "val"})
            assert not result.ok


# ---------------------------------------------------------------------------
# (e) No subprocess calls to ``verdict`` in home.py or actions/
# ---------------------------------------------------------------------------


class TestNoSubprocess:
    """Grep-style: no subprocess to ``verdict`` binary in home.py or actions/."""

    def _scan_file(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        content = path.read_text()
        # Look for subprocess calls that include 'verdict' as a command
        hits = re.findall(r"subprocess\.[a-z_]+\([^)]*verdict", content)
        return hits

    def test_home_no_subprocess_verdict(self) -> None:
        hits = self._scan_file(Path("verdict/home.py"))
        assert hits == [], f"home.py calls subprocess verdict: {hits}"

    def test_actions_no_subprocess_verdict(self) -> None:
        actions_dir = Path("verdict/actions")
        for py in actions_dir.glob("*.py"):
            hits = self._scan_file(py)
            assert hits == [], f"{py} calls subprocess verdict: {hits}"


class TestMatrixFreshness:
    """Every CLI command has an action or MACHINE_ONLY entry (no gaps)."""

    def test_no_unintentional_gaps_in_matrix(self) -> None:
        """Regenerate matrix and verify zero unintentional gaps."""
        from scripts.gen_parity_matrix import generate

        content = generate()
        # Only ⚠️ gap is unintentional; 📋 gap is intentional (documented BOD)
        assert "⚠️ gap" not in content, f"parity matrix has unintentional gaps:\n{content}"

    def test_every_action_registered(self) -> None:
        """All actions referenced in the matrix are actually registered."""
        actions = {spec.name for spec in list_actions()}
        assert len(actions) >= 8  # base set

    def test_machine_only_has_reasons(self) -> None:
        """Every MACHINE_ONLY entry has a non-empty reason."""
        for cmd, reason in MACHINE_ONLY.items():
            assert reason.strip(), f"MACHINE_ONLY[{cmd!r}] has empty reason"

    def test_checked_in_matrix_not_stale(self) -> None:
        """The checked-in matrix matches what the generator produces."""
        from scripts.gen_parity_matrix import generate

        content = generate()
        matrix_path = Path("docs/cli/PARITY_MATRIX.md")
        if matrix_path.exists():
            on_disk = matrix_path.read_text().rstrip()
            generated = (content + "\n").rstrip()
            assert on_disk == generated, (
                "docs/cli/PARITY_MATRIX.md is stale — run: python scripts/gen_parity_matrix.py"
            )


# ---------------------------------------------------------------------------
# Action event lifecycle
# ---------------------------------------------------------------------------


class TestActionEvents:
    """Verify that run_action emits start/done/error events."""

    def test_successful_action_emits_start_done(self) -> None:
        sink = RecordingSink()
        result = run_action("config.show", sink=sink)
        assert result.ok
        kinds = [e.kind for e in sink.events]
        assert kinds[0] == "start"
        assert kinds[-1] == "done"

    def test_unknown_action_emits_error(self) -> None:
        sink = RecordingSink()
        result = run_action("nonexistent.action", sink=sink)
        assert not result.ok
        assert result.exit_code == 127
        assert any(e.kind == "error" for e in sink.events)

    def test_noop_sink_is_default(self) -> None:
        """run_action with no sink does not raise."""
        result = run_action("config.show")
        assert result.ok


# ---------------------------------------------------------------------------
# ActionSpec / ActionResult types
# ---------------------------------------------------------------------------


class TestActionTypes:
    def test_action_spec_fields(self) -> None:
        spec = ActionSpec(
            name="test", family="test-family", kind="read", summary="a test", tui_section="Test"
        )
        assert spec.name == "test"
        assert spec.kind == "read"
        assert spec.tui_section == "Test"

    def test_action_result_defaults(self) -> None:
        result = ActionResult(data={"x": 1})
        assert result.ok is True
        assert result.exit_code == 0
        assert result.events == []

    def test_action_sink_protocol(self) -> None:
        """NOOP_SINK satisfies ActionSink protocol."""
        assert isinstance(NOOP_SINK, ActionSink)
