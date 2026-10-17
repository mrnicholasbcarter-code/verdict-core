"""Report card from reachable data only (spec v6.1 phase c, open item O6)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.xfail(
    strict=True, raises=ImportError, reason="phase c: report card not implemented yet"
)


def test_report_card_degrades_without_management_reads() -> None:
    from verdict.omniroute_pipeline.report_card import (
        build_report_card,  # type: ignore[import-not-found]
    )

    card = build_report_card(
        scoped_catalog={"nvidia": ["nvidia/z-ai/glm-5.3"]},
        management=None,
        passports={},
        probes={},
        free_flags=None,
    )
    assert card.degraded is True
    assert card.banner == "degraded: no management reads"


def test_worst_source_wins_with_conflict_shown() -> None:
    from verdict.omniroute_pipeline.report_card import (
        merge_verdicts,  # type: ignore[import-not-found]
    )

    row = merge_verdicts({"passport": "OK", "probe": "FAIL"})
    assert row.verdict == "FAIL"
    assert row.conflict == {"passport": "OK", "probe": "FAIL"}


def test_unverified_free_excluded() -> None:
    from verdict.omniroute_pipeline.report_card import (
        build_report_card,  # type: ignore[import-not-found]
    )

    card = build_report_card(
        scoped_catalog={"agentrouter": ["agentrouter/deepseek-v4-flash"]},
        management=None,
        passports={},
        probes={},
        free_flags=None,
    )
    assert card.rows["agentrouter/deepseek-v4-flash"].status == "UNVERIFIED_FREE"
    assert "agentrouter/deepseek-v4-flash" not in card.eligible


def test_no_data_row_gets_canary() -> None:
    from verdict.omniroute_pipeline.report_card import (
        build_report_card,  # type: ignore[import-not-found]
    )

    card = build_report_card(
        scoped_catalog={"x": ["x/m"]}, management=None, passports={}, probes={}, free_flags=None
    )
    assert card.rows["x/m"].status == "NO_DATA"
    assert card.rows["x/m"].next_action == "canary"
