"""Offline unit tests for verdict.discovery.sources.

All tests run with no network access.  HTTP calls are replaced by
:func:`tests.helpers_discovery.make_stub_fetcher` or by passing
fixture dicts directly to the parse functions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from tests.helpers_discovery import make_stub_fetcher
from verdict.discovery.collect import collect_sources
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


class TestCollectSourcesErrors:
    """Failing-first tests: errors must be recorded, not silently swallowed."""

    def test_failed_source_recorded_as_error_not_silent_success(self) -> None:
        """A source that raises must appear in errors, not as empty success."""
        from verdict.discovery.collect import CollectResult

        def bad_fetcher(url: str, **kwargs: object) -> bytes:
            raise OSError("simulated network failure")

        result = collect_sources(
            fetcher=bad_fetcher,
            include_omniroute=False,
            include_kilo=True,
            include_openrouter=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        assert isinstance(result, CollectResult), "collect_sources must return CollectResult"
        assert len(result.errors) == 1
        err = result.errors[0]
        assert err.source == "kilo"
        assert err.error_type == "OSError"
        assert "simulated network failure" in err.message

    def test_error_has_source_type_and_message_fields(self) -> None:
        """SourceError must carry source, error_type, and message."""
        from verdict.discovery.collect import CollectResult, SourceError

        def bad_fetcher(url: str, **kwargs: object) -> bytes:
            raise ValueError("bad payload")

        result = collect_sources(
            fetcher=bad_fetcher,
            include_omniroute=False,
            include_openrouter=True,
            include_kilo=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        assert isinstance(result, CollectResult)
        assert result.errors, "expected at least one error"
        for err in result.errors:
            assert isinstance(err, SourceError)
            assert err.source
            assert err.error_type
            assert err.message

    def test_successful_sources_still_returned_alongside_errors(self) -> None:
        """Rows from good sources must survive even when another source fails."""
        from verdict.discovery.sources.openrouter import OPENROUTER_MODELS_URL

        good_payload: dict[str, object] = {
            "data": [{"id": "openrouter/x", "pricing": {"prompt": "0", "completion": "0"}}]
        }

        def mixed_fetcher(url: str, **kwargs: object) -> bytes:
            import json

            if url == OPENROUTER_MODELS_URL:
                return json.dumps(good_payload).encode()
            raise OSError("kilo/models_dev/community network down")

        result = collect_sources(
            fetcher=mixed_fetcher,
            include_omniroute=False,
            include_openrouter=True,
            include_kilo=True,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        assert any(r.source == "openrouter" for r in result.rows)
        assert any(e.source == "kilo" for e in result.errors)


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
        result = collect_sources(fetcher=self._make_fetcher(), include_omniroute=False)
        assert len(result.rows) > 0
        sources_seen = {r.source for r in result.rows}
        assert "openrouter" in sources_seen
        assert "kilo" in sources_seen
        assert "models_dev" in sources_seen
        assert "free_coding_models" in sources_seen
        assert "dsh_our_free_model" in sources_seen

    def test_each_row_has_required_fields(self) -> None:
        result = collect_sources(fetcher=self._make_fetcher(), include_omniroute=False)
        for row in result.rows:
            assert isinstance(row.id, str) and row.id
            assert isinstance(row.provider, str) and row.provider
            assert isinstance(row.source, str) and row.source
            assert isinstance(row.source_url, str) and row.source_url
            assert isinstance(row.fetched_at, str) and row.fetched_at

    def test_failed_source_does_not_abort(self) -> None:

        # Stub with Kilo missing (will raise KeyError → caught as Exception)
        from verdict.discovery.sources.openrouter import OPENROUTER_MODELS_URL

        stub = make_stub_fetcher({OPENROUTER_MODELS_URL: self._openrouter_payload()})
        result = collect_sources(
            fetcher=stub,
            include_omniroute=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        # Kilo failed but openrouter still returned rows
        assert any(r.source == "openrouter" for r in result.rows)
        # Kilo failure is now recorded
        assert any(e.source == "kilo" for e in result.errors)

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
        result = collect_sources(
            omniroute_evidence=evidence,
            fetcher=make_stub_fetcher({}),
            include_openrouter=False,
            include_kilo=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        assert len(result.rows) == 1
        assert result.rows[0].id == "groq/llama-3.3-70b"
        assert result.rows[0].source == "omniroute"

    def test_toggle_disable_sources(self) -> None:
        result = collect_sources(
            fetcher=self._make_fetcher(),
            include_omniroute=False,
            include_openrouter=False,
            include_kilo=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        assert result.rows == []
        assert result.errors == []


# ---------------------------------------------------------------------------
# refresh_discovery_sources.py run() behavior
# ---------------------------------------------------------------------------


class TestRefreshScriptBehavior:
    """Tests for scripts/refresh_discovery_sources.py run() function.

    Fully offline: HTTP calls injected via the ``fetcher`` parameter added to
    ``run()`` for testability; OmniRoute evidence is monkeypatched.
    No live or paid provider calls.
    """

    _OPENROUTER_PAYLOAD: ClassVar[dict] = {
        "data": [
            {
                "id": "openrouter/google/gemini-flash:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 1000000,
            }
        ]
    }

    def test_stale_cache_removed_on_source_failure(self, tmp_path: Path) -> None:
        """After a successful run a subsequent failure must leave no stale rows on disk."""
        from scripts.refresh_discovery_sources import run as refresh_run
        from verdict.discovery.sources.openrouter import OPENROUTER_MODELS_URL

        cache_dir = tmp_path / "sources"

        # First run: openrouter succeeds → cache written.
        success_fetcher = make_stub_fetcher({OPENROUTER_MODELS_URL: self._OPENROUTER_PAYLOAD})
        refresh_run(sources=["openrouter"], cache_dir=cache_dir, fetcher=success_fetcher)
        stale = cache_dir / "openrouter.json"
        assert stale.exists(), "sanity: cache must be written on success"

        # Second run: openrouter fails (no URL in stub → KeyError).
        fail_fetcher = make_stub_fetcher({})
        refresh_run(sources=["openrouter"], cache_dir=cache_dir, fetcher=fail_fetcher)

        # Stale file must be gone so readers cannot see stale rows as fresh.
        assert not stale.exists(), "stale cache file must be removed after source failure"
        # Index must record the error, not an empty-success.
        idx = json.loads((cache_dir / "_index.json").read_text())
        entry = next(e for e in idx["sources"] if e["source"] == "openrouter")
        assert entry["error"] is not None, "index must record the error"
        assert entry["row_count"] == 0

    def test_omniroute_failure_no_traceback_in_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OmniRoute fetch failure must not dump a full traceback to stderr."""
        import verdict.omniroute_evidence as omni_mod
        from scripts.refresh_discovery_sources import run as refresh_run

        # The key prefix + "auth failed: " prefix together leave < 200 chars for the
        # X's, so the 200-char tail is split across the truncation boundary — the full
        # 200 consecutive X's cannot appear in logged output.
        fake_key_xs = "X" * 200
        fake_key = "sk-ant-api03-" + fake_key_xs

        def _raise(*args: object, **kwargs: object) -> None:
            raise RuntimeError(f"auth failed: {fake_key}")

        monkeypatch.setattr(omni_mod, "fetch_omniroute_evidence", _raise)

        refresh_run(sources=["omniroute"], cache_dir=tmp_path / "s")
        captured = capsys.readouterr()

        assert "Traceback (most recent call last):" not in captured.err, (
            "traceback.print_exc must not be called on adapter failures"
        )
        # The 200 consecutive X's must not appear because the message is truncated to 200 chars,
        # and the prefix ("auth failed: sk-ant-api03-") already consumes chars before the X's.
        assert fake_key_xs not in captured.err, (
            "exception message must be truncated so long secrets are not fully logged"
        )

    def test_index_entries_have_uniform_schema(self, tmp_path: Path) -> None:
        """Every _index.json sources entry must carry the same set of keys."""
        from scripts.refresh_discovery_sources import run as refresh_run
        from verdict.discovery.sources.openrouter import OPENROUTER_MODELS_URL

        # openrouter succeeds; kilo has no URL in stub so collect_sources records an error.
        fetcher = make_stub_fetcher({OPENROUTER_MODELS_URL: self._OPENROUTER_PAYLOAD})
        refresh_run(sources=["openrouter", "kilo"], cache_dir=tmp_path / "s", fetcher=fetcher)

        idx = json.loads((tmp_path / "s" / "_index.json").read_text())
        assert idx["sources"], "must have at least one entry"

        expected_keys = {"source", "row_count", "error", "free_tier_warning", "fetched_at"}
        for entry in idx["sources"]:
            assert set(entry.keys()) == expected_keys, (
                f"entry '{entry['source']}' has schema {set(entry.keys())}, "
                f"expected {expected_keys}"
            )


