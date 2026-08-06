"""
Real Ruflo Transport Implementations

Provides concrete transport implementations for communicating with actual Ruflo
orchestration backends via subprocess, HTTP, or MCP.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class RufloTransport(ABC):
    """Abstract base for Ruflo transport implementations."""

    @abstractmethod
    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        """Submit a task/workflow to Ruflo."""
        pass

    @abstractmethod
    def status(self, request: dict[str, Any]) -> dict[str, Any]:
        """Query task/workflow status from Ruflo."""
        pass

    @abstractmethod
    def control(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute a control action on Ruflo."""
        pass

    @abstractmethod
    def result(self, request: dict[str, Any]) -> dict[str, Any]:
        """Retrieve task result from Ruflo."""
        pass


@dataclass
class SubprocessTransportConfig:
    """Configuration for Ruflo subprocess transport."""
    ruflo_command: str = "ruflo"  # or "npx @claude-flow/cli@latest"
    working_dir: str | None = None
    env: dict[str, str] | None = None
    timeout_seconds: float = 60.0


class RufloSubprocessTransport(RufloTransport):
    """Transport that communicates with Ruflo via subprocess CLI.

    Uses the Ruflo CLI for submit/status/control/result operations.
    """

    def __init__(self, config: SubprocessTransportConfig | None = None):
        self.config = config or SubprocessTransportConfig()

    def _run_ruflo(self, args: list[str], input_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run Ruflo CLI command and return parsed JSON response."""
        cmd = [self.config.ruflo_command] + args

        env = os.environ.copy()
        if self.config.env:
            env.update(self.config.env)

        # Pass input as JSON stdin
        stdin_data = json.dumps(input_data) if input_data else None

        try:
            result = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                cwd=self.config.working_dir,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise RufloTransportError(f"Ruflo command timed out: {cmd}") from e
        except FileNotFoundError as e:
            raise RufloTransportError(f"Ruflo command not found: {self.config.ruflo_command}") from e
        except Exception as e:
            raise RufloTransportError(f"Failed to execute Ruflo: {e}") from e

        if result.returncode != 0:
            raise RufloTransportError(
                f"Ruflo command failed (exit {result.returncode}): {result.stderr}"
            )

        # Parse JSON output
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RufloTransportError(f"Invalid JSON from Ruflo: {result.stdout[:500]}") from e

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._run_ruflo(["task", "submit"], request)

    def status(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._run_ruflo(["task", "status"], request)

    def control(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action", "").lower()
        task_id = request.get("task_id", "")
        workflow_id = request.get("workflow_id")

        args = ["task", "control", action, task_id]
        if workflow_id:
            args.extend(["--workflow", workflow_id])

        # Add reason if provided
        reason = request.get("reason", "")
        if reason:
            args.extend(["--reason", reason])

        # Add approver if provided
        approver = request.get("approver", "")
        if approver:
            args.extend(["--approver", approver])

        return self._run_ruflo(args, request)

    def result(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = request.get("task_id", "")
        workflow_id = request.get("workflow_id")

        args = ["task", "result", task_id]
        if workflow_id:
            args.extend(["--workflow", workflow_id])

        return self._run_ruflo(args, request)


@dataclass
class HttpTransportConfig:
    """Configuration for Ruflo HTTP transport."""
    base_url: str  # e.g., "http://localhost:8080"
    api_key: str | None = None
    timeout_seconds: float = 30.0
    verify_ssl: bool = True


class RufloHttpTransport(RufloTransport):
    """Transport that communicates with Ruflo via HTTP REST API.

    Requires Ruflo to be running as an HTTP server.
    """

    def __init__(self, config: HttpTransportConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                headers=headers,
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_ssl,
            )
        return self._client

    async def _request(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        client = await self._get_client()
        try:
            response = await client.request(method, path, json=data)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise RufloTransportError(f"HTTP {e.response.status_code}: {e.response.text}") from e
        except httpx.TimeoutException as e:
            raise RufloTransportError(f"Request timeout: {e}") from e
        except Exception as e:
            raise RufloTransportError(f"Request failed: {e}") from e

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        # Synchronous wrapper for async method
        return asyncio.run(self._request("POST", "/api/v1/tasks/submit", request))

    def status(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = request.get("task_id", "")
        workflow_id = request.get("workflow_id")
        path = f"/api/v1/tasks/{task_id}/status"
        if workflow_id:
            path += f"?workflow_id={workflow_id}"
        return asyncio.run(self._request("GET", path))

    def control(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = request.get("task_id", "")
        action = request.get("action", "")
        workflow_id = request.get("workflow_id")
        path = f"/api/v1/tasks/{task_id}/control/{action}"
        if workflow_id:
            path += f"?workflow_id={workflow_id}"
        return asyncio.run(self._request("POST", path, request))

    def result(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = request.get("task_id", "")
        workflow_id = request.get("workflow_id")
        path = f"/api/v1/tasks/{task_id}/result"
        if workflow_id:
            path += f"?workflow_id={workflow_id}"
        return asyncio.run(self._request("GET", path))

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


class RufloTransportError(Exception):
    """Transport-level error for Ruflo communication."""
    pass


def create_ruflo_transport(transport_type: str = "auto", **kwargs) -> RufloTransport:
    """Factory function to create appropriate Ruflo transport.

    Args:
        transport_type: "subprocess", "http", or "auto" (tries subprocess first)
        **kwargs: Configuration passed to transport constructor

    Returns:
        RufloTransport instance
    """
    if transport_type == "subprocess":
        return RufloSubprocessTransport(SubprocessTransportConfig(**kwargs))
    elif transport_type == "http":
        return RufloHttpTransport(HttpTransportConfig(**kwargs))
    elif transport_type == "auto":
        # Try subprocess first
        try:
            transport = RufloSubprocessTransport(SubprocessTransportConfig(**kwargs))
            # Quick health check
            transport.status({"task_id": "health-check", "workflow_id": None})
            return transport
        except Exception:
            # Fall back to HTTP if configured
            if "base_url" in kwargs:
                return RufloHttpTransport(HttpTransportConfig(**kwargs))
            raise
    else:
        raise ValueError(f"Unknown transport type: {transport_type}")


# Synchronous wrapper for async HTTP transport
class SyncRufloHttpTransport(RufloTransport):
    """Synchronous wrapper around RufloHttpTransport."""

    def __init__(self, config: HttpTransportConfig):
        self._async_transport = RufloHttpTransport(config)

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._async_transport.submit(request)

    def status(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._async_transport.status(request)

    def control(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._async_transport.control(request)

    def result(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._async_transport.result(request)

    def close(self):
        asyncio.run(self._async_transport.close())