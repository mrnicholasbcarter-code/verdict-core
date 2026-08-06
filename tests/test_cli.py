"""CLI tests for the Pi.dev-inspired commands (CLI-001, #261)."""

from __future__ import annotations

import json

import pytest

from verdict import cli
from verdict.models import ModelInfo


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


class TestSubcommandRegistration:
    def test_new_subcommands_are_registered(self, capsys, monkeypatch):
        monkeypatch.setattr(cli.sys, "argv", ["verdict", "--help"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        for name in ("run", "plan", "inspect", "models", "replay", "simulate"):
            assert name in out


class TestCmdRun:
    def test_run_terse_routes_like_route(self, capsys):
        cli.cmd_run("deploy prod", "critical", terse=True)
        assert capsys.readouterr().out.strip() == "anthropic/claude-3-opus-20240229"


class TestCmdPlan:
    def test_plan_json_is_mutation_free(self, tmp_path, capsys):
        cli.cmd_plan(output_json=True)
        report = json.loads(capsys.readouterr().out)
        assert report["kind"] == "setup_plan"
        assert report["mutation_free"] is True
        assert report["network_access"] == "disabled"


class TestCmdModels:
    def test_models_json_lists_catalog(self, tmp_path, capsys, monkeypatch):
        cfg_dir = tmp_path / ".config" / "verdict"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "verdict.yaml").write_text(
            "primary_model: anthropic/claude-3-opus-20240229\nproviders: {}\n"
        )
        cli.cmd_models(output_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert payload[0]["id"] == "anthropic/claude-3-opus-20240229"
        assert "tier" in payload[0]

    def test_models_table_renders(self, capsys):
        cli.cmd_models(
            catalog=[ModelInfo(id="a/model", provider="a", capability_tier=2, context_window=8000)]
        )
        assert "a/model" in capsys.readouterr().out


class TestCmdInspect:
    def test_inspect_existing_model(self, capsys):
        cli.cmd_inspect(
            "a/model",
            catalog=[ModelInfo(id="a/model", provider="a", capability_tier=2)],
            output_json=True,
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == "a/model"

    def test_inspect_missing_model_exits_nonzero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.cmd_inspect("missing/model", catalog=[], output_json=True)
        assert exc.value.code == 1
        assert "not found" in json.loads(capsys.readouterr().out)["error"]


class TestCmdReplay:
    def test_replay_missing_session_reports_not_found(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("VERDICT_MEMORY_DB", str(tmp_path / "memory.db"))
        with pytest.raises(SystemExit) as exc:
            cli.cmd_replay("some-session", output_json=True)
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "missing"

    def test_replay_existing_session_renders(self, capsys, tmp_path, monkeypatch):
        from verdict.execution_session import ExecutionSession
        from verdict.memory_plane import MemoryPlane

        db_path = tmp_path / "memory.db"
        monkeypatch.setenv("VERDICT_MEMORY_DB", str(db_path))
        with MemoryPlane(str(db_path)) as plane:
            ExecutionSession.create(
                session_id="sess-1",
                task_spec={"prompt": "do it"},
                steps=[("step-1", "first")],
                plane=plane,
                model_id="x/model",
            )

        cli.cmd_replay("sess-1", output_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["session_id"] == "sess-1"
        assert payload["model_id"] == "x/model"


class TestCmdSimulate:
    def test_simulate_json_forecast(self, tmp_path, capsys):
        cfg_dir = tmp_path / ".config" / "verdict"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "verdict.yaml").write_text(
            "primary_model: anthropic/claude-3-opus-20240229\nproviders: {}\n"
        )
        cli.cmd_simulate("translate the docs", "low", output_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert "model" in payload
        assert payload["tier"] == 0
        assert payload["total_tokens"] == payload["prompt_tokens"] + payload["completion_tokens"]
        assert 0 <= payload["risk_score"] <= 100
        assert payload["cost_usd"] >= 0

    def test_simulate_respects_model_override(self, capsys, monkeypatch):
        monkeypatch.setattr(
            cli,
            "default_model_catalog",
            lambda: [
                ModelInfo(id="x/frontier", provider="x", capability_tier=0, cost_per_1k=5.0),
                ModelInfo(id="x/cheap", provider="x", capability_tier=3, cost_per_1k=0.1),
            ],
        )
        cli.cmd_simulate("task", "low", model_override="x/frontier", output_json=True)
        assert json.loads(capsys.readouterr().out)["model"] == "x/frontier"

    def test_simulate_table_renders(self, capsys, monkeypatch):
        monkeypatch.setattr(
            cli,
            "default_model_catalog",
            lambda: [ModelInfo(id="x/model", provider="x", capability_tier=2, context_window=8000)],
        )
        cli.cmd_simulate("hello world task", "medium")
        out = capsys.readouterr().out
        assert "Verdict pre-execution simulation" in out
        assert "Risk score" in out


class TestMainDispatch:
    def test_main_dispatches_simulate(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(
            cli.sys,
            "argv",
            ["verdict", "simulate", "some task", "--criticality", "medium", "--json"],
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert "risk_score" in payload

    def test_main_dispatches_models(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(cli.sys, "argv", ["verdict", "models", "--json"])
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)

    def test_main_dispatches_plan(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(cli.sys, "argv", ["verdict", "plan", "--json"])
        cli.main()
        report = json.loads(capsys.readouterr().out)
        assert report["mutation_free"] is True

    def test_main_dispatches_replay_missing(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(cli.sys, "argv", ["verdict", "replay", "sess-1", "--json"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "missing"
