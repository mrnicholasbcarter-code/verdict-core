"""Functional smoke tests for the CLI binary."""

import json
import os
import subprocess

import pytest


class TestCLIHelp:
    def test_help_exits_zero(self):
        result = subprocess.run(["verdict", "--help"], capture_output=True, text=True)
        assert result.returncode == 0

    def test_help_contains_description(self):
        result = subprocess.run(["verdict", "--help"], capture_output=True, text=True)
        assert "verdict" in result.stdout.lower()

    def test_help_lists_commands(self):
        result = subprocess.run(["verdict", "--help"], capture_output=True, text=True)
        assert "route" in result.stdout
        assert "setup" in result.stdout
        assert "stats" in result.stdout
        assert "benchmark" in result.stdout


class TestCLIRoute:
    @pytest.fixture(autouse=True)
    def setup_config(self, tmp_path):
        config_dir = os.path.expanduser("~/.config/verdict")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "verdict.yaml"), "w") as f:
            f.write("primary_model: 'anthropic/claude-3-opus-20240229'\nproviders: {}\n")

    def test_route_terse_offline_fails_closed(self):
        result = subprocess.run(
            ["verdict", "route", "test prompt", "--terse", "--allow-offline"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert json.loads(result.stdout)["transport_outcome"] == "error"

    def test_route_terse_offline_names_selected_model(self):
        result = subprocess.run(
            ["verdict", "route", "test prompt", "--terse", "--allow-offline"],
            capture_output=True,
            text=True,
        )
        assert json.loads(result.stdout)["model"] == "anthropic/claude-3-opus-20240229"

    def test_route_verbose_offline_fails_closed(self):
        result = subprocess.run(
            ["verdict", "route", "test prompt", "--allow-offline"], capture_output=True, text=True
        )
        assert result.returncode == 1
        assert '"transport_outcome": "error"' in result.stdout

    def test_route_critical_offline_names_primary_without_claiming_execution(self):
        result = subprocess.run(
            [
                "verdict",
                "route",
                "deploy prod",
                "--criticality",
                "critical",
                "--terse",
                "--allow-offline",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["model"] == "anthropic/claude-3-opus-20240229"
        assert payload["transport_outcome"] == "error"


class TestCLISetup:
    def test_setup_banner(self):
        result = subprocess.run(
            ["verdict", "setup"], input="done\n", capture_output=True, text=True
        )
        assert "Setup Wizard" in result.stdout or result.returncode == 0


class TestCLIStats:
    def test_stats_no_log_file(self):
        result = subprocess.run(
            ["verdict", "stats", "--log_path", "/tmp/nonexistent.jsonl"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
