"""Hermetic to_dict-shaped projection and static Prime resolver fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.harness_prime_compat import (
    PrimeCompatibilityContext,
    bind_prime_token,
    prime_selection_rows,
)
from verdict.harness_prime_selection import (
    PrimeDependencies,
    PrimeSelectionError,
    SettingsDocument,
    byte_digest,
    preview_selection,
)

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
SECRET = "sk-private-do-not-render"


def row(route_id: str = "cx/gpt-6-sol", **changes: Any) -> dict[str, Any]:
    # Public VerifiedModelRow.to_dict() schema, not sibling implementation fields.
    value: dict[str, Any] = {
        "route_id": route_id,
        "provider": route_id.split("/", 1)[0],
        "status": "VERIFIED",
        "coding_ok": False,
        "checked_at": (NOW - timedelta(minutes=1)).isoformat(),
        "fresh_until": (NOW + timedelta(minutes=5)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "last_success_at": (NOW - timedelta(minutes=1)).isoformat(),
        "evidence_source": "local-gateway-evidence",
        "identity": "verified",
        "capabilities": {"context_window": 128000, "tools": False, "structured": False},
        "restriction": None,
        "reason": None,
        "restrictions": [],
        "cooldown_until": None,
        "cooldown_scope": None,
        "failure_category": None,
        "http_status": None,
        "latency_ms": 7.0,
        "probe_class": "liveness",
        "agentic_ok": False,
        "agentic_checked_at": None,
        "capacity_class": "PREPAID",
        "refreshable": True,
        "refresh_reason": None,
        "hints": [],
        "freshness": "fresh",
    }
    value.update(changes)
    return value


def registry() -> dict[str, Any]:
    return {
        "providers": {
            "omniroute": {
                "api": "openai-completions",
                "baseUrl": "http://127.0.0.1:20128/v1",
                "apiKey": SECRET,
                "headers": {"Authorization": SECRET},
                "models": [
                    {
                        "id": "cx/gpt-6-sol",
                        "name": "Friendly GPT",
                        "thinkingLevelMap": {"high": "high"},
                        "cost": {"input": 3},
                    },
                    {
                        "id": "cc/claude-opus-5-5",
                        "name": "Friendly Claude",
                        "contextWindow": 123456,
                    },
                    {"id": "cx/old", "name": "Prior model", "api": "anthropic-messages"},
                ],
            },
            "openai": {
                "api": "openai-responses",
                "models": [{"id": "external", "name": "Other provider"}],
            },
        }
    }


def context(agent_dir: Path) -> PrimeCompatibilityContext:
    return PrimeCompatibilityContext(
        agent_dir=agent_dir,
        installed=True,
        release="0.9.8",
        binary_digest="fixture-binary-sha",
        gateway_endpoint="http://127.0.0.1:20128/v1",
        evidence_source="local-gateway-evidence",
        credentials_present=True,
    )


def inputs(tmp_path: Path) -> tuple[SettingsDocument, PrimeDependencies]:
    raw = json.dumps(
        {
            "enabledModels": ["cx/old", "external", "historic/exact"],
            "defaultModel": "cx/old",
            "defaultProvider": "omniroute",
            "credential": SECRET,
            "nested": {"auth": SECRET, "keep": [1, 2]},
        }
    ).encode()
    return (
        SettingsDocument.from_bytes(tmp_path / "settings.json", raw),
        PrimeDependencies(json.dumps(registry()).encode(), context(tmp_path)),
    )


def test_pure_multi_selection_scope_and_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, deps = inputs(tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("pure preview attempted I/O")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    plan = preview_selection(
        settings,
        [row(), row("cc/claude-opus-5-5")],
        selected_ids=["omniroute/cx/gpt-6-sol", "cc/claude-opus-5-5", "cx/gpt-6-sol"],
        dependencies=deps,
        now=NOW,
    )
    assert not plan.refusals
    assert plan.selected_ids == ("cx/gpt-6-sol", "cc/claude-opus-5-5")
    assert plan.after == ("external", "historic/exact", "cx/gpt-6-sol", "cc/claude-opus-5-5")
    assert plan.removed == ("cx/old",)
    assert plan.retained == ("external", "historic/exact")
    assert plan.added == ("cx/gpt-6-sol", "cc/claude-opus-5-5")
    assert plan.pre_digest == byte_digest(settings.raw_bytes)
    assert plan.proof_deadline == NOW + timedelta(minutes=5)
    assert any("not_a_full_verified_allowlist" in w for w in plan.warnings)
    assert "defaultModel_outside_scope_unchanged" in plan.warnings
    assert SECRET not in json.dumps(plan.to_dict()) + repr(plan) + repr(settings) + repr(deps)
    assert not plan.rows[0].coding_ok  # no hidden coding floor
    assert plan.rows[0].selectable
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "status", ["STALE", "UNVERIFIED", "FAILED", "UNAVAILABLE", "INVENTORY_ONLY", "EXCLUDED"]
)
def test_not_verified_status_refused(tmp_path: Path, status: str) -> None:
    settings, deps = inputs(tmp_path)
    plan = preview_selection(
        settings,
        [row(status=status, reason=SECRET)],
        selected_ids=["cx/gpt-6-sol"],
        dependencies=deps,
        now=NOW,
    )
    assert f"cx/gpt-6-sol:health_{status.lower()}" in plan.refusals
    assert plan.rows[0].status == status
    assert SECRET not in json.dumps(plan.to_dict())


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"fresh_until": NOW.isoformat()}, "proof_expired"),
        ({"fresh_until": None}, "missing_proof_timestamps"),
        ({"expires_at": None}, "missing_proof_timestamps"),
        ({"checked_at": (NOW + timedelta(seconds=1)).isoformat()}, "invalid_proof_timestamps"),
        ({"identity": "unverified"}, "identity_unverified"),
        (
            {
                "cooldown_until": (NOW + timedelta(seconds=1)).isoformat(),
                "cooldown_scope": "provider",
            },
            "active_cooldown",
        ),
        ({"restriction": SECRET}, "active_restriction"),
        ({"restrictions": [SECRET]}, "active_restriction"),
    ],
)
def test_proof_blockers(tmp_path: Path, changes: dict[str, Any], reason: str) -> None:
    settings, deps = inputs(tmp_path)
    plan = preview_selection(
        settings, [row(**changes)], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert f"cx/gpt-6-sol:{reason}" in plan.refusals
    assert SECRET not in json.dumps(plan.to_dict())


def test_visibility_is_not_health_and_sync_is_separate(tmp_path: Path) -> None:
    settings, deps = inputs(tmp_path)
    models = registry()
    models["providers"]["omniroute"]["models"] = []
    deps = replace(deps, registry_bytes=json.dumps(models).encode())
    plan = preview_selection(
        settings, [row()], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert plan.rows[0].status == "VERIFIED"
    assert not plan.rows[0].visible
    assert "cx/gpt-6-sol:not visible to Prime" in plan.refusals
    assert "cx/gpt-6-sol:separate_sync_required" in plan.refusals
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"installed": False}, "binary_missing"),
        ({"release": "0.9.99"}, "release_support_unknown"),
        ({"credentials_present": None}, "credentials_unknown"),
        ({"credentials_present": False}, "credentials_missing"),
        ({"gateway_endpoint": "http://other-gateway/v1"}, "gateway_endpoint_unbound"),
        ({"gateway_endpoint": "http://u:secret@localhost/v1"}, "gateway_endpoint_unbound"),
        ({"evidence_source": "another-generation"}, "evidence_source_unbound"),
        ({"path_recognized": False}, "effective_path_unrecognized"),
    ],
)
def test_readiness_fail_closed(tmp_path: Path, changes: dict[str, Any], reason: str) -> None:
    settings, deps = inputs(tmp_path)
    deps = replace(deps, discovery=replace(deps.discovery, **changes))
    plan = preview_selection(
        settings, [row()], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert any(reason in r for r in plan.refusals)
    assert plan.rows[0].execution != "compatible_not_launch_confirmed"


def test_per_model_api_override_not_provider_api(tmp_path: Path) -> None:
    models = registry()
    models["providers"]["omniroute"]["api"] = "anthropic-messages"
    models["providers"]["omniroute"]["models"][0]["api"] = "openai-responses"
    rows = prime_selection_rows(
        models, [row(), row("cc/claude-opus-5-5")], context=context(tmp_path), now=NOW
    )
    assert rows[0].selectable
    assert rows[0].execution == "compatible_not_launch_confirmed"
    assert "unsupported_api" in rows[1].reasons
    assert rows[0].visible and rows[1].visible


@pytest.mark.parametrize(
    "token",
    [
        "*",
        "auto/*",
        "cx/*",
        "[cx]",
        "Friendly GPT",
        "gpt",
        "--probe",
        "cx/gpt-6-sol:high",
        "CX/GPT-6-SOL",
    ],
)
def test_nonexact_selected_tokens_refused(tmp_path: Path, token: str) -> None:
    settings, deps = inputs(tmp_path)
    plan = preview_selection(settings, [row()], selected_ids=[token], dependencies=deps, now=NOW)
    assert plan.refusals
    assert not any(r.selectable for r in plan.rows)


@pytest.mark.parametrize("token", ["cx/*", "gpt", "Friendly GPT", "cx/gpt-6-sol:high", "auto/*"])
def test_unsafe_existing_scope_refused_without_expansion(tmp_path: Path, token: str) -> None:
    settings, deps = inputs(tmp_path)
    settings = SettingsDocument.from_bytes(
        settings.path, json.dumps({"enabledModels": [token]}).encode()
    )
    plan = preview_selection(
        settings, [row()], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert plan.refusals
    assert "resolve_unsafe_scope_through_Prime_/scoped-models" in plan.warnings


@pytest.mark.parametrize(
    "other",
    [
        {"id": "cx/gpt-6-sol", "name": "duplicate id"},
        {"id": "CX/GPT-6-SOL", "name": "case duplicate"},
        {"id": "competing", "name": "cx/gpt-6-sol"},
    ],
)
def test_provider_and_name_ambiguity_refused(tmp_path: Path, other: dict[str, Any]) -> None:
    models = registry()
    models["providers"]["openai"]["models"].append(other)
    binding = bind_prime_token("cx/gpt-6-sol", models)
    assert binding.disposition == "unsafe"
    assert "identity_unsupported" in binding.reasons
    overlay = prime_selection_rows(models, [row()], context=context(tmp_path), now=NOW)
    assert overlay[0].visible and overlay[0].status == "VERIFIED"
    assert not overlay[0].selectable


def test_literal_colon_and_case_kept(tmp_path: Path) -> None:
    models = registry()
    models["providers"]["omniroute"]["models"].append({"id": "cx/Literal:tag", "name": "Literal"})
    overlay = prime_selection_rows(
        models, [row("cx/Literal:tag")], context=context(tmp_path), now=NOW
    )
    assert overlay[0].selectable
    assert overlay[0].prime_id == "cx/Literal:tag"
    models["providers"]["omniroute"]["models"].append({"id": "cx/Literal", "name": "Base"})
    assert bind_prime_token("cx/Literal:tag", models).disposition == "unsafe"


def test_projection_duplicates_fail_closed(tmp_path: Path) -> None:
    settings, deps = inputs(tmp_path)
    plan = preview_selection(
        settings, [row(), row()], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert "cx/gpt-6-sol:projection_missing_or_ambiguous" in plan.refusals


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"nested":{"x":1,"x":2}}',
        b"[]",
        b'{"enabledModels":1}',
        b'{"enabledModels":[1]}',
        b'{"x":NaN}',
        b"broken sk-private-do-not-render",
    ],
)
def test_invalid_documents_do_not_echo_content(tmp_path: Path, raw: bytes) -> None:
    with pytest.raises(PrimeSelectionError) as error:
        SettingsDocument.from_bytes(tmp_path / "settings.json", raw)
    assert SECRET not in str(error.value)


def test_naive_time_and_empty_selection_refused(tmp_path: Path) -> None:
    settings, deps = inputs(tmp_path)
    with pytest.raises(ValueError, match="timezone_aware_time_required"):
        preview_selection(
            settings,
            [row()],
            selected_ids=["cx/gpt-6-sol"],
            dependencies=deps,
            now=NOW.replace(tzinfo=None),
        )
    plan = preview_selection(settings, [], selected_ids=[], dependencies=deps, now=NOW)
    assert "nonempty_selection_required" in plan.refusals


def test_project_override_warns_and_wrong_target_refuses(tmp_path: Path) -> None:
    settings, deps = inputs(tmp_path)
    deps = replace(deps, discovery=replace(deps.discovery, project_scope_override=True))
    plan = preview_selection(
        settings, [row()], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert "current_project_enabledModels_overrides_global_scope" in plan.warnings
    settings = replace(settings, path=tmp_path / "mirror" / "settings.json")
    plan = preview_selection(
        settings, [row()], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert "effective_path_unrecognized" in plan.refusals


def test_model_auth_requires_per_id_readiness(tmp_path: Path) -> None:
    models = registry()
    models["providers"]["omniroute"]["models"][0]["apiKey"] = "!do-not-execute"
    result = prime_selection_rows(models, [row()], context=context(tmp_path), now=NOW)[0]
    assert not result.selectable and "model_credentials_unknown" in result.reasons
    ctx = replace(context(tmp_path), credential_overrides=(("cx/gpt-6-sol", True),))
    assert prime_selection_rows(models, [row()], context=ctx, now=NOW)[0].selectable


def test_malformed_untrusted_metadata_fail_closed(tmp_path: Path) -> None:
    models = registry()
    models["providers"]["omniroute"]["models"][0]["api"] = ["openai-completions"]
    result = prime_selection_rows(
        models, [row(identity={}, cooldown_scope=[])], context=context(tmp_path), now=NOW
    )[0]
    assert not result.selectable
    assert "identity_unverified" in result.reasons and "unsupported_api" in result.reasons
    assert result.identity is None and result.cooldown_scope is None


@pytest.mark.parametrize(
    "code,warning",
    [
        ("chat_only_not_coding_verified", "chat verified; tools unverified"),
        ("agentic_not_fresh", "agentic proof not fresh"),
        ("pool_binding_ambiguous", "pool binding ambiguous"),
        ("account_binding_ambiguous", "account binding ambiguous"),
    ],
)
def test_informational_verified_restriction_warns_not_blocks(
    tmp_path: Path, code: str, warning: str
) -> None:
    settings, deps = inputs(tmp_path)
    plan = preview_selection(
        settings,
        [row(coding_ok=False, restriction=code, restrictions=[code])],
        selected_ids=["cx/gpt-6-sol"],
        dependencies=deps,
        now=NOW,
    )
    assert not plan.refusals
    assert plan.rows[0].selectable
    assert warning in " ".join(plan.to_dict()["rows"][0]["warnings"])


def test_unknown_restriction_still_blocks(tmp_path: Path) -> None:
    settings, deps = inputs(tmp_path)
    plan = preview_selection(
        settings,
        [row(restrictions=["future_unknown_rule"])],
        selected_ids=["cx/gpt-6-sol"],
        dependencies=deps,
        now=NOW,
    )
    assert "active_restriction" in plan.rows[0].reasons
    assert not plan.rows[0].selectable


def test_actual_chat_projection_is_selectable_with_warning(tmp_path: Path) -> None:
    from tests.test_verified_models_projection import at_rest_entry, conn, health_cache
    from tests.test_verified_models_projection import row as inventory_row
    from verdict.orchestration.verified_models import (
        project_verified_models,
        snapshots_from_documents,
    )

    proof = at_rest_entry(
        "cc/claude-opus-5-5", checked_at=NOW - timedelta(seconds=60), tool_ok=False
    )
    evidence = snapshots_from_documents(health_cache_doc=health_cache(proof), now=NOW)
    projected = project_verified_models(
        [inventory_row("cc/claude-opus-5-5")], [conn("cc")], evidence, now=NOW
    )
    assert projected.rows[0].status.value == "VERIFIED"
    ctx = replace(context(tmp_path), evidence_source="health_cache")
    overlay = prime_selection_rows(registry(), projected.to_dict()["rows"], context=ctx, now=NOW)
    assert overlay[0].selectable, overlay[0]
    assert not overlay[0].coding_ok
    assert "chat verified; tools unverified" in overlay[0].warnings


def test_unsafe_spaced_row_does_not_abort_registry_and_exact_id_binds(tmp_path: Path) -> None:
    """Defect 1 (a): one unsafe-spaced row + the selected exact VERIFIED id.

    preview_selection must succeed and the selected id must bind, exactly the
    real-registry shape (242 omniroute rows like "aihorde/A-Zovya RPG
    Inpainting" alongside normal exact ids).
    """
    models = registry()
    models["providers"]["omniroute"]["models"].append(
        {"id": "aihorde/A-Zovya RPG Inpainting", "name": "aihorde/A-Zovya RPG Inpainting"}
    )
    settings, deps = inputs(tmp_path)
    deps = replace(deps, registry_bytes=json.dumps(models).encode())
    plan = preview_selection(
        settings, [row()], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert not plan.refusals
    assert plan.rows[0].selectable
    assert plan.rows[0].prime_id == "cx/gpt-6-sol"


def test_unsafe_row_still_matches_as_competitor_fail_closed(tmp_path: Path) -> None:
    """Defect 1 (b): a token that is a case-insensitive substring of an unsafe
    row's id/name still refuses as identity_unsupported (fail closed), it is
    never silently dropped from resolution.
    """
    models = registry()
    models["providers"]["omniroute"]["models"].append(
        {"id": "aihorde/A-Zovya RPG Inpainting", "name": "aihorde/A-Zovya RPG Inpainting"}
    )
    binding = bind_prime_token("zovya", models)
    assert binding.disposition == "unsafe"
    assert "identity_unsupported" in binding.reasons


@pytest.mark.parametrize(
    "unsafe_id", ["CX/GPT-6-SOL ", "cx/gpt-6-sol (old)", "CX/GPT-6-SOL\x1b", "cx/\u212a"]
)
def test_unsafe_row_matching_selected_id_makes_it_ambiguous(tmp_path: Path, unsafe_id: str) -> None:
    """Review blocker: an unsafe row that Prime could still match for the selected
    exact id (case-insensitive substring of its id) must make that id ambiguous.
    It must never bind to the safe row or become selectable."""
    models = registry()
    token = "cx/k" if unsafe_id == "cx/\u212a" else "cx/gpt-6-sol"
    if token == "cx/k":
        models["providers"]["omniroute"]["models"].append({"id": "cx/k", "name": "cx/k"})
    models["providers"]["omniroute"]["models"].append({"id": unsafe_id, "name": "unrelated"})
    binding = bind_prime_token(token, models)
    assert binding.disposition == "unsafe"
    assert "identity_unsupported" in binding.reasons
    settings, deps = inputs(tmp_path)
    deps = replace(deps, registry_bytes=json.dumps(models).encode())
    plan = preview_selection(
        settings, [row(token)], selected_ids=[token], dependencies=deps, now=NOW
    )
    assert plan.refusals
    assert not any(r.selectable for r in plan.rows)
    assert unsafe_id not in json.dumps(plan.to_dict(), default=str)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["providers"]["omniroute"].update(models="not-a-list"),
        lambda m: m["providers"]["omniroute"]["models"].append({"id": 123, "name": "x"}),
        lambda m: m["providers"].update(omniroute="not-a-mapping"),
        lambda m: m.update(providers="not-a-mapping"),
    ],
)
def test_structurally_invalid_registry_still_refused(tmp_path: Path, mutate: Any) -> None:
    """Defect 1 (c): structural invalidity (non-list models, non-str id, a
    provider config/providers that is not a mapping) still raises, unlike an
    individual unsafe-but-well-typed row.
    """
    models = registry()
    mutate(models)
    with pytest.raises(ValueError, match="registry_invalid"):
        bind_prime_token("cx/gpt-6-sol", models)


def test_consume_prime_preview_with_unsafe_row_never_echoes_raw_unsafe_id(tmp_path: Path) -> None:
    """Defect 1 (d)+(e): an end-to-end preview_selection using a registry fixture
    that contains an unsafe row (no live calls, injected rows) succeeds, and
    no unsafe raw id/name ever appears in the preview JSON/text.
    """
    models = registry()
    models["providers"]["omniroute"]["models"].append(
        {"id": "aihorde/A-Zovya RPG Inpainting", "name": "aihorde/A-Zovya RPG Inpainting"}
    )
    settings, deps = inputs(tmp_path)
    deps = replace(deps, registry_bytes=json.dumps(models).encode())
    plan = preview_selection(
        settings, [row()], selected_ids=["cx/gpt-6-sol"], dependencies=deps, now=NOW
    )
    assert not plan.refusals
    text = json.dumps(plan.to_dict())
    assert "A-Zovya" not in text
    assert "Inpainting" not in text
