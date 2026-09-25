"""Test that all credential reads are registered.

This drift test scans verdict/ for os.environ reads of credential-like names
and fails if any are not in CREDENTIALS registry.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from verdict.credentials_registry import CREDENTIALS


def test_all_credentials_are_registered() -> None:
    """Scan verdict/ for env var reads and ensure all credential-like names are registered."""
    # Patterns to match credential environment variable reads
    patterns = [
        r'os\.environ\[(["'])([A-Z_]+)\1\]',  # os.environ["KEY"]
        r'os\.environ\.get\((["'])([A-Z_]+)\1',  # os.environ.get("KEY"
        r'os\.getenv\((["'])([A-Z_]+)\1',  # os.getenv("KEY"
    ]
    
    # Suffixes that indicate credentials
    credential_suffixes = (
        "_KEY",
        "_TOKEN",
        "_SECRET",
        "_BASE_URL",
        "_API_BASE",
    )
    
    # Get all registered names
    registered = {cred.env_name for cred in CREDENTIALS}
    
    # Scan all Python files in verdict/
    verdict_dir = Path(__file__).parent.parent / "verdict"
    python_files = list(verdict_dir.rglob("*.py"))
    
    found_credentials = set()
    
    for py_file in python_files:
        if "__pycache__" in str(py_file):
            continue
        
        try:
            content = py_file.read_text()
        except Exception:
            continue
        
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                env_name = match.group(2)
                
                # Check if it looks like a credential
                if any(env_name.endswith(suffix) for suffix in credential_suffixes):
                    found_credentials.add(env_name)
    
    # Find unregistered credentials
    unregistered = found_credentials - registered
    
    if unregistered:
        msg = (
            f"Found {len(unregistered)} unregistered credential(s) in verdict/:\n"
            + "\n".join(f"  - {name}" for name in sorted(unregistered))
            + "\n\nAdd them to verdict/credentials_registry.py CREDENTIALS."
        )
        pytest.fail(msg)


def test_registry_is_complete_with_grep() -> None:
    """Use git grep to double-check no credentials are missed."""
    # This is a belt-and-suspenders check using grep
    try:
        result = subprocess.run(
            [
                "git",
                "grep",
                "-n",
                "-E",
                r"os\.environ\[|os\.environ\.get\(|os\.getenv\(",
                "--",
                "verdict/",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip("git grep not available or timeout")
        return
    
    if result.returncode not in {0, 1}:
        pytest.skip(f"git grep failed: {result.stderr}")
        return
    
    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    
    credential_suffixes = ("_KEY", "_TOKEN", "_SECRET", "_BASE_URL", "_API_BASE")
    registered = {cred.env_name for cred in CREDENTIALS}
    
    unregistered = set()
    
    for line in lines:
        # Look for credential-like names
        for suffix in credential_suffixes:
            # Match patterns like os.getenv("SOMETHING_KEY")
            matches = re.findall(
                rf'["\']([A-Z_]*{re.escape(suffix)})["\']',
                line,
            )
            for match in matches:
                if match not in registered:
                    unregistered.add(match)
    
    if unregistered:
        msg = (
            f"git grep found {len(unregistered)} unregistered credential(s):\n"
            + "\n".join(f"  - {name}" for name in sorted(unregistered))
            + "\n\nAdd them to verdict/credentials_registry.py CREDENTIALS."
        )
        pytest.fail(msg)
