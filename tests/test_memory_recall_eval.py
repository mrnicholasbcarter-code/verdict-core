"""Unit tests for scripts/memory_recall_eval.py (in-process fake, no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.memory_recall_eval import (
    MAX_TERMS,
    build_query,
    contains,
    evaluate,
    expected_source,
    main,
    query_terms,
)

SRC = "/ops/rules.md"


def page_body(section: str, text: str, source: str = SRC) -> str:
    cite = {"source_path": source, "section": f"Rules > {section}", "part": 1}
    return f"# Operator rule: rules part 1\n\n## Source citation\n{json.dumps(cite)}\n\n## Text\n\n{text}"


class FakeMemory:
    def __init__(
        self, ranked: list[dict[str, Any]], pages: dict[tuple[str, str, str], str]
    ) -> None:
        self.ranked = ranked
        self.pages = pages
        self.queries: list[dict[str, Any]] = []

    def query(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(arguments)
        key = "global_hits" if arguments.get("global") else "hits"
        return {key: self.ranked}

    def read_page(self, workspace: str, project: str, path: str) -> dict[str, Any] | None:
        body = self.pages.get((workspace, project, path))
        return None if body is None else {"path": path, "body": body}


ROW = {
    "id": "q-01",
    "category": "operator-rule",
    "question": "When can I call a story finished?",
    "expected_sources": [f"{SRC} ## Definition of done"],
    "must_contain": ["WORKS FOR REAL", "confirmed by the parent"],
}
GOOD = page_body("Definition of done", "1. WORKS FOR REAL ... 7. CONFIRMED BY THE PARENT")


def test_query_drops_stopwords_and_quotes_terms() -> None:
    assert build_query("When can I call a story finished?") == '"call" OR "story" OR "finished"'


def test_query_keeps_compound_tokens_whole() -> None:
    terms = query_terms("Can cx/gpt-6-luna review ADR-036 at 127.0.0.1:20128 on ~/.verdict?")
    assert terms == ["cx/gpt-6-luna", "review", "ADR-036", "127.0.0.1:20128", "~/.verdict"]


def test_query_dedupes_case_insensitively_and_caps_terms() -> None:
    assert query_terms("Gateway gateway GATEWAY") == ["Gateway"]
    words = " ".join(f"word{i}" for i in range(40))
    assert len(query_terms(words)) == MAX_TERMS
    assert build_query(words).count(" OR ") == MAX_TERMS - 1


def test_query_escapes_embedded_quotes() -> None:
    assert build_query('the "-fast" ids') == '"-fast" OR "ids"'
    assert build_query('say a"b') == '"say" OR "a""b"'


def test_contains_is_case_insensitive_and_tolerates_plural() -> None:
    assert contains("Never use -fast IDS", "-fast ids")
    assert contains("one review per story", "reviews")
    assert not contains("one review", "reviewer")


def test_expected_source_matches_path_and_leaf_section() -> None:
    assert expected_source(GOOD, ROW["expected_sources"]) == ROW["expected_sources"][0]
    assert expected_source(page_body("Other", "x"), ROW["expected_sources"]) is None
    assert (
        expected_source(
            page_body("Definition of done", "x", "/elsewhere.md"), ROW["expected_sources"]
        )
        is None
    )
    assert expected_source("no citation here", ROW["expected_sources"]) is None


def test_scoring_hit_at_1_and_5_and_must_contain() -> None:
    ranked = [{"path": "a.md"}, {"path": "b.md"}, {"path": "c.md"}]
    pages = {
        ("default", "operator", "a.md"): page_body("Definition of done", "WORKS FOR REAL only"),
        ("default", "operator", "b.md"): page_body(
            "Unrelated", "WORKS FOR REAL, CONFIRMED BY THE PARENT"
        ),
        ("default", "operator", "c.md"): GOOD,
    }
    memory = FakeMemory(ranked, pages)
    report = evaluate(memory, [ROW], "default", "operator", 0.9)
    result = report["results"][0]
    assert [h["matched"] for h in result["hits"]] == [False, False, True]
    assert result["hits"][0]["missing"] == ["confirmed by the parent"]
    assert result["first_match_rank"] == 3
    assert (report["hit_at_1"], report["hit_at_5"], report["pass"]) == (0.0, 1.0, True)
    assert memory.queries == [
        {
            "query": '"call" OR "story" OR "finished"',
            "limit": 5,
            "workspace": "default",
            "project": "operator",
        }
    ]


def test_only_top_five_hits_count() -> None:
    ranked = [{"path": f"p{i}.md"} for i in range(6)]
    pages = {("default", "operator", f"p{i}.md"): page_body("Other", "x") for i in range(5)}
    pages[("default", "operator", "p5.md")] = GOOD
    report = evaluate(FakeMemory(ranked, pages), [ROW], "default", "operator", 0.9)
    assert len(report["results"][0]["hits"]) == 5
    assert report["hit_at_5"] == 0.0
    assert report["misses"] == ["q-01"]


def test_foreign_project_hits_are_counted_and_fail_a_scoped_run() -> None:
    ranked = [
        {"path": "c.md", "workspace_name": "default", "project_name": "ref-omniroute"},
        {"path": "c.md"},
    ]
    pages = {("default", "ref-omniroute", "c.md"): GOOD, ("default", "operator", "c.md"): GOOD}
    report = evaluate(FakeMemory(ranked, pages), [ROW], "default", "operator", 0.9)
    assert report["foreign_hits"] == 1
    assert report["questions_with_foreign_hits"] == 1
    assert report["hit_at_5"] == 1.0
    assert report["pass"] is False


def test_unreadable_page_in_requested_scope_counts_as_foreign() -> None:
    report = evaluate(FakeMemory([{"path": "gone.md"}], {}), [ROW], "default", "operator", 0.9)
    assert report["results"][0]["hits"][0]["foreign"] is True
    assert report["foreign_hits"] == 1


def test_global_diagnostic_reports_foreign_hits_without_failing() -> None:
    ranked = [{"path": "c.md", "workspace_name": "default", "project_name": "ref-omniroute"}]
    memory = FakeMemory(ranked, {("default", "ref-omniroute", "c.md"): GOOD})
    report = evaluate(memory, [ROW], "default", "operator", 0.9, global_=True)
    assert memory.queries[0] == {
        "query": '"call" OR "story" OR "finished"',
        "limit": 5,
        "global": True,
    }
    assert report["foreign_hits"] == 1
    assert report["pass"] is True


def _golden(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "golden.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))


def test_main_exit_codes_and_result_file(tmp_path: Path) -> None:
    second = {**ROW, "id": "q-02"}
    golden = _golden(tmp_path, [ROW, second])
    out = tmp_path / "out" / "result.json"
    hit = FakeMemory([{"path": "c.md"}], {("default", "operator", "c.md"): GOOD})
    argv = [
        "--golden",
        str(golden),
        "--workspace",
        "default",
        "--project",
        "operator",
        "--out",
        str(out),
    ]
    assert main(argv, memory=hit) == 0
    saved = json.loads(out.read_text("utf-8"))
    assert saved["questions"] == 2 and saved["pass"] is True
    first = saved["results"][0]["hits"][0]
    assert (first["rank"], first["path"], first["matched"], first["foreign"]) == (
        1,
        "c.md",
        True,
        False,
    )

    miss = FakeMemory(
        [{"path": "x.md"}], {("default", "operator", "x.md"): page_body("Other", "x")}
    )
    assert main(argv, memory=miss) == 1
    assert main([*argv, "--threshold", "0"], memory=miss) == 0
    assert json.loads(out.read_text("utf-8"))["hit_at_5"] == 0.0


def test_main_usage_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "r.json"
    base = ["--workspace", "default", "--project", "operator", "--out", str(out)]
    assert main(["--golden", str(tmp_path / "missing.jsonl"), *base]) == 2
    monkeypatch.delenv("AI_MEMORY_AUTH_TOKEN", raising=False)
    assert main(["--golden", str(_golden(tmp_path, [ROW])), *base]) == 2
    assert not out.exists()
