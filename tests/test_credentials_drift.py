"""Test that all credential reads are registered."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from verdict.credentials_registry import CREDENTIALS


def test_all_credentials_are_registered() -> None:
    """Scan verdict/ for env var reads and ensure all credential-like names are registered."""
    credential_suffixes = ("_KEY", "_TOKEN", "_SECRET", "_BASE_URL", "_API_BASE")
    registered = {cred.env_name for cred in CREDENTIALS}

    verdict_dir = Path(__file__).parent.parent / "verdict"
    python_files = list(verdict_dir.rglob("*.py"))
    found_credentials: set[str] = set()

    for py_file in python_files:
        if "__pycache__" in str(py_file):
            continue
        try:
            content = py_file.read_text()
            tree = ast.parse(content)
        except Exception:
            continue

        for node in ast.walk(tree):
            env_name: str | None = None

            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "os"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                # os.environ["KEY"]
                env_name = node.slice.value

            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "getenv")
                and len(node.args) > 0
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                # os.environ.get("KEY") or os.getenv("KEY")
                env_name = node.args[0].value

            if env_name and any(env_name.endswith(s) for s in credential_suffixes):
                found_credentials.add(env_name)

    # Exclude known non-credential constants used in test harness / examples
    test_vars = {
        "_FAMILY_LEG_KEY",
        "_PROMPT_KEY",
        "_SECRET_KEY",
        "_SMALL_TOKEN",
        "_SETTINGS_BASE_URL",
        "_SETTINGS_BASE_URL_KEY",
        "_SETTINGS_PROVIDER_KEY",
        "API_KEY",
        "DEFAULT_TOKEN",
        "DEFAULT_MAX_TOKEN",
        "DEFAULT_CHEAP_PATH_TOKEN",
        "DEFAULT_BASE_URL",
        "DEFAULT_UPSTREAM_BASE_URL",
        "GH_TOKEN",
        "HF_TOKEN",
    }
    found_credentials -= test_vars

    unregistered = found_credentials - registered
    if unregistered:
        msg = (
            f"Found {len(unregistered)} unregistered credential(s):\n"
            + "\n".join(f"  - {name}" for name in sorted(unregistered))
            + "\n\nAdd them to verdict/credentials_registry.py CREDENTIALS."
        )
        pytest.fail(msg)
