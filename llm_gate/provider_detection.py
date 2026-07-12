"""Intelligent provider detection for llm-gate.

Detects locally installed LLM CLIs, configured API keys, running local servers,
and suggests centralized routers (9router, omniroute) for unified access.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypedDict

import httpx
import yaml

class ServerInfo(TypedDict, total=False):
    cli_name: str
    repo: str
    description: str
    default_base_url: str
    models_endpoint: str
    detect_running: Callable[[], bool]
    install_hint: str
    github: str

# Known local LLM servers and their default endpoints
LOCAL_SERVERS: dict[str, ServerInfo] = {
    "ollama": {
        "cli_name": "ollama",
        "default_base_url": "http://localhost:11434/v1",
        "models_endpoint": "/api/tags",
        "detect_running": lambda: _check_port(11434),
    },
    "lmstudio": {
        "cli_name": "lms",
        "default_base_url": "http://localhost:1234/v1",
        "models_endpoint": "/models",
        "detect_running": lambda: _check_port(1234),
    },
    "vllm": {
        "cli_name": "vllm",
        "default_base_url": "http://localhost:8000/v1",
        "models_endpoint": "/models",
        "detect_running": lambda: _check_port(8000),
    },
    "llamacpp": {
        "cli_name": "llama-server",
        "default_base_url": "http://localhost:8080/v1",
        "models_endpoint": "/models",
        "detect_running": lambda: _check_port(8080),
    },
    "text-generation-inference": {
        "cli_name": "text-generation-launcher",
        "default_base_url": "http://localhost:8080/v1",
        "models_endpoint": "/models",
        "detect_running": lambda: _check_port(8080),
    },
    "koboldcpp": {
        "cli_name": "koboldcpp",
        "default_base_url": "http://localhost:5001/v1",
        "models_endpoint": "/models",
        "detect_running": lambda: _check_port(5001),
    },
    "ollama-cloud": {
        "cli_name": "ollama",
        "default_base_url": "https://ollama.com/api",
        "models_endpoint": "/models",
        "detect_running": lambda: False,  # cloud service
    },
}


# Centralized routers / API aggregators
CENTRALIZED_ROUTERS: dict[str, ServerInfo] = {
    "9router": {
        "repo": "1jehuang/9router",
        "description": "Local multi-provider router with OpenAI-compatible API",
        "default_base_url": "http://localhost:20128/v1",
        "models_endpoint": "/models",
        "detect_running": lambda: _check_port(20128),
        "install_hint": "pipx install 9router",
        "github": "https://github.com/1jehuang/9router",
    },
    "omniroute": {
        "repo": "NeuronZero/omniroute",
        "description": "Universal LLM API router with load balancing",
        "default_base_url": "http://localhost:20128/v1",
        "models_endpoint": "/models",
        "detect_running": lambda: _check_port(20128),
        "install_hint": "pipx install omniroute",
        "github": "https://github.com/NeuronZero/omniroute",
    },
    "openrouter": {
        "repo": "openrouter/openrouter-api",
        "description": "Unified API for 300+ models (cloud)",
        "default_base_url": "https://openrouter.ai/api/v1",
        "models_endpoint": "/models",
        "detect_running": lambda: False,
        "install_hint": "Get API key at https://openrouter.ai/keys",
        "github": "https://openrouter.ai",
    },
}


# Provider CLI detection patterns
PROVIDER_CLIS = {
    "anthropic": ["claude", "anthropic"],
    "openai": ["openai"],
    "openrouter": ["openrouter"],
    "google": ["gemini", "gcloud"],
    "vertex": ["gcloud"],
    "xai": ["grok"],
    "groq": ["groq"],
    "together": ["together"],
    "fireworks": ["fireworks"],
    "cohere": ["cohere"],
    "mistral": ["mistral"],
    "perplexity": ["perplexity"],
    "deepinfra": ["deepinfra"],
    "replicate": ["replicate"],
    "huggingface": ["huggingface-cli", "hf"],
    "copilot": ["gh", "github-copilot"],
    "opencode": ["opencode"],
    "opencode-go": ["opencode-go"],
    "kilocode": ["kilo"],
    "huggingface-hub": ["huggingface-cli"],
}


# Environment variable patterns for API keys
API_KEY_ENV_VARS = {
    "openrouter": ["OPENROUTER_API_KEY", "OPENAI_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"],
    "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY", "GCP_API_KEY"],
    "vertex": ["GOOGLE_APPLICATION_CREDENTIALS"],
    "xai": ["XAI_API_KEY", "GROK_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "together": ["TOGETHER_API_KEY"],
    "fireworks": ["FIREWORKS_API_KEY"],
    "cohere": ["COHERE_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "perplexity": ["PERPLEXITY_API_KEY"],
    "deepinfra": ["DEEPINFRA_API_KEY"],
    "replicate": ["REPLICATE_API_TOKEN"],
    "huggingface": ["HF_TOKEN", "HUGGINGFACE_API_KEY", "HUGGINGFACE_HUB_TOKEN"],
    "copilot": ["GH_TOKEN", "GITHUB_TOKEN"],
    "opencode": ["OPENCODE_API_KEY"],
    "kilocode": ["KILO_API_KEY"],
    "bedrock": ["AWS_ACCESS_KEY_ID"],
    "alibaba": ["DASHSCOPE_API_KEY"],
    "minimax": ["MINIMAX_API_KEY"],
    "stepfun": ["STEPFUN_API_KEY"],
    "zai": ["GLM_API_KEY", "ZAI_API_KEY"],
    "kimi": ["KIMI_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "novita": ["NOVITA_API_KEY"],
    "nvidia": ["NVIDIA_API_KEY"],
    "xiaomi": ["XIAOMI_API_KEY"],
    "arcee": ["ARCEE_API_KEY"],
    "gmi": ["GMI_API_KEY"],
}


def _check_port(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port is open on localhost."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


def _which(cmd: str) -> str | None:
    """Find executable in PATH."""
    return shutil.which(cmd)


def _run_command(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    """Run command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return -1, "", str(e)


