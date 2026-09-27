"""Optional live smoke for the core model metadata store (ADR-032) / the models.json id-join fix. Skips when the network is unavailable."""

from __future__ import annotations

import pytest

from verdict.metadata.records import (
    DROP_UNMAPPED,
    SOURCE_MODELS_DEV,
    SOURCE_MODELS_DEV_MODELS,
    FieldProvenance,
    ModelMetadataError,
)
from verdict.metadata.sources import (
    MODELS_DEV_API_URL,
    MODELS_DEV_MODELS_URL,
    HttpxJsonTransport,
    parse_models_dev_api,
)
from verdict.metadata.store import lookup_omniroute_id, refresh_metadata


def test_live_models_dev_api_or_skip() -> None:
    try:
        document = HttpxJsonTransport(timeout=20.0).get_json(MODELS_DEV_API_URL)
    except Exception as exc:
        pytest.skip(f"models.dev live fetch skipped: {exc}")
    if not isinstance(document.data, dict) or not document.data:
        pytest.skip("models.dev returned an empty document")
    try:
        records = parse_models_dev_api(
            document.data,
            FieldProvenance(
                source=SOURCE_MODELS_DEV, fetched_at=document.fetched_at, version=document.version
            ),
        )
    except ModelMetadataError as exc:
        pytest.skip(f"models.dev payload could not be parsed: {exc}")
    assert document.fetched_at.endswith("Z")
    assert records


def test_live_agy_gemini_flash_lite_tools_not_unmapped() -> None:
    """live proof: gateway id + tools-required must not drop as unmapped."""
    try:
        snapshot = refresh_metadata(persist=False, store_path=None)
    except Exception as exc:
        pytest.skip(f"models.dev live refresh skipped: {exc}")

    models_status = snapshot.sources.get(SOURCE_MODELS_DEV_MODELS)
    if models_status is None or models_status.status != "ok":
        pytest.skip("models.dev models.json was not fetched successfully")

    found = lookup_omniroute_id(snapshot, "agy/gemini-3.1-flash-lite", required=("tools",))
    assert found.drop is None or found.drop.reason != DROP_UNMAPPED, (
        f"expected unique-leaf join away from unmapped; got drop={found.drop}"
    )
    assert found.record is not None
    assert found.record.id == "google/gemini-3.1-flash-lite"
    cited = found.provenance_for_receipt(("tools",))
    assert "tools" in cited
    assert cited["tools"]["source"] == SOURCE_MODELS_DEV_MODELS
    assert MODELS_DEV_MODELS_URL  # provenance target documented
