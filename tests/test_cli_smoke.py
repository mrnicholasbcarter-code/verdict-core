import subprocess
import os

def test_cli_help():
    result = subprocess.run(["llm-gate", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "llm-gate: Tier-based LLM Router" in result.stdout

def test_cli_route_terse():
    # Setup mock config
    os.makedirs(os.path.expanduser("~/.config/llm-gate"), exist_ok=True)
    with open(os.path.expanduser("~/.config/llm-gate/llm-gate.yaml"), "w") as f:
        f.write("primary_model: 'anthropic/claude-3-opus-20240229'\nproviders: {}")
        
    result = subprocess.run(["llm-gate", "route", "test prompt", "--terse"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "claude-3-opus-20240229" in result.stdout

def test_cli_setup(monkeypatch):
    # Pass 'q' or mock responses to avoid blocking stdin
    result = subprocess.run(["llm-gate", "setup"], input="q\n", capture_output=True, text=True)
    # Just asserting it doesn't crash on NameError
    assert "llm-gate Setup Wizard" in result.stdout or result.returncode == 0