# ---------------------------------------------------------------------------
# Round-3 OCR finding tests
# ---------------------------------------------------------------------------

# Finding 1 (additional tests) -----------------------------------------------


class TestRefreshSuccessWithZeroRows:
    """Finding 1: a success that returns zero rows must overwrite the stale cache."""

    _OPENROUTER_PAYLOAD: ClassVar[dict] = {
        "data": [
            {
                "id": "openrouter/google/gemini-flash:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 1000000,
            }
        ]
    }

    def test_network_success_with_zero_rows_overwrites_stale_cache(self, tmp_path: Path) -> None:
        from scripts.refresh_discovery_sources import run as refresh_run
        from verdict.discovery.sources.openrouter import OPENROUTER_MODELS_URL

        cache_dir = tmp_path / "sources"

        # First run: openrouter succeeds with rows.
        success = make_stub_fetcher({OPENROUTER_MODELS_URL: self._OPENROUTER_PAYLOAD})
        refresh_run(sources=["openrouter"], cache_dir=cache_dir, fetcher=success)
        assert (cache_dir / "openrouter.json").exists(), "sanity: cache written on success"

        # Second run: openrouter succeeds but returns zero rows (empty data list).
        empty = make_stub_fetcher({OPENROUTER_MODELS_URL: {"data": []}})
        refresh_run(sources=["openrouter"], cache_dir=cache_dir, fetcher=empty)

        # Cache file must exist (overwritten with empty rows — not stale rows).
        assert (cache_dir / "openrouter.json").exists(), (
            "cache file must be (re)written on success even when row count is zero"
        )
        import json as _json

        written = _json.loads((cache_dir / "openrouter.json").read_text())
        assert written["row_count"] == 0, "re-written file must reflect zero rows"

    def test_omniroute_success_with_zero_rows_overwrites_stale_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone

        import verdict.omniroute_evidence as omni_mod
        from scripts.refresh_discovery_sources import run as refresh_run

        cache_dir = tmp_path / "sources"

        evidence_one = omni_mod.parse_omniroute_evidence(
            v1_models={"data": [{"id": "groq/llama-3.3-70b", "owned_by": "groq"}]},
            providers=None,
            resilience=None,
            limits=None,
            cooldowns=None,
            health_matrix=None,
            now=datetime(2026, 11, 20, tzinfo=timezone.utc),
        )
        evidence_zero = omni_mod.parse_omniroute_evidence(
            v1_models={"data": []},
            providers=None,
            resilience=None,
            limits=None,
            cooldowns=None,
            health_matrix=None,
            now=datetime(2026, 11, 20, tzinfo=timezone.utc),
        )

        # First call succeeds with rows; second returns empty.
        call_count = {"n": 0}

        def _evidence(*a: object, **kw: object) -> object:
            call_count["n"] += 1
            return evidence_one if call_count["n"] == 1 else evidence_zero

        monkeypatch.setattr(omni_mod, "fetch_omniroute_evidence", _evidence)

        refresh_run(sources=["omniroute"], cache_dir=cache_dir)
        assert (cache_dir / "omniroute.json").exists(), "sanity"

        refresh_run(sources=["omniroute"], cache_dir=cache_dir)
        assert (cache_dir / "omniroute.json").exists(), (
            "omniroute cache must be (re)written on success even when rows = 0"
        )
        import json as _json

        written = _json.loads((cache_dir / "omniroute.json").read_text())
        assert written["row_count"] == 0


