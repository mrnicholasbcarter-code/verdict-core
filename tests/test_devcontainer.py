"""Check the declared Codespaces development-container configuration."""

import json
from pathlib import Path


def test_devcontainer_declares_ssh_feature_and_demo_command() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[1] / ".devcontainer" / "devcontainer.json").read_text()
    )
    assert "ghcr.io/devcontainers/features/sshd:1" in config["features"]
    assert config["postAttachCommand"] == "verdict demo --speed 0"
