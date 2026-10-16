"""Authenticated ai-memory backend client (skeleton).

Token is loaded from a configured file path inside the calling process only.
Never print tokens; never put them in controller env from this package.
Full MCP/HTTP mapping lands in a follow-up commit after contract freeze.
"""

from __future__ import annotations

from pathlib import Path

from verdict_memory.contracts import (
    SHARED_MEMORY_PROTOCOL_VERSION,
    CertificationState,
    MemoryProviderError,
    ProviderErrorCode,
    ProviderHealth,
    ProviderResultStatus,
)

DEFAULT_ENDPOINT = "http://127.0.0.1:49374"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "ai-memory" / "auth-token"
DEFAULT_TIMEOUT = 5.0


class AiMemoryClientConfig:
    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        token_file: str | Path | None = DEFAULT_TOKEN_FILE,
        token: str | None = None,
        workspace: str = "default",
        project: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not project.strip():
            raise ValueError("project is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.endpoint = endpoint.strip().rstrip("/")
        self.token_file = Path(token_file) if token_file else None
        self._token_override = token
        self.workspace = workspace
        self.project = project
        self.timeout = timeout

    def load_token(self) -> str | None:
        if self._token_override is not None:
            return self._token_override
        if self.token_file is None:
            return None
        if not self.token_file.is_file():
            return None
        # Read only in-process; caller must not log.
        return self.token_file.read_text("utf-8").strip() or None


class AiMemoryClient:
    """Authenticated client. Network methods land after contract commit."""

    provider_id = "ai-memory"
    protocol_version = SHARED_MEMORY_PROTOCOL_VERSION

    def __init__(self, config: AiMemoryClientConfig) -> None:
        self.config = config

    def health(self) -> ProviderHealth:
        token = self.config.load_token()
        if not self.config.endpoint:
            raise MemoryProviderError(ProviderErrorCode.NOT_CONFIGURED, "endpoint missing")
        if not token:
            return ProviderHealth(
                self.provider_id,
                ProviderResultStatus.AUTH_FAILED,
                CertificationState.CONFIGURED,
                self.protocol_version,
                None,
                "ai-memory",
                "not-configured",
                "auth token missing",
                capability="partial",
            )
        # Live probe is intentionally not in the first interface commit.
        return ProviderHealth(
            self.provider_id,
            ProviderResultStatus.AVAILABLE,
            CertificationState.CONFIGURED,
            self.protocol_version,
            None,
            "ai-memory",
            "configured",
            "token present; live probe deferred",
            capability="partial",
        )


__all__ = [
    "AiMemoryClient",
    "AiMemoryClientConfig",
    "DEFAULT_ENDPOINT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TOKEN_FILE",
]
