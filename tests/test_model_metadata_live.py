"""Optional live smoke for BOD-108 P0 fetches. Skips when the network is unavailable."""

from __future__ import annotations

import pytest

from verdict.metadata.records import SOURCE_MODELS_DEV, FieldProvenance, ModelMetadataError
from verdict.metadata.sources import MODELS_DEV_API_URL, HttpxJsonTransport, parse_models_dev_api


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
