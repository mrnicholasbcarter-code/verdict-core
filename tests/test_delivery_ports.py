"""delivery ports — subprocess-mocked; no live GitHub."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from verdict.delivery import DeliveryError
from verdict.delivery_ports import GhCliPort, LinearEvidencePort, run_gh_json


def _completed(stdout: bytes | str, returncode: int = 0) -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout if isinstance(stdout, bytes) else stdout.encode()
    mock.stderr = b""
    return mock


def test_run_gh_json_parses_and_bounds_failure() -> None:
    with patch("verdict.delivery_ports.subprocess.run", return_value=_completed('{"ok":true}')):
        assert run_gh_json(["api", "x"]) == {"ok": True}
    with (
        patch(
            "verdict.delivery_ports.subprocess.run", return_value=_completed("nope", returncode=1)
        ),
        pytest.raises(DeliveryError, match="gh failed"),
    ):
        run_gh_json(["api", "x"])


def test_gh_create_and_checks_normalize(tmp_path: Any) -> None:
    port = GhCliPort(repo="owner/repo")
    with patch(
        "verdict.delivery_ports.run_gh_json",
        return_value={"url": "https://github.com/owner/repo/pull/1"},
    ):
        url = port.create_pull_request(title="t", body="b", head="feat/x", base="main")
        assert url.endswith("/pull/1")

    checks = [
        {"name": "lint", "bucket": "pass", "state": "SUCCESS"},
        {"name": "test", "bucket": "pending", "state": "PENDING"},
        {"name": "sec", "bucket": "fail", "state": "FAILURE"},
    ]
    with patch("verdict.delivery_ports.run_gh_json", return_value=checks):
        normalized = port.list_required_checks(
            head_sha="a" * 40, pr_url="https://github.com/owner/repo/pull/1"
        )
    assert normalized[0]["conclusion"] == "SUCCESS"
    assert normalized[1]["status"] == "IN_PROGRESS"
    assert normalized[2]["conclusion"] == "FAILURE"


def test_linear_evidence_port_writes_receipt(tmp_path: Any) -> None:
    port = LinearEvidencePort(evidence_dir=tmp_path)
    port.record_delivery_evidence(
        issue="BOD-68", pr_url="https://example/pr/1", merge_sha="b" * 40, evidence={"ok": True}
    )
    port.mark_done(issue="BOD-68")
    receipt = json.loads((tmp_path / "BOD-68-delivery.json").read_text())
    assert receipt["merge_sha"] == "b" * 40
    assert (tmp_path / "BOD-68-done.marker").read_text().strip() == "BOD-68"
