from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from verdict.guidance import GuidanceConfig, GuidanceControlPlane


def _config(root: Path, *, enabled: bool = True, timeout_ms: int = 1000) -> GuidanceConfig:
    return GuidanceConfig(
        enabled=enabled,
        repo_root=root,
        guidance_path=root / "GUIDANCE.md",
        init_timeout_ms=timeout_ms,
    )


@pytest.mark.asyncio
async def test_guidance_is_disabled_without_loading_files(tmp_path: Path) -> None:
    plane = await GuidanceControlPlane.initialize(_config(tmp_path, enabled=False))

    assert plane.status.state == "disabled"
    assert plane.status.enabled is False


@pytest.mark.asyncio
async def test_guidance_loads_platform_neutral_rules_and_preserves_authority(
    tmp_path: Path,
) -> None:
    (tmp_path / "GUIDANCE.md").write_text(
        "# Project guidance\n\n- [allow] keep changes scoped\n- [approval] protected work needs review\n",
        encoding="utf-8",
    )

    plane = await GuidanceControlPlane.initialize(_config(tmp_path))
    result = plane.evaluate({"goal": "review protected work", "protected_work": True})

    assert plane.status.state == "ready"
    assert result["schema_version"] == "1"
    assert result["decision"] == "approval_required"
    assert result["authorization"] == "unchanged"
    assert result["matched_rules"]


@pytest.mark.asyncio
async def test_missing_guidance_is_degraded_when_explicitly_enabled(tmp_path: Path) -> None:
    plane = await GuidanceControlPlane.initialize(_config(tmp_path))

    assert plane.status.state == "degraded"
    assert plane.status.reason == "guidance_file_missing"


@pytest.mark.asyncio
async def test_malformed_guidance_is_degraded(tmp_path: Path) -> None:
    (tmp_path / "GUIDANCE.md").write_bytes(b"# Guidance\n\xff")

    plane = await GuidanceControlPlane.initialize(_config(tmp_path))

    assert plane.status.state == "degraded"
    assert plane.status.reason == "guidance_file_invalid_utf8"


def test_guidance_path_cannot_escape_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERDICT_GUIDANCE_ENABLED", "1")
    monkeypatch.setenv("VERDICT_GUIDANCE_PATH", "../outside/GUIDANCE.md")
    with pytest.raises(ValueError, match="guidance_path_outside_repo_root"):
        GuidanceConfig.from_environment(tmp_path)


@pytest.mark.asyncio
async def test_initialization_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "GUIDANCE.md").write_text("# Guidance\n", encoding="utf-8")

    async def slow_to_thread(*args: object, **kwargs: object) -> tuple[list[object], str]:
        await asyncio.sleep(0.05)
        return [], "version"

    monkeypatch.setattr("verdict.guidance.asyncio.to_thread", slow_to_thread)
    plane = await GuidanceControlPlane.initialize(_config(tmp_path, timeout_ms=1))

    assert plane.status.state == "degraded"
    assert plane.status.reason == "initialization_timeout"


@pytest.mark.asyncio
async def test_concurrent_initialization_is_independent_and_bounded(tmp_path: Path) -> None:
    (tmp_path / "GUIDANCE.md").write_text("# Guidance\n- [deny] dangerous task\n", encoding="utf-8")

    first, second = await asyncio.gather(
        GuidanceControlPlane.initialize(_config(tmp_path)),
        GuidanceControlPlane.initialize(_config(tmp_path)),
    )

    assert first.status.state == second.status.state == "ready"
    assert first.status.policy_version == second.status.policy_version


def test_api_keeps_guidance_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERDICT_GUIDANCE_ENABLED", raising=False)
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")

    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200
        status = client.get("/v1/guidance/status")
        response = client.post(
            "/v1/guidance/execute", json={"schema_version": "1", "task": {"goal": "test"}}
        )
        openapi = client.get("/openapi.json").json()

    assert status.json()["status"] == "disabled"
    assert response.status_code == 404
    assert list(path for path in openapi["paths"] if path == "/v1/guidance/execute") == [
        "/v1/guidance/execute"
    ]
