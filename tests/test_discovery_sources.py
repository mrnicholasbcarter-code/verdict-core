"""Offline unit tests for verdict.discovery.sources.

All tests run with no network access.  HTTP calls are replaced by
:func:`~verdict.discovery.sources._fetcher.make_stub_fetcher` or by passing
fixture dicts directly to the parse functions.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from verdict.discovery.collect import collect_sources
from verdict.discovery.sources._fetcher import make_stub_fetcher
from verdict.discovery.sources._types import DiscoveryRow
from verdict.discovery.sources.community import (
    _DSH_DEFAULT_COMMIT,
    _DSH_FILE,
    _DSH_REPO,
    _FCM_DEFAULT_COMMIT,
    _FCM_FILE,
    _FCM_REPO,
    _GITHUB_RAW,
    fetch_dsh,
    fetch_fcm,
    parse_dsh,
    parse_fcm,
)
from verdict.discovery.sources.kilo import fetch_kilo, parse_kilo
from verdict.discovery.sources.models_dev import parse_models_dev
from verdict.discovery.sources.omniroute import parse_free_tier_ids, rows_from_evidence
from verdict.discovery.sources.openrouter import fetch_openrouter, parse_openrouter

# ---------------------------------------------------------------------------
# DiscoveryRow
# ---------------------------------------------------------------------------


class TestDiscoveryRow:
    def test_to_dict_round_trip(self) -> None:
        row = DiscoveryRow(
            id="groq/llama-3.3-70b-versatile",
            provider="groq",
            source="openrouter",
            source_url="https://openrouter.ai/api/v1/models",
            fetched_at="2026-11-20T00:00:00+00:00",
            input_price=0.0,
            output_price=0.0,
            free_flag=True,
            context_length=131072,
            supports_tools=True,
        )
        d = row.to_dict()
        assert d["id"] == "groq/llama-3.3-70b-versatile"
        assert d["input_price"] == 0.0
        assert d["free_flag"] is True
        assert d["context_length"] == 131072
        assert d["license_restricted"] is False

    def test_none_fields_serialise(self) -> None:
        row = DiscoveryRow(
            id="test/model",
            provider="test",
            source="kilo",
            source_url="https://example.com/models",
            fetched_at="2026-11-20T00:00:00+00:00",
        )
        d = row.to_dict()
        assert d["input_price"] is None
        assert d["free_flag"] is None
        assert d["supports_tools"] is None


# ---------------------------------------------------------------------------
# OmniRoute adapter
# ---------------------------------------------------------------------------


class TestOmniRouteAdapter:
    """Tests for rows_from_evidence and parse_free_tier_ids."""

    def _make_evidence(self, inventory: dict, connections_source: str = "management") -> object:
        """Build a minimal OmniRouteEvidence-like object from dicts."""
        from datetime import datetime, timezone

        from verdict.omniroute_evidence import parse_omniroute_evidence

        v1_models_payload: dict = {"data": list(inventory.values())}
        return parse_omniroute_evidence(
            v1_models=v1_models_payload, now=datetime(2026, 11, 20, tzinfo=timezone.utc)
        )

    def test_rows_extracted_from_inventory(self) -> None:
        from datetime import datetime, timezone

        from verdict.omniroute_evidence import parse_omniroute_evidence

        v1_payload = {
            "data": [
                {
                    "id": "groq/llama-3.3-70b-versatile",
                    "owned_by": "groq",
                    "pricing": {"prompt": 0, "completion": 0},
                    "context_length": 131072,
                }
            ]
        }
        evidence = parse_omniroute_evidence(
            v1_models=v1_payload,
            providers=None,
            resilience=None,
            limits=None,
            cooldowns=None,
            health_matrix=None,
            now=datetime(2026, 11, 20, tzinfo=timezone.utc),
        )
        result = rows_from_evidence(evidence)
        assert len(result) == 1
        row = result[0]
        assert row.id == "groq/llama-3.3-70b-versatile"
        assert row.provider == "groq"
        assert row.input_price == 0.0
        assert row.output_price == 0.0
        assert row.context_length == 131072
        assert row.source == "omniroute"

    def test_opaque_routes_excluded(self) -> None:
        from datetime import datetime, timezone

        from verdict.omniroute_evidence import parse_omniroute_evidence

        v1_payload = {
            "data": [
                {"id": "auto/best", "owned_by": "auto"},
                {"id": "combo/fast", "owned_by": "combo"},
                {"id": "groq/valid", "owned_by": "groq"},
            ]
        }
        evidence = parse_omniroute_evidence(
            v1_models=v1_payload,
            providers=None,
            resilience=None,
            limits=None,
            cooldowns=None,
            health_matrix=None,
            now=datetime(2026, 11, 20, tzinfo=timezone.utc),
        )
        result = rows_from_evidence(evidence)
        ids = {r.id for r in result}
        assert "auto/best" not in ids
        assert "combo/fast" not in ids
        assert "groq/valid" in ids

    def test_free_tier_ids_set_free_flag(self) -> None:
        from datetime import datetime, timezone

        from verdict.omniroute_evidence import parse_omniroute_evidence

        v1_payload = {
            "data": [
                {"id": "openrouter/google/gemini-2.0-flash-exp:free", "owned_by": "openrouter"},
                {"id": "groq/llama-3.3-70b", "owned_by": "groq"},
            ]
        }
        evidence = parse_omniroute_evidence(
            v1_models=v1_payload,
            providers=None,
            resilience=None,
            limits=None,
            cooldowns=None,
            health_matrix=None,
            now=datetime(2026, 11, 20, tzinfo=timezone.utc),
        )
        free_ids = ["openrouter/google/gemini-2.0-flash-exp:free"]
        result = rows_from_evidence(evidence, free_tier_ids=free_ids)
        by_id = {r.id: r for r in result}
        assert by_id["openrouter/google/gemini-2.0-flash-exp:free"].free_flag is True
        assert by_id["groq/llama-3.3-70b"].free_flag is False

    def test_parse_free_tier_ids_valid(self) -> None:
        payload = {"perModel": ["provider/a", "provider/b", "provider/c"]}
        ids = parse_free_tier_ids(payload)
        assert "provider/a" in ids
        assert len(ids) == 3

    def test_parse_free_tier_ids_invalid(self) -> None:
        assert parse_free_tier_ids(None) == frozenset()
        assert parse_free_tier_ids({}) == frozenset()
        assert parse_free_tier_ids("bad") == frozenset()
        assert parse_free_tier_ids({"perModel": "not-a-list"}) == frozenset()

    def test_no_rows_on_empty_inventory(self) -> None:
        from datetime import datetime, timezone

        from verdict.omniroute_evidence import parse_omniroute_evidence

        evidence = parse_omniroute_evidence(
            v1_models={"data": []},
            providers=None,
            resilience=None,
            limits=None,
            cooldowns=None,
            health_matrix=None,
            now=datetime(2026, 11, 20, tzinfo=timezone.utc),
        )
        result = rows_from_evidence(evidence)
        assert result == []


# ---------------------------------------------------------------------------
# OpenRouter adapter
# ---------------------------------------------------------------------------


class TestOpenRouterAdapter:
    def _payload(self) -> dict:
        return {
            "data": [
                {
                    "id": "openrouter/google/gemini-2.0-flash-exp:free",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "context_length": 1048576,
                    "supported_parameters": [{"name": "tools"}, {"name": "temperature"}],
                },
                {
                    "id": "openrouter/anthropic/claude-3.5-sonnet",
                    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                    "context_length": 200000,
                    "supported_parameters": ["tools", "max_tokens"],
                },
                {
                    "id": "auto/best",  # opaque, should be skipped
                    "pricing": {},
                },
            ]
        }

    def test_parse_basic(self) -> None:
        rows = parse_openrouter(self._payload())
        ids = {r.id for r in rows}
        assert "openrouter/google/gemini-2.0-flash-exp:free" in ids
        assert "openrouter/anthropic/claude-3.5-sonnet" in ids
        assert "auto/best" not in ids

    def test_pricing_extracted(self) -> None:
        rows = parse_openrouter(self._payload())
        by_id = {r.id: r for r in rows}
        gemini = by_id["openrouter/google/gemini-2.0-flash-exp:free"]
        assert gemini.input_price == 0.0
        assert gemini.output_price == 0.0
        claude = by_id["openrouter/anthropic/claude-3.5-sonnet"]
        assert claude.input_price == pytest.approx(0.000003)
        assert claude.output_price == pytest.approx(0.000015)

    def test_tools_detected(self) -> None:
        rows = parse_openrouter(self._payload())
        by_id = {r.id: r for r in rows}
        gemini = by_id["openrouter/google/gemini-2.0-flash-exp:free"]
        assert gemini.supports_tools is True
        claude = by_id["openrouter/anthropic/claude-3.5-sonnet"]
        assert claude.supports_tools is True

    def test_context_length(self) -> None:
        rows = parse_openrouter(self._payload())
        by_id = {r.id: r for r in rows}
        assert by_id["openrouter/google/gemini-2.0-flash-exp:free"].context_length == 1048576

    def test_empty_payload(self) -> None:
        assert parse_openrouter({}) == []
        assert parse_openrouter({"data": []}) == []
        assert parse_openrouter("bad") == []

    def test_fetch_via_stub(self) -> None:
        from verdict.discovery.sources.openrouter import OPENROUTER_MODELS_URL

        stub = make_stub_fetcher({OPENROUTER_MODELS_URL: self._payload()})
        rows = fetch_openrouter(fetcher=stub)
        assert len(rows) == 2
        assert rows[0].source == "openrouter"


# ---------------------------------------------------------------------------
# Kilo adapter
# ---------------------------------------------------------------------------


class TestKiloAdapter:
    def _payload(self) -> list:
        return [
            {"id": "llama-3.3-70b-versatile", "isFree": True, "contextLength": 131072},
            {
                "id": "claude-3.5-sonnet",
                "isFree": False,
                "pricing": {"prompt": 0.000003, "completion": 0.000015},
            },
            {"id": "free-model", "pricing": {"prompt": 0, "completion": 0}},
        ]

    def test_parse_basic(self) -> None:
        rows = parse_kilo(self._payload())
        assert len(rows) == 3

    def test_free_flag_from_is_free(self) -> None:
        rows = parse_kilo(self._payload())
        by_id = {r.id: r for r in rows}
        assert by_id["kc/llama-3.3-70b-versatile"].free_flag is True
        assert by_id["kc/claude-3.5-sonnet"].free_flag is False

    def test_free_flag_from_zero_pricing(self) -> None:
        rows = parse_kilo(self._payload())
        by_id = {r.id: r for r in rows}
        assert by_id["kc/free-model"].free_flag is True

    def test_context_length(self) -> None:
        rows = parse_kilo(self._payload())
        by_id = {r.id: r for r in rows}
        assert by_id["kc/llama-3.3-70b-versatile"].context_length == 131072

    def test_provider_prefix(self) -> None:
        rows = parse_kilo([{"id": "my-model", "isFree": True}])
        assert rows[0].provider == "kc"

    def test_empty_payload(self) -> None:
        assert parse_kilo([]) == []
        assert parse_kilo({}) == []

    def test_fetch_via_stub(self) -> None:
        from verdict.discovery.sources.kilo import KILO_MODELS_URL

        stub = make_stub_fetcher({KILO_MODELS_URL: self._payload()})
        rows = fetch_kilo(fetcher=stub)
        assert len(rows) == 3
        assert rows[0].source == "kilo"


# ---------------------------------------------------------------------------
# models.dev adapter
# ---------------------------------------------------------------------------


class TestModelsDevAdapter:
    def _payload(self) -> dict:
        return {
            "models": [
                {
                    "id": "groq/llama-3.3-70b-versatile",
                    "contextWindow": 131072,
                    "capabilities": {"tools": True},
                },
                {
                    "id": "openai/gpt-4o",
                    "contextWindow": 128000,
                    "pricing": {"prompt": 0.0000025, "completion": 0.00001},
                },
            ]
        }

    def test_parse_basic(self) -> None:
        rows = parse_models_dev(self._payload())
        assert len(rows) == 2

    def test_tools_capability(self) -> None:
        rows = parse_models_dev(self._payload())
        by_id = {r.id: r for r in rows}
        assert by_id["groq/llama-3.3-70b-versatile"].supports_tools is True

    def test_context_length(self) -> None:
        rows = parse_models_dev(self._payload())
        by_id = {r.id: r for r in rows}
        assert by_id["openai/gpt-4o"].context_length == 128000

    def test_pricing(self) -> None:
        rows = parse_models_dev(self._payload())
        by_id = {r.id: r for r in rows}
        gpt = by_id["openai/gpt-4o"]
        assert gpt.input_price == pytest.approx(0.0000025)

    def test_list_payload(self) -> None:
        payload = [{"id": "groq/model-a", "contextWindow": 8192}]
        rows = parse_models_dev(payload)
        assert len(rows) == 1

    def test_source_field(self) -> None:
        rows = parse_models_dev(self._payload())
        assert all(r.source == "models_dev" for r in rows)


# ---------------------------------------------------------------------------
# Community list adapters
# ---------------------------------------------------------------------------


class TestFCMAdapter:
    _FIXTURE_JS = """
    /**
     * sources.js
     */
    export const groq = [
      ["llama-3.3-70b-versatile", "Llama 3.3 70B", "A", 49, "128k"],
      ["llama-3.1-8b-instant", "Llama 3.1 8B", "B", 20, "128k"],
    ];

    export const cerebras = [
      ["llama3.1-70b", "Llama 3.1 70B Cerebras", "A", 45, "128k"],
    ];

    export const shouldBeSkipped = { notAnArray: true };
    """

    def test_parse_basic(self) -> None:
        rows = parse_fcm(self._FIXTURE_JS)
        assert len(rows) == 3

    def test_provider_names(self) -> None:
        rows = parse_fcm(self._FIXTURE_JS)
        providers = {r.provider for r in rows}
        assert "groq" in providers
        assert "cerebras" in providers

    def test_model_ids(self) -> None:
        rows = parse_fcm(self._FIXTURE_JS)
        ids = {r.id for r in rows}
        assert "groq/llama-3.3-70b-versatile" in ids
        assert "groq/llama-3.1-8b-instant" in ids
        assert "cerebras/llama3.1-70b" in ids

    def test_license_fields(self) -> None:
        rows = parse_fcm(self._FIXTURE_JS)
        for r in rows:
            assert r.license == "MIT-with-Enhanced-Disclaimers"
            assert r.license_restricted is False
            assert r.repo == "vava-nessa/free-coding-models"

    def test_commit_in_provenance(self) -> None:
        rows = parse_fcm(self._FIXTURE_JS, commit="abc123")
        for r in rows:
            assert r.commit == "abc123"

    def test_object_export_skipped(self) -> None:
        # An export whose body contains { ... } must not produce rows
        rows = parse_fcm(self._FIXTURE_JS)
        ids = {r.id for r in rows}
        # shouldBeSkipped is an object, not an array — must produce no rows
        assert not any("shouldBeSkipped" in rid for rid in ids)

    def test_empty_js(self) -> None:
        assert parse_fcm("") == []

    def test_hostile_js_no_eval(self) -> None:
        # Template literal or function call must not produce rows
        hostile = """
        export const evil = [`${process.exit(1)}`];
        export const fn_call = [foo()];
        """
        rows = parse_fcm(hostile)
        assert rows == []

    def test_fetch_via_stub(self) -> None:
        url = f"{_GITHUB_RAW}/{_FCM_REPO}/{_FCM_DEFAULT_COMMIT}/{_FCM_FILE}"
        stub = make_stub_fetcher({url: self._FIXTURE_JS.encode()})
        rows = fetch_fcm(fetcher=stub)
        assert len(rows) == 3

    def test_source_field(self) -> None:
        rows = parse_fcm(self._FIXTURE_JS)
        assert all(r.source == "free_coding_models" for r in rows)


class TestDSHAdapter:
    _FIXTURE: ClassVar[list[object]] = [
        {
            "id": "groq",
            "name": "Groq",
            "category": "apikey",
            "models": [
                {"id": "llama-3.3-70b-versatile", "contextWindow": 131072},
                {"id": "llama-3.1-8b-instant", "contextWindow": 131072},
            ],
        },
        {
            "id": "cerebras",
            "name": "Cerebras",
            "category": "apikey",
            "models": [
                {"id": "llama3.1-70b", "contextWindow": 131072, "vision": False, "reasoning": False}
            ],
        },
        {
            "id": "bad-provider"
            # no models key
        },
    ]

    def test_parse_basic(self) -> None:
        rows = parse_dsh(self._FIXTURE)
        assert len(rows) == 3

    def test_model_ids(self) -> None:
        rows = parse_dsh(self._FIXTURE)
        ids = {r.id for r in rows}
        assert "groq/llama-3.3-70b-versatile" in ids
        assert "cerebras/llama3.1-70b" in ids

    def test_context_length(self) -> None:
        rows = parse_dsh(self._FIXTURE)
        by_id = {r.id: r for r in rows}
        assert by_id["groq/llama-3.3-70b-versatile"].context_length == 131072

    def test_license_fields(self) -> None:
        rows = parse_dsh(self._FIXTURE)
        for r in rows:
            assert r.license == "MIT"
            assert r.license_restricted is False

    def test_missing_models_skipped(self) -> None:
        rows = parse_dsh(self._FIXTURE)
        providers = {r.provider for r in rows}
        assert "bad-provider" not in providers

    def test_empty_payload(self) -> None:
        assert parse_dsh([]) == []
        assert parse_dsh({}) == []

    def test_fetch_via_stub(self) -> None:
        url = f"{_GITHUB_RAW}/{_DSH_REPO}/{_DSH_DEFAULT_COMMIT}/{_DSH_FILE}"
        stub = make_stub_fetcher({url: self._FIXTURE})
        rows = fetch_dsh(fetcher=stub)
        assert len(rows) == 3

    def test_source_field(self) -> None:
        rows = parse_dsh(self._FIXTURE)
        assert all(r.source == "dsh_our_free_model" for r in rows)


# ---------------------------------------------------------------------------
# collect_sources
# ---------------------------------------------------------------------------


class TestCollectSources:
    """Integration-level tests: all sources offline via stubs."""

    def _openrouter_payload(self) -> dict:
        return {
            "data": [
                {
                    "id": "openrouter/google/gemini-flash:free",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "context_length": 1000000,
                }
            ]
        }

    def _kilo_payload(self) -> list:
        return [{"id": "kc-llama", "isFree": True, "contextLength": 131072}]

    def _models_dev_payload(self) -> dict:
        return {"models": [{"id": "groq/llama-3.3-70b", "contextWindow": 131072}]}

    _FCM_JS = """
    export const llm7 = [["mistral-7b", "Mistral 7B", "B", 20, "32k"]];
    """

    _DSH_JSON: ClassVar[list[object]] = [
        {"id": "llm7", "name": "LLM7", "models": [{"id": "mistral-7b"}]}
    ]

    def _make_fetcher(self) -> object:
        from verdict.discovery.sources.kilo import KILO_MODELS_URL
        from verdict.discovery.sources.models_dev import MODELS_DEV_URL
        from verdict.discovery.sources.openrouter import OPENROUTER_MODELS_URL

        fcm_url = f"{_GITHUB_RAW}/{_FCM_REPO}/{_FCM_DEFAULT_COMMIT}/{_FCM_FILE}"
        dsh_url = f"{_GITHUB_RAW}/{_DSH_REPO}/{_DSH_DEFAULT_COMMIT}/{_DSH_FILE}"
        return make_stub_fetcher(
            {
                OPENROUTER_MODELS_URL: self._openrouter_payload(),
                KILO_MODELS_URL: self._kilo_payload(),
                MODELS_DEV_URL: self._models_dev_payload(),
                fcm_url: self._FCM_JS.encode(),
                dsh_url: self._DSH_JSON,
            }
        )

    def test_collect_all_sources_offline(self) -> None:
        rows = collect_sources(fetcher=self._make_fetcher(), include_omniroute=False)
        assert len(rows) > 0
        sources_seen = {r.source for r in rows}
        assert "openrouter" in sources_seen
        assert "kilo" in sources_seen
        assert "models_dev" in sources_seen
        assert "free_coding_models" in sources_seen
        assert "dsh_our_free_model" in sources_seen

    def test_each_row_has_required_fields(self) -> None:
        rows = collect_sources(fetcher=self._make_fetcher(), include_omniroute=False)
        for row in rows:
            assert isinstance(row.id, str) and row.id
            assert isinstance(row.provider, str) and row.provider
            assert isinstance(row.source, str) and row.source
            assert isinstance(row.source_url, str) and row.source_url
            assert isinstance(row.fetched_at, str) and row.fetched_at

    def test_failed_source_does_not_abort(self) -> None:

        # Stub with Kilo missing (will raise KeyError → caught as Exception)
        from verdict.discovery.sources.openrouter import OPENROUTER_MODELS_URL

        stub = make_stub_fetcher({OPENROUTER_MODELS_URL: self._openrouter_payload()})
        rows = collect_sources(
            fetcher=stub, include_omniroute=False, include_models_dev=False, include_community=False
        )
        # Kilo failed but openrouter still returned rows
        assert any(r.source == "openrouter" for r in rows)

    def test_omniroute_with_fixture_evidence(self) -> None:
        from datetime import datetime, timezone

        from verdict.omniroute_evidence import parse_omniroute_evidence

        evidence = parse_omniroute_evidence(
            v1_models={
                "data": [{"id": "groq/llama-3.3-70b", "owned_by": "groq", "context_length": 131072}]
            },
            providers=None,
            resilience=None,
            limits=None,
            cooldowns=None,
            health_matrix=None,
            now=datetime(2026, 11, 20, tzinfo=timezone.utc),
        )
        rows = collect_sources(
            omniroute_evidence=evidence,
            fetcher=make_stub_fetcher({}),
            include_openrouter=False,
            include_kilo=False,
            include_models_dev=False,
            include_community=False,
        )
        assert len(rows) == 1
        assert rows[0].id == "groq/llama-3.3-70b"
        assert rows[0].source == "omniroute"

    def test_toggle_disable_sources(self) -> None:
        rows = collect_sources(
            fetcher=self._make_fetcher(),
            include_omniroute=False,
            include_openrouter=False,
            include_kilo=False,
            include_models_dev=False,
            include_community=False,
        )
        assert rows == []
