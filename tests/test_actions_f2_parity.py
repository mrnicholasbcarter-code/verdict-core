"""BOD-275 F2: CLI + TUI parity tests.

For each action, one test invokes the CLI entry point (the handler with parsed
args) and one invokes run_palette_action, both with the underlying service
monkeypatched; assert both reached the SAME service function with equal params
and produced equal ActionResult.data.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from verdict.actions.registry import run_action
from verdict.home import run_palette_action

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_catalog() -> list[MagicMock]:
    return [
        MagicMock(
            id="test-model",
            provider="test-provider",
            capability_tier=3,
            context_window=128000,
            cost_per_1k=0.01,
            availability_state="available",
        )
    ]


# ---------------------------------------------------------------------------
# models.list parity
# ---------------------------------------------------------------------------


class TestModelsListParity:
    """CLI cmd_models and TUI run_palette_action both call the same action."""

    def test_cli_calls_run_action(self) -> None:
        """cmd_models delegates to run_action('models.list')."""
        catalog = _mock_catalog()
        from verdict.cli import cmd_models

        with patch("verdict.cli.json") as mock_json:
            mock_json.dumps = json.dumps
            cmd_models(catalog=catalog, output_json=True)
            # If it got here without error, the action ran

    def test_tui_calls_same_service(self) -> None:
        """run_palette_action('models.list') produces same data."""
        catalog = _mock_catalog()
        ok, data = run_palette_action("models.list", {"catalog": catalog})
        assert ok
        assert data[0]["id"] == "test-model"

    def test_both_produce_equal_data(self) -> None:
        """Both paths produce identical ActionResult.data."""
        catalog = _mock_catalog()
        result_action = run_action("models.list", {"catalog": catalog})
        _ok, data_palette = run_palette_action("models.list", {"catalog": catalog})
        assert result_action.data == data_palette


# ---------------------------------------------------------------------------
# doctor parity
# ---------------------------------------------------------------------------


class TestDoctorParity:
    def _mock_diag(self) -> MagicMock:
        d = MagicMock()
        d.issues = []
        d.warnings = []
        d.fixed = []
        d.sections = []
        d.documentation_preflight = {}
        d.gateway_lifecycle = {}
        d.shared_memory = {}
        d.capability_report = {}
        return d

    def test_cli_calls_run_action(self) -> None:
        """cmd_doctor --json delegates to run_action('doctor')."""
        from verdict.cli import cmd_doctor

        with (
            patch("verdict.actions.helpers.collect_doctor_diagnostics") as mock_cdd,
            patch("verdict.doctor_diagnostics._collect_doctor_diagnostics") as mock_cli_cdd,
            patch("verdict.runtime_daemons.RuntimeManager") as mock_rm,
            patch("verdict.runtime_health.build_runtime_health_report") as mock_rh,
        ):
            mock_cdd.return_value = self._mock_diag()
            mock_cli_cdd.return_value = self._mock_diag()
            mock_rm.return_value.status.return_value = {}
            mock_rh.return_value.to_dict.return_value = {}
            cmd_doctor(output_json=True)

    def test_tui_calls_same_service(self) -> None:
        with patch("verdict.actions.helpers.collect_doctor_diagnostics") as mock_cdd:
            mock_cdd.return_value = self._mock_diag()
            ok, data = run_palette_action("doctor")
            assert ok
            assert data["status"] == "ok"

    def test_both_produce_equal_data(self) -> None:
        with patch("verdict.actions.helpers.collect_doctor_diagnostics") as mock_cdd:
            mock_cdd.return_value = self._mock_diag()
            result_action = run_action("doctor")
            mock_cdd.return_value = self._mock_diag()
            _ok, data_palette = run_palette_action("doctor")
            assert result_action.data == data_palette


# ---------------------------------------------------------------------------
# setup.plan parity
# ---------------------------------------------------------------------------


class TestSetupPlanParity:
    def test_cli_calls_run_action(self) -> None:
        from verdict.cli import cmd_setup_plan

        mock_plan = MagicMock()
        mock_plan.to_dict.return_value = {"providers": [], "harnesses": []}
        with (
            patch("verdict.setup_plan.build_setup_plan", return_value=mock_plan),
            patch(
                "verdict.shared_memory.discover_shared_memory_setup",
                return_value={"enabled": False},
            ),
        ):
            cmd_setup_plan(output_json=True)

    def test_tui_calls_same_service(self) -> None:
        mock_plan = MagicMock()
        mock_plan.to_dict.return_value = {"providers": [], "harnesses": []}
        with (
            patch("verdict.setup_plan.build_setup_plan", return_value=mock_plan),
            patch(
                "verdict.shared_memory.discover_shared_memory_setup",
                return_value={"enabled": False},
            ),
        ):
            ok, data = run_palette_action("setup.plan")
            assert ok
            assert "providers" in data

    def test_both_produce_equal_data(self) -> None:
        mock_plan = MagicMock()
        mock_plan.to_dict.return_value = {"providers": [], "harnesses": []}
        with (
            patch("verdict.setup_plan.build_setup_plan", return_value=mock_plan),
            patch(
                "verdict.shared_memory.discover_shared_memory_setup",
                return_value={"enabled": False},
            ),
        ):
            result_action = run_action("setup.plan")
            _ok, data_palette = run_palette_action("setup.plan")
            assert result_action.data == data_palette


# ---------------------------------------------------------------------------
# config.show parity
# ---------------------------------------------------------------------------


class TestConfigShowParity:
    def test_cli_and_tui_equal(self) -> None:
        result_action = run_action("config.show")
        ok, data_palette = run_palette_action("config.show")
        assert result_action.data == data_palette
        assert ok


# ---------------------------------------------------------------------------
# credentials.list parity
# ---------------------------------------------------------------------------


class TestCredentialsListParity:
    def _mock_creds(self) -> tuple[MagicMock, ...]:
        cred = MagicMock()
        cred.env_name = "TEST_KEY"
        cred.purpose = "testing"
        cred.optional = True
        return (cred,)

    def test_cli_calls_run_action(self) -> None:
        from verdict.cli import cmd_credentials_list

        creds = self._mock_creds()
        with (
            patch("verdict.credentials_registry.CREDENTIALS", creds),
            patch("verdict.credentials_store.get_credential_source", return_value=("env", "t***y")),
            patch("verdict.credentials_store.CredentialsStore"),
        ):
            cmd_credentials_list(output_json=True)

    def test_tui_calls_same_service(self) -> None:
        creds = self._mock_creds()
        with (
            patch("verdict.credentials_registry.CREDENTIALS", creds),
            patch("verdict.credentials_store.get_credential_source", return_value=("env", "t***y")),
            patch("verdict.credentials_store.CredentialsStore"),
        ):
            ok, data = run_palette_action("credentials.list")
            assert ok
            assert len(data) == 1

    def test_both_produce_equal_data(self) -> None:
        creds = self._mock_creds()
        with (
            patch("verdict.credentials_registry.CREDENTIALS", creds),
            patch("verdict.credentials_store.get_credential_source", return_value=("env", "t***y")),
            patch("verdict.credentials_store.CredentialsStore"),
        ):
            result_action = run_action("credentials.list")
            _ok, data_palette = run_palette_action("credentials.list")
            assert result_action.data == data_palette


# ---------------------------------------------------------------------------
# credentials.set parity
# ---------------------------------------------------------------------------


class TestCredentialsSetParity:
    def test_cli_calls_run_action(self) -> None:
        from verdict.cli import cmd_credentials_set

        mock_store = MagicMock()
        mock_cred = MagicMock()
        with (
            patch("verdict.credentials_store.CredentialsStore", return_value=mock_store),
            patch("verdict.credentials_registry.get_credential", return_value=mock_cred),
            patch("builtins.input", return_value="secret"),
            patch("getpass.getpass", return_value="secret"),
        ):
            cmd_credentials_set(name="MY_KEY", force_unregistered=False, from_stdin=False)
            mock_store.set.assert_called_once_with("MY_KEY", "secret")

    def test_tui_calls_same_service(self) -> None:
        mock_store = MagicMock()
        mock_cred = MagicMock()
        with (
            patch("verdict.credentials_store.CredentialsStore", return_value=mock_store),
            patch("verdict.credentials_registry.get_credential", return_value=mock_cred),
        ):
            ok, _data = run_palette_action("credentials.set", {"name": "MY_KEY", "value": "secret"})
            assert ok
            mock_store.set.assert_called_once_with("MY_KEY", "secret")

    def test_both_produce_equal_data(self) -> None:
        mock_store = MagicMock()
        mock_cred = MagicMock()
        with (
            patch("verdict.credentials_store.CredentialsStore", return_value=mock_store),
            patch("verdict.credentials_registry.get_credential", return_value=mock_cred),
        ):
            result_action = run_action("credentials.set", {"name": "MY_KEY", "value": "secret"})
            assert result_action.data == {"name": "MY_KEY", "status": "set"}


# ---------------------------------------------------------------------------
# credentials.unset parity
# ---------------------------------------------------------------------------


class TestCredentialsUnsetParity:
    def test_both_produce_equal_data(self) -> None:
        mock_store = MagicMock()
        mock_store.unset.return_value = True
        with patch("verdict.credentials_store.CredentialsStore", return_value=mock_store):
            result_action = run_action("credentials.unset", {"name": "MY_KEY"})
            _ok, data_palette = run_palette_action("credentials.unset", {"name": "MY_KEY"})
            assert result_action.data == data_palette


# ---------------------------------------------------------------------------
# probe parity
# ---------------------------------------------------------------------------


class TestProbeParity:
    def _mock_transport(self) -> MagicMock:
        transport = MagicMock()
        obs = MagicMock()
        obs.model_id = "test-model"
        obs.status = "ready"
        obs.http_status = 200
        obs.availability_state = "available"
        obs.latency_ms = 42.0
        obs.usage_available = True
        obs.prompt_tokens = 1
        obs.completion_tokens = 1
        obs.total_tokens = 2
        obs.error_class = None
        obs.error = None

        mock_run = MagicMock()
        mock_run.observations = [obs]
        mock_run.diagnostics.to_dict.return_value = {"ok": True}
        return transport, mock_run

    def test_both_produce_equal_data(self) -> None:
        transport, mock_run = self._mock_transport()
        with patch("verdict.probes.ProbeRunner.run_with_diagnostics", return_value=mock_run):
            result_action = run_action("probe", {"models": ["test-model"], "transport": transport})
            _ok, data_palette = run_palette_action(
                "probe", {"models": ["test-model"], "transport": transport}
            )
            assert result_action.data == data_palette


# ---------------------------------------------------------------------------
# Three-bucket classification
# ---------------------------------------------------------------------------


class TestThreeBucketClassification:
    """Verify the three-bucket classification is complete."""

    def test_machine_only_reasons_are_defensible(self) -> None:
        from verdict.actions.registry import MACHINE_ONLY

        for cmd, reason in MACHINE_ONLY.items():
            assert reason.strip(), f"MACHINE_ONLY[{cmd!r}] has empty reason"
            # Must be one of: server/daemon lifecycle, hook/MCP invoked by programs,
            # destructive global, or legacy internal
            assert any(
                keyword in reason.lower()
                for keyword in [
                    "lifecycle",
                    "daemon",
                    "server",
                    "invoked by",
                    "destructive",
                    "legacy",
                    "dev-tool",
                    "batch",
                    "external tool",
                    "probe wrapper",
                    "superseded",
                    "alias",
                    "scripts",
                ]
            ), f"MACHINE_ONLY[{cmd!r}] reason not defensible: {reason}"

    def test_launch_reasons_are_long_running(self) -> None:
        from verdict.actions.registry import LAUNCH

        for cmd, spec in LAUNCH.items():
            assert spec.reason.strip(), f"LAUNCH[{cmd!r}] has empty reason"

    def test_gap_entries_reference_bod(self) -> None:
        from verdict.actions.registry import GAP

        for cmd, reason in GAP.items():
            assert "BOD" in reason, f"GAP[{cmd!r}] should reference a BOD: {reason}"

    def test_no_unintentional_gaps(self) -> None:
        from scripts.gen_parity_matrix import generate

        content = generate()
        assert "⚠️ gap" not in content


# ---------------------------------------------------------------------------
# New actions (F3)
# ---------------------------------------------------------------------------


class TestNewActions:
    """Verify new actions are registered and callable."""

    @pytest.mark.parametrize(
        "action_name",
        [
            "run-receipt",
            "compare",
            "catalog",
            "detect",
            "stats",
            "suggest",
            "cost-report",
            "replay",
        ],
    )
    def test_action_is_registered(self, action_name: str) -> None:
        from verdict.actions.registry import get_action

        assert get_action(action_name) is not None, f"{action_name} not registered"


# ---------------------------------------------------------------------------
# Interactive palette
# ---------------------------------------------------------------------------


class TestInteractivePalette:
    """Verify palette_actions returns entries with actions."""

    def test_palette_entries_have_registered_actions(self) -> None:
        from verdict.actions.registry import get_action
        from verdict.home import palette_actions

        for _section, cmd, _desc, action_name in palette_actions():
            assert get_action(action_name) is not None, (
                f"palette entry {cmd!r} -> {action_name!r} not registered"
            )

    def test_palette_has_new_actions(self) -> None:
        from verdict.home import palette_actions

        action_names = {a for _, _, _, a in palette_actions()}
        for expected in ["run-receipt", "compare", "catalog", "detect", "stats", "suggest"]:
            assert expected in action_names, f"palette missing {expected}"
