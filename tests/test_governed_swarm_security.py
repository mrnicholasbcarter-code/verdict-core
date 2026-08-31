from __future__ import annotations

import json

import pytest

from verdict.receipt_store import ReceiptStore
from verdict.swarm_evidence import MissionEventType, MissionEvidence


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "raw prompt with sk-secret"},
        {"messages": [{"role": "user", "content": "password=hunter2"}]},
        {"tool_arguments": {"cmd": "cat token"}},
        {"url": "https://user:pass@example.test/path?token=abc"},
        {"completion": "api_key=abcd"},
        {"nested": {"authorization": "Bearer abc"}},
        {"command": "export AWS_SECRET_ACCESS_KEY=abc"},
        {"candidate": {"digest": "sha256:a", "prompt": "hidden"}},
    ],
)
def test_security_corpus_rejects_non_allowlisted_sensitive_payloads(
    payload: dict[str, object],
) -> None:
    evidence = MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/security",
        swarm_id="swarm-1",
        event_id="root",
        contract_version="swarm-spec/v1",
    )

    with pytest.raises(ValueError, match="unsupported evidence payload"):
        evidence.append(MissionEventType.FAILURE, event_id="bad", payload=payload)


def test_persisted_exported_evidence_contains_no_raw_sensitive_values() -> None:
    evidence = MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/security",
        swarm_id="swarm-1",
        event_id="root",
        contract_version="swarm-spec/v1",
    )
    evidence.append(
        MissionEventType.FAILURE,
        event_id="safe",
        payload={
            "reason": "Bearer sk-live https://example.test/path?token=abc",
            "code": "denied",
            "category": "out_of_envelope",
        },
    )

    exported = json.dumps(evidence.export())

    assert "sk-live" not in exported
    assert "token=abc" not in exported
    assert "[REDACTED]" in exported