@dataclass
class DetectedProvider:
    """A detected LLM provider with metadata."""

    id: str
    name: str
    type: str  # "local_server", "cli_provider", "centralized_router", "cloud_api", "custom"
    base_url: str | None = None
    models: list[str] = field(default_factory=list)
    api_key_env: str | None = None
    api_key_configured: bool = False
    cli_available: bool = False
    server_running: bool = False
    install_hint: str | None = None
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectionResult:
    """Complete provider detection results."""

    local_servers: list[DetectedProvider] = field(default_factory=list)
    cli_providers: list[DetectedProvider] = field(default_factory=list)
    centralized_routers: list[DetectedProvider] = field(default_factory=list)
    cloud_apis: list[DetectedProvider] = field(default_factory=list)
    custom_endpoints: list[DetectedProvider] = field(default_factory=list)

    def all_providers(self) -> list[DetectedProvider]:
        """Get all detected providers as a flat list."""
        return (
            self.local_servers
            + self.cli_providers
            + self.centralized_routers
            + self.cloud_apis
            + self.custom_endpoints
        )

    def has_any_provider(self) -> bool:
        return len(self.all_providers()) > 0


def detect_local_servers() -> list[DetectedProvider]:
    """Detect running local LLM servers (Ollama, LM Studio, vLLM, etc.)."""
    detected = []

    for server_id, info in LOCAL_SERVERS.items():
        cli_available = _which(info["cli_name"]) is not None
        server_running = info["detect_running"]()

        if cli_available or server_running:
            models = []
            base_url = info["default_base_url"]

            # Try to fetch models if server is running
            if server_running:
                models = _fetch_models_from_server(base_url, info.get("models_endpoint", "/models"))

            detected.append(
                DetectedProvider(
                    id=server_id,
                    name=server_id.title().replace("-", " "),
                    type="local_server",
                    base_url=base_url,
                    models=models,
                    cli_available=cli_available,
                    server_running=server_running,
                    description=f"Local {server_id} server"
                    + (" (running)" if server_running else " (installed, not running)"),
                    metadata={"cli_name": info["cli_name"]},
                )
            )

    return detected


