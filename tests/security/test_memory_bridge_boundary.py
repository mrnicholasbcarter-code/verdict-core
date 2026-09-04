"""Scope boundary tests for ``verdict.memory_bridge`` (T019)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.security.fixtures import SECRET_KEYED_VALUES
from verdict.memory_bridge import MemoryHookController
from verdict.memory_gate import MemoryWriteRequest
from verdict.memory_plane import MemoryPlane


@pytest.fixture
def controller() -> MemoryHookController:
    with tempfile.TemporaryDirectory() as tmp:
        plane = MemoryPlane(path=Path(tmp) / "boundary.db")
        try:
            yield MemoryHookController(plane=plane)
        finally:
            plane.close()


def test_write_memory_does_not_cross_scope_boundary(controller: MemoryHookController) -> None:
    secret = SECRET_KEYED_VALUES["api_key"]
    result = controller.write_memory(
        MemoryWriteRequest(
            namespace="bridge-boundary",
            key="memory-write",
            value={"api_key": secret},
            authority="agent",
            confidence=1.0,
            provenance={"source": "security-boundary-test"},
            scope="tenant-a",
        )
    )

    assert result["allowed"] is True
    stored = controller.plane.get("bridge-boundary", "memory-write", scope="tenant-a")
    assert stored is not None
    assert secret not in stored.content
    assert controller.plane.get("bridge-boundary", "memory-write", scope="tenant-b") is None


def test_file_write_memory_is_not_retrievable_from_default_scope(
    controller: MemoryHookController,
) -> None:
    secret = SECRET_KEYED_VALUES["api_key"]
    file_path = "src/security-boundary-fixture.py"
    result = controller.on_file_write(file_path, json.dumps({"api_key": secret}))

    assert result["status"] == "success"
    stored = controller.plane.get("files", file_path, scope="project")
    assert stored is not None
    assert secret not in stored.content
    assert controller.plane.get("files", file_path, scope="default") is None