# Finding 2 ------------------------------------------------------------------


class TestRefreshOnlyRequestedCommunitySourcesFetched:
    """Finding 2: --sources free_coding_models must not also fetch dsh_our_free_model."""

    def test_only_fcm_requested_fetches_only_fcm(self, tmp_path: Path) -> None:
        from scripts.refresh_discovery_sources import run as refresh_run
        from verdict.discovery.sources.community import (
            _DSH_DEFAULT_COMMIT,
            _DSH_FILE,
            _DSH_REPO,
            _FCM_DEFAULT_COMMIT,
            _FCM_FILE,
            _FCM_REPO,
            _GITHUB_RAW,
        )

        calls: list[str] = []

        def _recording_fetcher(url: str, **kw: object) -> bytes:
            calls.append(url)
            # Return minimal valid payloads for URLs we expect; raise for unexpected.
            fcm_url = f"{_GITHUB_RAW}/{_FCM_REPO}/{_FCM_DEFAULT_COMMIT}/{_FCM_FILE}"
            if url == fcm_url:
                return b'export const test = [["model-a", "Model A", "B", 20, "32k"]];'
            raise KeyError(f"unexpected URL: {url!r}")

        refresh_run(
            sources=["free_coding_models"], cache_dir=tmp_path / "s", fetcher=_recording_fetcher
        )

        dsh_url = f"{_GITHUB_RAW}/{_DSH_REPO}/{_DSH_DEFAULT_COMMIT}/{_DSH_FILE}"
        assert dsh_url not in calls, (
            "requesting only free_coding_models must not fetch dsh_our_free_model"
        )

    def test_only_dsh_requested_fetches_only_dsh(self, tmp_path: Path) -> None:
        from scripts.refresh_discovery_sources import run as refresh_run
        from verdict.discovery.sources.community import (
            _DSH_DEFAULT_COMMIT,
            _DSH_FILE,
            _DSH_REPO,
            _FCM_DEFAULT_COMMIT,
            _FCM_FILE,
            _FCM_REPO,
            _GITHUB_RAW,
        )

        calls: list[str] = []

        def _recording_fetcher(url: str, **kw: object) -> bytes:
            calls.append(url)
            dsh_url = f"{_GITHUB_RAW}/{_DSH_REPO}/{_DSH_DEFAULT_COMMIT}/{_DSH_FILE}"
            if url == dsh_url:
                return b'[{"id": "prov1", "models": [{"id": "m1"}]}]'
            raise KeyError(f"unexpected URL: {url!r}")

        refresh_run(
            sources=["dsh_our_free_model"], cache_dir=tmp_path / "s", fetcher=_recording_fetcher
        )

        fcm_url = f"{_GITHUB_RAW}/{_FCM_REPO}/{_FCM_DEFAULT_COMMIT}/{_FCM_FILE}"
        assert fcm_url not in calls, (
            "requesting only dsh_our_free_model must not fetch free_coding_models"
        )


