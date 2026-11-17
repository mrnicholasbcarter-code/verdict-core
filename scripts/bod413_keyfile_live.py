#!/usr/bin/env python3
"""Run BOD-413 key-file acceptance against the current installed checkout."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_REQUIRED_ENV = ("VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_API_KEY", "OPENAI_API_KEY")


def _load_base_url() -> None:
    credentials_env = Path.home() / ".config" / "verdict" / "credentials.env"
    for line in credentials_env.read_text().splitlines():
        if line.startswith("OMNIROUTE_BASE_URL="):
            os.environ["OMNIROUTE_BASE_URL"] = line.split("=", 1)[1].strip().strip('"')
            return


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        config_dir = Path(temp_dir) / "verdict"
        config_dir.mkdir(mode=0o700)
        key_link = config_dir / "omniroute-api-key"
        key_link.symlink_to(Path.home() / ".config" / "verdict" / "omniroute-api-key")
        os.environ["XDG_CONFIG_HOME"] = temp_dir
        for name in _REQUIRED_ENV:
            os.environ.pop(name, None)
        _load_base_url()

        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        print(f"sha: {sha}")
        print(f"cwd: {os.getcwd()}")

        from verdict.actions import run_action
        from verdict.credentials_store import resolve_gateway_key
        from verdict.orchestration.executors import DirectGatewayExecutor

        resolved = resolve_gateway_key()
        if resolved is None:
            raise RuntimeError("gateway key did not resolve")
        source, resolved_key = resolved[1], resolved[0]
        print(f"resolver_source: {source}")

        executor = DirectGatewayExecutor()
        bearer = executor._headers().get("authorization") == f"Bearer {resolved_key}"
        print(f"executor_auth_is_bearer_file_key: {bearer}")

        result = run_action("credentials.test", {"name": "OMNIROUTE_API_KEY"})
        status = result.data.get("status")
        print(f"credentials_test_status_is_not_missing: {status != 'missing'}")
        print(f"credentials_test_status_value: {status}")

        output = "\n".join(
            (
                f"sha: {sha}",
                f"cwd: {os.getcwd()}",
                f"resolver_source: {source}",
                f"executor_auth_is_bearer_file_key: {bearer}",
                f"credentials_test_status_is_not_missing: {status != 'missing'}",
                f"credentials_test_status_value: {status}",
            )
        )
        if resolved_key in output:
            raise RuntimeError("key leaked in acceptance output")
        return 0 if result.ok and bearer and status != "missing" else 1


if __name__ == "__main__":
    raise SystemExit(main())
