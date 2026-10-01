"""Keep the Codespaces quickstart remotely verifiable."""

import json
from pathlib import Path


def test_codespaces_has_ssh_and_offline_demo() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[1] / ".devcontainer" / "devcontainer.json").read_text()
    )
    assert "ghcr.io/devcontainers/features/sshd:1" in config["features"]
    assert config["postAttachCommand"] == "verdict demo --speed 0"
