"""Credentials and dependency registry for verdict.

Every credential read by verdict must be registered here with:
- env_name: environment variable name
- purpose: short explanation
- features: list of features that require it
- optional: whether it can be missing
- live_check: callable that tests the credential (or None)

Every optional dependency must be registered with:
- name: package/tool name
- install_command: exact command to install it
- check: callable that returns (present, version_or_none)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialSpec:
    """Single credential registration."""

    env_name: str
    purpose: str
    features: tuple[str, ...]
    optional: bool
    live_check: Callable[[str], tuple[bool, str]] | None = None


@dataclass(frozen=True)
class DependencySpec:
    """Single dependency registration."""

    name: str
    install_command: str
    check: Callable[[], tuple[bool, str | None]]


def _check_omniroute_api_key(value: str) -> tuple[bool, str]:
    """Live check for OMNIROUTE_API_KEY."""
    import httpx

    base_url = os.getenv("OMNIROUTE_BASE_URL")
    if not base_url:
        return False, "OMNIROUTE_BASE_URL not set"
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {value}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, type(e).__name__


def _check_omniroute_management_token(value: str) -> tuple[bool, str]:
    """Live check for OMNIROUTE_MANAGEMENT_TOKEN."""
    import httpx

    base_url = os.getenv("OMNIROUTE_BASE_URL")
    if not base_url:
        return False, "OMNIROUTE_BASE_URL not set"
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/admin/health",
            headers={"X-Management-Token": value},
            timeout=10.0,
        )
        if resp.status_code in {200, 204}:
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, type(e).__name__


def _check_typesafe_api_key(value: str) -> tuple[bool, str]:
    """Live check for TYPESAFE_API_KEY (OpenJev)."""
    import httpx

    base_url = os.getenv("TYPESAFE_BASE_URL")
    if not base_url:
        return False, "TYPESAFE_BASE_URL not set"
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/v1/models",
            headers={"User-Agent": "verdict/live-check"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, type(e).__name__


CREDENTIALS: tuple[CredentialSpec, ...] = (
    CredentialSpec(
        env_name="OMNIROUTE_API_KEY",
        purpose="OmniRoute gateway API access",
        features=("routing", "gateway"),
        optional=False,
        live_check=_check_omniroute_api_key,
    ),
    CredentialSpec(
        env_name="OMNIROUTE_BASE_URL",
        purpose="OmniRoute gateway endpoint",
        features=("routing", "gateway"),
        optional=False,
        live_check=None,
    ),
    CredentialSpec(
        env_name="OMNIROUTE_MANAGEMENT_TOKEN",
        purpose="OmniRoute admin/management operations",
        features=("gateway-admin",),
        optional=True,
        live_check=_check_omniroute_management_token,
    ),
    CredentialSpec(
        env_name="OMNIROUTE_USAGE_API_KEY_ID",
        purpose="OmniRoute usage tracking key identifier",
        features=("usage-tracking",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="LLMGATE_UPSTREAM_API_KEY",
        purpose="LLMGate upstream API key fallback",
        features=("gateway",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="LLMGATE_UPSTREAM_BASE_URL",
        purpose="LLMGate upstream base URL fallback",
        features=("gateway",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="LLMGATE_PROBE_API_KEY",
        purpose="LLMGate probe endpoint API key",
        features=("probing",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="LLMGATE_PROBE_BASE_URL",
        purpose="LLMGate probe endpoint base URL",
        features=("probing",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="LLMGATE_AUTH_TOKEN",
        purpose="LLMGate authentication token for protected endpoints",
        features=("gateway-auth",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="OPENAI_API_KEY",
        purpose="OpenAI API key for direct provider access and harness probing",
        features=("openai", "harness-probing"),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="OPENAI_BASE_URL",
        purpose="Custom OpenAI API base URL",
        features=("openai",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="OPENAI_API_BASE",
        purpose="Alternative OpenAI API base URL (legacy)",
        features=("openai",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="GEMINI_API_KEY",
        purpose="Google Gemini API key for direct access",
        features=("gemini",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="OPENROUTER_API_KEY",
        purpose="OpenRouter API key for usage tracking",
        features=("openrouter-usage",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="XAI_MANAGEMENT_API_KEY",
        purpose="xAI management API key for usage tracking",
        features=("xai-usage",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="PI_API_KEY",
        purpose="Prime Intellect memory service API key",
        features=("memory-pi",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="HERMES_API_KEY",
        purpose="Hermes memory service API key",
        features=("memory-hermes",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="TYPESAFE_API_KEY",
        purpose="OpenJev decision signals in SHADOW mode",
        features=("decision-signals",),
        optional=True,
        live_check=_check_typesafe_api_key,
    ),
    CredentialSpec(
        env_name="TYPESAFE_BASE_URL",
        purpose="OpenJev decision signals base URL",
        features=("decision-signals",),
        optional=True,
        live_check=None,
    ),
    # Legacy names - being renamed to TYPESAFE_* by BOD-235 (parallel PR, not yet merged)
    CredentialSpec(
        env_name="OPENJEV_API_KEY",
        purpose="OpenJev decision signals API key (legacy name; use TYPESAFE_API_KEY after BOD-235)",
        features=("decision-signals",),
        optional=True,
        live_check=None,
    ),
    CredentialSpec(
        env_name="OPENJEV_BASE_URL",
        purpose="OpenJev decision signals base URL (legacy name; use TYPESAFE_BASE_URL after BOD-235)",
        features=("decision-signals",),
        optional=True,
        live_check=None,
    ),
)


def _check_python_extra(extra: str) -> tuple[bool, str | None]:
    """Check if a Python extra is installed by importing a known module."""
    import importlib.util

    if extra == "server":
        spec = importlib.util.find_spec("uvicorn")
        return (True, None) if spec else (False, None)
    if extra == "dashboard":
        spec = importlib.util.find_spec("streamlit")
        return (True, None) if spec else (False, None)
    if extra == "all":
        # Check both
        uvicorn_spec = importlib.util.find_spec("uvicorn")
        streamlit_spec = importlib.util.find_spec("streamlit")
        if uvicorn_spec and streamlit_spec:
            return True, None
        return False, None
    return False, None


def _check_gh_cli() -> tuple[bool, str | None]:
    """Check if gh CLI is installed."""
    gh_path = shutil.which("gh")
    if not gh_path:
        return False, None
    try:
        result = subprocess.run(["gh", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            if lines:
                # "gh version 2.x.x ..."
                parts = lines[0].split()
                if len(parts) >= 3:
                    return True, parts[2]
        return True, None
    except Exception:
        return True, None


def _check_node() -> tuple[bool, str | None]:
    """Check if node is installed."""
    node_path = shutil.which("node")
    if not node_path:
        return False, None
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            version = result.stdout.strip().lstrip("v")
            return True, version
        return True, None
    except Exception:
        return True, None


def _check_npm() -> tuple[bool, str | None]:
    """Check if npm is installed."""
    npm_path = shutil.which("npm")
    if not npm_path:
        return False, None
    try:
        result = subprocess.run(["npm", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return True, result.stdout.strip()
        return True, None
    except Exception:
        return True, None


def _check_verdict_client() -> tuple[bool, str | None]:
    """Check if @bodanglin/verdict-client is installed."""
    try:
        result = subprocess.run(
            ["npm", "list", "-g", "@bodanglin/verdict-client"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and "@bodanglin/verdict-client" in result.stdout:
            # Try to extract version
            lines = result.stdout.split("\n")
            for line in lines:
                if "@bodanglin/verdict-client" in line:
                    parts = line.split("@")
                    if len(parts) >= 3:
                        return True, parts[-1].strip()
            return True, None
        return False, None
    except Exception:
        return False, None


def _check_mcp_memory_service() -> tuple[bool, str | None]:
    """Check if mcp-memory-service is available."""
    mcp_path = shutil.which("mcp-memory-service")
    return (True, None) if mcp_path else (False, None)


DEPENDENCIES: tuple[DependencySpec, ...] = (
    DependencySpec(
        name="verdict[server]",
        install_command="uv pip install verdict[server]",
        check=lambda: _check_python_extra("server"),
    ),
    DependencySpec(
        name="verdict[dashboard]",
        install_command="uv pip install verdict[dashboard]",
        check=lambda: _check_python_extra("dashboard"),
    ),
    DependencySpec(
        name="verdict[all]",
        install_command="uv pip install verdict[all]",
        check=lambda: _check_python_extra("all"),
    ),
    DependencySpec(
        name="gh",
        install_command="See https://cli.github.com/manual/installation",
        check=_check_gh_cli,
    ),
    DependencySpec(name="node", install_command="See https://nodejs.org/", check=_check_node),
    DependencySpec(name="npm", install_command="Bundled with Node.js", check=_check_npm),
    DependencySpec(
        name="@bodanglin/verdict-client",
        install_command="npm install -g @bodanglin/verdict-client",
        check=_check_verdict_client,
    ),
    DependencySpec(
        name="mcp-memory-service",
        install_command="See verdict memory documentation",
        check=_check_mcp_memory_service,
    ),
    DependencySpec(
        name="OmniRoute gateway",
        install_command="Contact Prime Intellect for OmniRoute access",
        check=lambda: (bool(os.getenv("OMNIROUTE_BASE_URL")), None),
    ),
)


def get_credential(env_name: str) -> CredentialSpec | None:
    """Look up a credential by env name."""
    for cred in CREDENTIALS:
        if cred.env_name == env_name:
            return cred
    return None


def get_dependency(name: str) -> DependencySpec | None:
    """Look up a dependency by name."""
    for dep in DEPENDENCIES:
        if dep.name == name:
            return dep
    return None