# Finding 3 ------------------------------------------------------------------


class TestCollectSourcesErrorKind:
    """Finding 3: adapter bugs get a distinct error_kind from expected outages."""

    def test_outage_errors_have_source_error_kind(self) -> None:
        from urllib.error import URLError

        from verdict.discovery.collect import collect_sources

        def _raises(*a: object, **kw: object) -> bytes:
            raise URLError("connection refused")

        result = collect_sources(
            fetcher=_raises,
            include_omniroute=False,
            include_openrouter=True,
            include_kilo=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        assert len(result.errors) == 1
        err = result.errors[0]
        assert err.error_kind == "source_error", (
            f"URLError is a known outage; error_kind should be 'source_error', got {err.error_kind!r}"
        )

    def test_adapter_bug_has_adapter_bug_kind(self) -> None:
        from verdict.discovery.collect import collect_sources

        def _buggy_fetcher(*a: object, **kw: object) -> bytes:
            raise AttributeError("adapter_internal_bug")

        result = collect_sources(
            fetcher=_buggy_fetcher,
            include_omniroute=False,
            include_openrouter=True,
            include_kilo=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        assert len(result.errors) == 1
        err = result.errors[0]
        assert err.error_kind == "adapter_bug", (
            f"AttributeError is an unexpected bug; error_kind should be 'adapter_bug', got {err.error_kind!r}"
        )

    def test_message_sanitizes_credential_params(self) -> None:
        """Exception messages containing credential fragments are sanitized."""
        from verdict.discovery.collect import collect_sources

        def _leaky_fetcher(*a: object, **kw: object) -> bytes:
            raise OSError("HTTP 403 at https://api.example.com/v1/models?api_key=sk-secret-123")

        result = collect_sources(
            fetcher=_leaky_fetcher,
            include_omniroute=False,
            include_openrouter=True,
            include_kilo=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=False,
        )
        assert len(result.errors) == 1
        err = result.errors[0]
        assert "sk-secret-123" not in err.message, (
            "credential values must be redacted from error messages"
        )


# Finding 4 ------------------------------------------------------------------


class TestSharedPriceParser:
    """Finding 4: shared price parser accepts numeric strings."""

    def test_zero_string_kilo(self) -> None:
        from verdict.discovery.sources.kilo import parse_kilo

        rows = parse_kilo([{"id": "kc/m", "pricing": {"prompt": "0", "completion": "0"}}])
        assert rows[0].input_price == 0.0
        assert rows[0].output_price == 0.0

    def test_fractional_string_kilo(self) -> None:
        from verdict.discovery.sources.kilo import parse_kilo

        rows = parse_kilo(
            [{"id": "kc/m", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]
        )
        assert rows[0].input_price == pytest.approx(0.000001)
        assert rows[0].output_price == pytest.approx(0.000002)

    def test_zero_string_models_dev(self) -> None:
        from verdict.discovery.sources.models_dev import parse_models_dev

        rows = parse_models_dev(
            {"models": [{"id": "prov/m", "pricing": {"prompt": "0", "completion": "0"}}]}
        )
        assert rows[0].input_price == 0.0
        assert rows[0].output_price == 0.0

    def test_fractional_string_models_dev(self) -> None:
        from verdict.discovery.sources.models_dev import parse_models_dev

        rows = parse_models_dev(
            {
                "models": [
                    {"id": "prov/m", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
                ]
            }
        )
        assert rows[0].input_price == pytest.approx(0.000001)
        assert rows[0].output_price == pytest.approx(0.000002)

    def test_numeric_int_accepted(self) -> None:
        from verdict.discovery.sources.kilo import parse_kilo

        rows = parse_kilo([{"id": "kc/m", "pricing": {"prompt": 0, "completion": 5}}])
        assert rows[0].input_price == 0.0
        assert rows[0].output_price == 5.0

    def test_none_returns_none(self) -> None:
        from verdict.discovery.sources.kilo import parse_kilo

        rows = parse_kilo([{"id": "kc/m"}])
        assert rows[0].input_price is None
        assert rows[0].output_price is None

    def test_garbage_string_returns_none(self) -> None:
        from verdict.discovery.sources.kilo import parse_kilo

        rows = parse_kilo([{"id": "kc/m", "pricing": {"prompt": "abc", "completion": "xyz"}}])
        assert rows[0].input_price is None
        assert rows[0].output_price is None


# Finding 5 ------------------------------------------------------------------


class TestFetchDSHMalformedJSON:
    """Finding 5: fetch_dsh raises on malformed JSON."""

    def test_raises_on_malformed_json(self) -> None:
        import json as _json

        from verdict.discovery.sources.community import (
            _DSH_DEFAULT_COMMIT,
            _DSH_FILE,
            _DSH_REPO,
            _GITHUB_RAW,
            fetch_dsh,
        )

        dsh_url = f"{_GITHUB_RAW}/{_DSH_REPO}/{_DSH_DEFAULT_COMMIT}/{_DSH_FILE}"
        stub = make_stub_fetcher({dsh_url: b"not valid json {"})
        with pytest.raises(_json.JSONDecodeError):
            fetch_dsh(fetcher=stub)

    def test_collect_sources_records_dsh_json_error_as_source_error(self) -> None:
        """DSH JSON errors are expected outages, not adapter bugs."""
        from verdict.discovery.collect import collect_sources
        from verdict.discovery.sources.community import (
            _DSH_DEFAULT_COMMIT,
            _DSH_FILE,
            _DSH_REPO,
            _GITHUB_RAW,
        )

        dsh_url = f"{_GITHUB_RAW}/{_DSH_REPO}/{_DSH_DEFAULT_COMMIT}/{_DSH_FILE}"
        stub = make_stub_fetcher({dsh_url: b"not valid json {"})
        result = collect_sources(
            fetcher=stub,
            include_omniroute=False,
            include_openrouter=False,
            include_kilo=False,
            include_models_dev=False,
            include_fcm=False,
            include_dsh=True,
        )
        assert len(result.errors) == 1
        err = result.errors[0]
        assert err.source == "dsh_our_free_model"
        assert err.error_kind == "source_error"


# Finding 6 ------------------------------------------------------------------


class TestFetcherExactByteLimit:
    """Finding 6: default_fetcher never overshoots max_bytes by a chunk."""

    def test_response_at_exact_limit_succeeds(self) -> None:
        """A response of exactly max_bytes bytes is returned without error."""
        from io import BytesIO
        from unittest.mock import patch

        from verdict.discovery.sources._fetcher import default_fetcher

        payload = b"x" * 10

        class _ExactResponse:
            def __init__(self) -> None:
                self._buf = BytesIO(payload)

            def read(self, n: int) -> bytes:
                return self._buf.read(n)

            def __enter__(self) -> _ExactResponse:
                return self

            def __exit__(self, *a: object) -> None:
                pass

        with patch("verdict.http_safety.open_no_redirect", return_value=_ExactResponse()):
            result = default_fetcher("http://example.com", max_bytes=10)
        assert result == payload

    def test_read_does_not_overshoot_max_bytes_by_a_chunk(self) -> None:
        """With max_bytes=5, each read requests at most max_bytes+1 bytes, not chunk_size."""
        from io import BytesIO
        from unittest.mock import patch

        from verdict.discovery.sources._fetcher import default_fetcher

        read_sizes: list[int] = []

        class _TrackingResponse:
            def __init__(self, data: bytes) -> None:
                self._buf = BytesIO(data)

            def read(self, n: int) -> bytes:
                read_sizes.append(n)
                return self._buf.read(n)

            def __enter__(self) -> _TrackingResponse:
                return self

            def __exit__(self, *a: object) -> None:
                pass

        with patch(
            "verdict.http_safety.open_no_redirect", return_value=_TrackingResponse(b"x" * 4)
        ):
            result = default_fetcher("http://example.com", max_bytes=5)

        assert result == b"x" * 4
        # With fix: first read requests min(65536, 5-0+1) = 6, not 65536.
        assert all(n <= 6 for n in read_sizes), (
            f"read sizes {read_sizes} should all be ≤ 6 with max_bytes=5"
        )
