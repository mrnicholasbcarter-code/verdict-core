"""Functional smoke tests for the CLI binary."""

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

    def test_route_terse_exits_zero(self):
        result = subprocess.run(
            ["verdict", "route", "test prompt", "--terse"], capture_output=True, text=True
        )
        assert result.returncode == 0

    def test_route_terse_outputs_model_name(self):
        result = subprocess.run(
            ["verdict", "route", "test prompt", "--terse"], capture_output=True, text=True
        )
        assert "claude-3-opus-20240229" in result.stdout

    def test_route_verbose_exits_zero(self):
        result = subprocess.run(["verdict", "route", "test prompt"], capture_output=True, text=True)
        assert result.returncode == 0

    def test_route_critical_returns_primary(self):
        result = subprocess.run(
            ["verdict", "route", "deploy prod", "--criticality", "critical", "--terse"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "claude-3-opus-20240229" in result.stdout


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
