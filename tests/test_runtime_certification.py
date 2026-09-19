"""BOD-92 runtime certification — evidence-only proof fixtures (no live network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.runtime_certification import (
    CERTIFICATION_TTL_SECONDS,
    HARNESS_PARITY_FACETS,
    AuthorityClass,
    CertificationState,
    ComponentKind,
    DetectedSnapshot,
    ParityLevel,
    ProbeBudget,
    RuntimeCertificationError,
    certify_runtime,
    classify_memory_mcps,
    harness_parity_from_evidence,
    register_detector,
    reset_detectors,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_detectors() -> None:
    reset_detectors()
    yield
    reset_detectors()


def test_binary_presence_is_not_harness_parity() -> None:
    """Installed binary alone must not claim MCP/hooks/subagent support."""

    parity = harness_parity_from_evidence(
        harness_id="claude-code",
        evidence={
            "binary_present": True,
            "version": "1.2.3",
            # no MCP/config/hooks evidence
        },
        source="fixture.binary",
    )
    by_facet = {item.facet: item for item in parity}
    assert set(by_facet) == set(HARNESS_PARITY_FACETS)
    assert by_facet["mcp"].level is ParityLevel.UNSUPPORTED
    assert by_facet["hooks"].level is ParityLevel.UNSUPPORTED
    assert (
        "binary" in by_facet["mcp"].reason.lower() or "presence" in by_facet["mcp"].reason.lower()
    )


def test_harness_parity_supported_partial_unsupported() -> None:
    parity = harness_parity_from_evidence(
        harness_id="codex",
        evidence={
            "binary_present": True,
            "mcp_config": True,
            "hooks_config": False,
            "subagents": "partial",
            "resume": True,
            "tool_interception": False,
            "model_selection": True,
            "config_import": True,
            "structured_output": "unknown",
        },
        source="fixture.config",
    )
    by_facet = {item.facet: item for item in parity}
    assert by_facet["mcp"].level is ParityLevel.SUPPORTED
    assert by_facet["hooks"].level is ParityLevel.UNSUPPORTED
    assert by_facet["subagents"].level is ParityLevel.PARTIAL
    assert by_facet["resume"].level is ParityLevel.SUPPORTED
    assert by_facet["structured_output"].level is ParityLevel.UNSUPPORTED


def test_duplicate_memory_mcps_select_canonical_authority() -> None:
    classified = classify_memory_mcps(
        (
            DetectedSnapshot(
                component_id="codebase-memory-mcp",
                kind=ComponentKind.MCP,
                identity="mcp/codebase-memory-mcp",
                source="fixture.mcp_config",
                capabilities=frozenset({"memory", "context"}),
                health_claim="ready",
            ),
            DetectedSnapshot(
                component_id="basic-memory-pilot",
                kind=ComponentKind.MCP,
                identity="mcp/basic-memory-pilot",
                source="fixture.mcp_config",
                capabilities=frozenset({"memory"}),
                health_claim="ready",
            ),
            DetectedSnapshot(
                component_id="ecc-memory-vault",
                kind=ComponentKind.MCP,
                identity="mcp/ecc-memory-vault",
                source="fixture.mcp_config",
                capabilities=frozenset({"memory"}),
                health_claim="configured",
            ),
        )
    )
    assert classified["codebase-memory-mcp"] is AuthorityClass.AUTHORITY
    assert classified["basic-memory-pilot"] is AuthorityClass.REDUNDANT
    assert classified["ecc-memory-vault"] is AuthorityClass.REDUNDANT


def test_multi_gateway_identity_not_collapsed() -> None:
    report = certify_runtime(
        snapshots=(
            DetectedSnapshot(
                component_id="omniroute",
                kind=ComponentKind.GATEWAY,
                identity="gateway/omniroute@local",
                source="fixture.mcp",
                version="0.9",
                health_claim="ready",
                capabilities=frozenset({"models", "health"}),
                models=(
                    {
                        "gateway": "omniroute",
                        "provider": "openrouter",
                        "model_id": "free/worker-a",
                        "invocation": "ok",
                        "auth_state": "authorized",
                    },
                ),
            ),
            DetectedSnapshot(
                component_id="litellm",
                kind=ComponentKind.GATEWAY,
                identity="gateway/litellm@local",
                source="fixture.config",
                version="1.0",
                health_claim="ready",
                capabilities=frozenset({"models"}),
                models=(
                    {
                        "gateway": "litellm",
                        "provider": "openrouter",
                        "model_id": "free/worker-a",
                        "invocation": "ok",
                        "auth_state": "authorized",
                    },
                ),
            ),
        ),
        now=NOW,
        probe_budget=ProbeBudget(max_probes=4, max_premium_probes=0),
    )
    model_ids = [c.identity for c in report.components if c.kind is ComponentKind.MODEL_POOL]
    assert "gateway/omniroute/provider/openrouter/model/free/worker-a" in model_ids
    assert "gateway/litellm/provider/openrouter/model/free/worker-a" in model_ids
    assert len(set(model_ids)) == 2


def test_catalog_claim_alone_never_marks_model_ready() -> None:
    report = certify_runtime(
        snapshots=(
            DetectedSnapshot(
                component_id="omniroute",
                kind=ComponentKind.GATEWAY,
                identity="gateway/omniroute@local",
                source="fixture.catalog",
                health_claim="ready",
                models=(
                    {
                        "gateway": "omniroute",
                        "provider": "openrouter",
                        "model_id": "stale/advertised",
                        "catalog_claimed": True,
                        # no invocation / assessment evidence
                    },
                ),
            ),
        ),
        now=NOW,
    )
    model = next(c for c in report.components if c.kind is ComponentKind.MODEL_POOL)
    assert model.state is CertificationState.UNAVAILABLE
    assert (
        "catalog" in model.limitations[0].lower()
        or "invocation" in " ".join(model.limitations).lower()
    )


def test_stale_evidence_expires_and_records_freshness() -> None:
    report = certify_runtime(
        snapshots=(
            DetectedSnapshot(
                component_id="serena",
                kind=ComponentKind.MCP,
                identity="mcp/serena",
                source="fixture.mcp",
                health_claim="ready",
                observed_at=NOW - timedelta(seconds=CERTIFICATION_TTL_SECONDS + 60),
                capabilities=frozenset({"symbols"}),
            ),
        ),
        now=NOW,
        ttl_seconds=CERTIFICATION_TTL_SECONDS,
    )
    mcp = next(c for c in report.components if c.component_id == "serena")
    assert mcp.freshness == "stale"
    assert mcp.state is CertificationState.UNAVAILABLE
    assert report.expires_at == NOW + timedelta(seconds=CERTIFICATION_TTL_SECONDS)


def test_unsupported_ops_never_fabricate_quota_or_cache() -> None:
    report = certify_runtime(
        snapshots=(
            DetectedSnapshot(
                component_id="generic-openai",
                kind=ComponentKind.GATEWAY,
                identity="gateway/generic-openai@cfg",
                source="fixture.config",
                health_claim="ready",
                capabilities=frozenset({"invoke"}),
                # quota/cache intentionally omitted
            ),
        ),
        now=NOW,
    )
    gw = next(c for c in report.components if c.component_id == "generic-openai")
    assert gw.quota is None
    assert gw.cache is None
    assert any("quota" in lim.lower() for lim in gw.limitations)
    assert any("cache" in lim.lower() for lim in gw.limitations)
    payload = report.to_dict()
    assert payload["components"][0]["quota"] is None
    assert payload["components"][0]["cache"] is None


def test_mcp_certification_states_and_no_secrets_in_report() -> None:
    report = certify_runtime(
        snapshots=(
            DetectedSnapshot(
                component_id="omniroute-mcp",
                kind=ComponentKind.MCP,
                identity="mcp/omniroute",
                source="fixture.mcp",
                health_claim="ready",
                capabilities=frozenset({"models", "health", "assess"}),
            ),
            DetectedSnapshot(
                component_id="docs-mcp-server",
                kind=ComponentKind.MCP,
                identity="mcp/docs-mcp-server",
                source="fixture.mcp",
                health_claim="degraded",
                capabilities=frozenset({"docs"}),
            ),
            DetectedSnapshot(
                component_id="sequential-thinking",
                kind=ComponentKind.MCP,
                identity="mcp/sequential-thinking",
                source="fixture.mcp",
                health_claim="unsupported",
                capabilities=frozenset({"reasoning"}),
            ),
        ),
        now=NOW,
    )
    by_id = {c.component_id: c for c in report.components if c.kind is ComponentKind.MCP}
    assert by_id["omniroute-mcp"].state is CertificationState.READY
    assert by_id["docs-mcp-server"].state is CertificationState.DEGRADED
    assert by_id["sequential-thinking"].state is CertificationState.UNSUPPORTED
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    for banned in ("sk-", "api_key", "bearer ", "password", "authorization", "/home/"):
        assert banned not in encoded.lower()


def test_probe_budget_is_bounded() -> None:
    with pytest.raises(RuntimeCertificationError, match="probe budget"):
        certify_runtime(
            snapshots=tuple(
                DetectedSnapshot(
                    component_id=f"mcp-{i}",
                    kind=ComponentKind.MCP,
                    identity=f"mcp/{i}",
                    source="fixture",
                    health_claim="ready",
                    requires_probe=True,
                )
                for i in range(5)
            ),
            now=NOW,
            probe_budget=ProbeBudget(max_probes=3, max_premium_probes=0),
        )


def test_detector_registry_is_plugin_driven() -> None:
    def _detector(_ctx: object) -> tuple[DetectedSnapshot, ...]:
        return (
            DetectedSnapshot(
                component_id="prime-cli",
                kind=ComponentKind.HARNESS,
                identity="harness/prime-cli",
                source="detector.prime",
                health_claim="configured",
                evidence={"binary_present": True, "mcp_config": True, "resume": True},
            ),
        )

    register_detector("prime", _detector)
    report = certify_runtime(snapshots=(), now=NOW, run_registered_detectors=True)
    harness = next(c for c in report.components if c.component_id == "prime-cli")
    assert harness.kind is ComponentKind.HARNESS
    assert harness.parity is not None
    assert any(p.facet == "mcp" and p.level is ParityLevel.SUPPORTED for p in harness.parity)


def test_certification_is_evidence_only_not_routing() -> None:
    report = certify_runtime(
        snapshots=(
            DetectedSnapshot(
                component_id="omniroute",
                kind=ComponentKind.GATEWAY,
                identity="gateway/omniroute",
                source="fixture",
                health_claim="ready",
            ),
        ),
        now=NOW,
    )
    payload = report.to_dict()
    assert "route" not in payload
    assert "selected" not in payload
    assert "stay" not in json.dumps(payload).lower()
    assert "switch" not in json.dumps(payload).lower()
    assert payload["purpose"] == "evidence"


def test_docs_and_serena_responsibility_markers() -> None:
    report = certify_runtime(
        snapshots=(
            DetectedSnapshot(
                component_id="docs-mcp-server",
                kind=ComponentKind.MCP,
                identity="mcp/docs",
                source="fixture",
                health_claim="ready",
                capabilities=frozenset({"docs"}),
            ),
            DetectedSnapshot(
                component_id="context7",
                kind=ComponentKind.MCP,
                identity="mcp/context7",
                source="fixture",
                health_claim="ready",
                capabilities=frozenset({"docs"}),
            ),
            DetectedSnapshot(
                component_id="serena",
                kind=ComponentKind.MCP,
                identity="mcp/serena",
                source="fixture",
                health_claim="ready",
                capabilities=frozenset({"symbols", "references"}),
            ),
        ),
        now=NOW,
    )
    by_id = {c.component_id: c for c in report.components}
    assert by_id["docs-mcp-server"].authority_class is AuthorityClass.AUTHORITY
    assert by_id["context7"].authority_class is AuthorityClass.ADVISORY
    assert "semantic" in " ".join(by_id["serena"].limitations).lower() or any(
        "symbols" in c for c in by_id["serena"].capabilities
    )


def test_secret_material_rejected_at_snapshot_boundary() -> None:
    with pytest.raises(RuntimeCertificationError, match="secret"):
        DetectedSnapshot(
            component_id="bad",
            kind=ComponentKind.MCP,
            identity="mcp/bad",
            source="sk-live-secret",
            health_claim="ready",
        )


def test_cli_certify_json_from_fixture(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from verdict.cli import cmd_certify

    fixture = tmp_path / "snapshots.json"
    fixture.write_text(
        json.dumps(
            {
                "snapshots": [
                    {
                        "component_id": "serena",
                        "kind": "mcp",
                        "identity": "mcp/serena",
                        "source": "fixture.cli",
                        "health_claim": "ready",
                        "capabilities": ["symbols"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cmd_certify(snapshot_path=str(fixture), output_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["purpose"] == "evidence"
    assert payload["components"][0]["component_id"] == "serena"
    assert "sk-" not in json.dumps(payload).lower()