def _fetch_models_from_server(base_url: str, models_endpoint: str) -> list[str]:
    """Fetch available models from a local server."""
    try:
        # Try OpenAI-compatible /models endpoint first
        url = base_url.rstrip("/") + "/models"
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data:
                    return [m.get("id", "") for m in data["data"] if m.get("id")]
                return [m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")]

        # Fallback to server-specific endpoint
        url = base_url.rstrip("/v1").rstrip("/") + models_endpoint
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                # Handle different response formats
                if "models" in data:
                    return [m.get("name", m.get("id", "")) for m in data["models"]]
                if isinstance(data, list):
                    return [m.get("name", m.get("id", "")) for m in data]
    except Exception:
        pass
    return []


def detect_cli_providers() -> list[DetectedProvider]:
    """Detect installed provider CLIs and their auth status."""
    detected = []

    for provider_id, cli_names in PROVIDER_CLIS.items():
        cli_found = None
        for cli in cli_names:
            if _which(cli):
                cli_found = cli
                break

        if cli_found:
            # Check for API key in environment
            env_vars = API_KEY_ENV_VARS.get(provider_id, [])
            api_key_configured = any(os.getenv(v) for v in env_vars)

            # Check for auth in common config locations
            auth_configured = _check_provider_auth(provider_id)

            detected.append(
                DetectedProvider(
                    id=provider_id,
                    name=provider_id.replace("-", " ").title(),
                    type="cli_provider",
                    cli_available=True,
                    api_key_env=env_vars[0] if env_vars else None,
                    api_key_configured=api_key_configured or auth_configured,
                    description=f"CLI: {cli_found}"
                    + (
                        " (auth configured)"
                        if api_key_configured or auth_configured
                        else " (no auth)"
                    ),
                    metadata={"cli_name": cli_found},
                )
            )

    return detected


def _check_provider_auth(provider_id: str) -> bool:
    """Check for provider auth in config files."""
    # Check common config locations
    config_paths = [
        Path.home() / ".config" / provider_id / "config.json",
        Path.home() / ".config" / provider_id / "credentials.json",
        Path.home() / f".{provider_id}" / "config.json",
        Path.home() / f".{provider_id}" / "credentials.json",
    ]

    for path in config_paths:
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                    if data.get("api_key") or data.get("access_token") or data.get("credentials"):
                        return True
            except Exception:
                pass
    return False


def detect_centralized_routers() -> list[DetectedProvider]:
    """Detect centralized routers (9router, omniroute, etc.)."""
    detected = []

    for router_id, info in CENTRALIZED_ROUTERS.items():
        server_running = info["detect_running"]()
        cli_available = _which(router_id) is not None

        if cli_available or server_running:
            models = []
            base_url = info["default_base_url"]

            if server_running:
                models = _fetch_models_from_server(base_url, info.get("models_endpoint", "/models"))

            detected.append(
                DetectedProvider(
                    id=router_id,
                    name=router_id.replace("-", " ").title(),
                    type="centralized_router",
                    base_url=base_url,
                    models=models,
                    cli_available=cli_available,
                    server_running=server_running,
                    install_hint=info.get("install_hint"),
                    description=info.get("description", "")
                    + (" (running)" if server_running else " (installed)"),
                    metadata={"github": info.get("github")},
                )
            )

    return detected


def detect_cloud_apis() -> list[DetectedProvider]:
    """Detect cloud API providers via environment variables."""
    detected = []

    for provider_id, env_vars in API_KEY_ENV_VARS.items():
        if provider_id in PROVIDER_CLIS:
            continue  # Already handled by CLI detection

        api_key_configured = any(os.getenv(v) for v in env_vars)

        if api_key_configured:
            detected.append(
                DetectedProvider(
                    id=provider_id,
                    name=provider_id.replace("-", " ").title(),
                    type="cloud_api",
                    api_key_env=env_vars[0],
                    api_key_configured=True,
                    description=f"API key found in {next(v for v in env_vars if os.getenv(v))}",
                )
            )

    return detected


def detect_custom_endpoints() -> list[DetectedProvider]:
    """Detect custom OpenAI-compatible endpoints from config/env."""
    detected = []

    # Check OPENAI_BASE_URL
    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("OPENAI_API_KEY")

    if base_url and not base_url.startswith("https://api.openai.com"):
        models = (
            _fetch_models_from_server(base_url, "/models") if _check_port_from_url(base_url) else []
        )
        detected.append(
            DetectedProvider(
                id="custom",
                name=f"Custom ({base_url})",
                type="custom",
                base_url=base_url,
                models=models,
                api_key_env="OPENAI_API_KEY",
                api_key_configured=bool(api_key),
                server_running=_check_port_from_url(base_url),
                description=f"Custom OpenAI-compatible endpoint: {base_url}",
            )
        )

    # Check llm-gate config
    config_path = Path.home() / ".config" / "llm-gate" / "llm-gate.yaml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            for name, provider in (config.get("providers") or {}).items():
                detected.append(
                    DetectedProvider(
                        id=f"custom:{name}",
                        name=name,
                        type="custom",
                        base_url=provider.get("base_url"),
                        api_key_env=provider.get("api_key_env"),
                        api_key_configured=bool(os.getenv(provider.get("api_key_env", ""))),
                        description=f"From llm-gate config: {provider.get('base_url')}",
                    )
                )
        except Exception:
            pass

    return detected


def _check_port_from_url(url: str) -> bool:
    """Extract host:port from URL and check if port is open."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return _check_port(port, host)
    except Exception:
        return False


def detect_all_providers() -> DetectionResult:
    """Run all provider detection passes."""
    return DetectionResult(
        local_servers=detect_local_servers(),
        cli_providers=detect_cli_providers(),
        centralized_routers=detect_centralized_routers(),
        cloud_apis=detect_cloud_apis(),
        custom_endpoints=detect_custom_endpoints(),
    )


def format_detection_report(result: DetectionResult, verbose: bool = False) -> str:
    """Format detection results as a human-readable report."""
    lines = ["\n╔══════════════════════════════════════════════════════════════╗"]
    lines.append("║         llm-gate Provider Detection Report                 ║")
    lines.append("╚══════════════════════════════════════════════════════════════╝\n")

    all_providers = result.all_providers()

    if not all_providers:
        lines.append("No providers detected. Run 'llm-gate setup' to configure one.\n")
        return "\n".join(lines)

    # Group by type
    type_order = ["centralized_router", "local_server", "cli_provider", "cloud_api", "custom"]
    type_labels = {
        "centralized_router": "🔀 Centralized Routers (Recommended)",
        "local_server": "🖥️  Local Model Servers",
        "cli_provider": "📦 Provider CLIs",
        "cloud_api": "☁️  Cloud APIs",
        "custom": "🔧 Custom Endpoints",
    }

    for ptype in type_order:
        providers = [p for p in all_providers if p.type == ptype]
        if not providers:
            continue

        lines.append(f"\n{type_labels.get(ptype, ptype.title())}:")
        lines.append("─" * 60)

        for p in providers:
            status_parts = []
            if p.server_running:
                status_parts.append("🟢 Running")
            elif p.cli_available:
                status_parts.append("📦 Installed")
            else:
                status_parts.append("⚪ Available")

            if p.api_key_configured:
                status_parts.append("🔑 Auth OK")
            elif p.api_key_env:
                status_parts.append(f"🔒 Needs {p.api_key_env}")

            model_info = f" — {len(p.models)} models" if p.models else ""
            if p.models and verbose:
                model_info += f" ({', '.join(p.models[:5])}{'...' if len(p.models) > 5 else ''})"

            lines.append(f"  • {p.name}: {' | '.join(status_parts)}{model_info}")

            if p.install_hint and verbose:
                lines.append(f"    Install: {p.install_hint}")

    # Add recommendation section
    lines.append("\n" + "═" * 60)
    lines.append("💡 RECOMMENDATIONS:")
    lines.append("═" * 60)

    # Check for centralized router
    has_router = any(p.type == "centralized_router" and p.server_running for p in all_providers)
    has_local = any(p.type == "local_server" and p.server_running for p in all_providers)
    has_cloud = any(
        p.type in ("cli_provider", "cloud_api") and p.api_key_configured for p in all_providers
    )

    if not has_router and (has_local or has_cloud):
        lines.append("  • Install a centralized router (9router or omniroute) to unify")
        lines.append(
            "    all your local models + cloud APIs behind one OpenAI-compatible endpoint."
        )
        lines.append("    This makes llm-gate actually useful for routing between tiers.")
        lines.append("")
        lines.append("    Quick start:")
        lines.append("      pipx install 9router")
        lines.append("      9router serve  # Runs on http://localhost:20128/v1")
        lines.append("      llm-gate setup  # Will auto-detect 9router")
    elif has_router:
        lines.append("  ✅ Centralized router detected — llm-gate can route between")
        lines.append("     local models and cloud providers intelligently.")
        router = next(
            p for p in all_providers if p.type == "centralized_router" and p.server_running
        )
        lines.append(f"     Base URL: {router.base_url}")
        if router.models:
            lines.append(f"     Models: {', '.join(router.models[:5])}")

    if not has_local and not has_cloud and not has_router:
        lines.append("  • No providers detected. Get started with:")
        lines.append("      pipx install 9router && 9router serve")
        lines.append("    Or install a local server:")
        lines.append("      curl -fsSL https://ollama.com/install.sh | sh")
        lines.append("      ollama serve &")

    lines.append("")
    return "\n".join(lines)


def generate_llm_gate_config(result: DetectionResult) -> dict[str, Any]:
    """Generate suggested llm-gate.yaml config from detection results."""
    config: dict[str, Any] = {
        "primary_model": "anthropic/claude-3-opus-20240229",
        "providers": {},
    }

    # Prioritize centralized routers
    routers = [p for p in result.centralized_routers if p.server_running]
    if routers:
        router = routers[0]
        config["providers"][router.id] = {
            "base_url": router.base_url,
            "api_key_env": None,
        }
        # Use router's models as primary
        if router.models:
            config["primary_model"] = router.models[0]
        return config

    # Fall back to local servers
    local = [p for p in result.local_servers if p.server_running]
    if local:
        server = local[0]
        config["providers"][server.id] = {
            "base_url": server.base_url,
            "api_key_env": None,
        }
        if server.models:
            config["primary_model"] = server.models[0]
        return config

    # Fall back to cloud providers with auth
    cloud = [
        p
        for p in result.all_providers()
        if p.type in ("cli_provider", "cloud_api") and p.api_key_configured
    ]
    if cloud:
        provider = cloud[0]
        if provider.id in ("openrouter", "openai"):
            config["providers"]["openrouter"] = {
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            }
            config["primary_model"] = "anthropic/claude-3-opus-20240229"
        elif provider.id == "anthropic":
            config["providers"]["anthropic"] = {
                "base_url": "https://api.anthropic.com",
                "api_key_env": "ANTHROPIC_API_KEY",
            }
            config["primary_model"] = "claude-3-opus-20240229"
        return config

    return config


if __name__ == "__main__":
    import sys

    result = detect_all_providers()
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        print(
            json.dumps(
                {
                    "local_servers": [p.__dict__ for p in result.local_servers],
                    "cli_providers": [p.__dict__ for p in result.cli_providers],
                    "centralized_routers": [p.__dict__ for p in result.centralized_routers],
                    "cloud_apis": [p.__dict__ for p in result.cloud_apis],
                    "custom_endpoints": [p.__dict__ for p in result.custom_endpoints],
                },
                indent=2,
            )
        )
    elif len(sys.argv) > 1 and sys.argv[1] == "--config":
        import yaml

        config = generate_llm_gate_config(result)
        print(yaml.dump(config, default_flow_style=False))
    else:
        verbose = "--verbose" in sys.argv or "-v" in sys.argv
        print(format_detection_report(result, verbose=verbose))
