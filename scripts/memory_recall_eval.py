#!/usr/bin/env python3
"""Recall eval for ai-memory: does a project-scoped query still find the expected pages?

Usage:
    scripts/memory_recall_eval.py --golden FILE --workspace W --project P --out RESULT.json
                                  [--endpoint URL] [--threshold 0.90] [--global]

Each golden row is ``{"id", "category", "question", "expected_sources", "must_contain"}``
where an expected source is ``"<source_path> ## <section heading>"``. The question
becomes an FTS query (stopwords dropped, compound tokens such as ``cx/gpt-6-luna``
kept whole, a quoted OR of at most 24 terms) sent to ``memory_query`` with limit 5,
restricted to ``--workspace``/``--project``. A hit matches when its page cites an
expected source (``## Source citation`` JSON: ``source_path`` and the last segment
of ``section``) and its body contains every ``must_contain`` string
(case-insensitive; a trailing ``s`` on the last word is optional).

A hit is foreign when it comes from another project than the requested one, or its
page cannot be read in the requested scope. Scoped runs pass only with hit@5 >=
``--threshold`` and no foreign hit. ``--global`` searches every project as a
diagnostic; foreign hits are then reported, not failed.

The bearer token is read from ``AI_MEMORY_AUTH_TOKEN``. Read-only: the script only
calls ``memory_query`` and ``memory_read_page``.

Exit codes: 0 pass, 1 fail, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

import httpx

DEFAULT_ENDPOINT = "http://127.0.0.1:49374"
LIMIT = 5
MAX_TERMS = 24
_STOPWORD_TEXT = """
    a about above after again against all am an and any are as at
    be because been before being below between both but by can could did do
    does doing done down during each few for from further had has have having
    he her here hers him his how i if in into is it its
    itself just me more most my myself no nor not now of off on
    once only or other our ours out over own same she should so some
    such than that the their theirs them then there these they this those through
    to too under until up very was we were what when where which while
    who whom why will with would you your yours ok okay fine may might
    must shall get got use used using really actually already also ever every one
    anything something
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())
_EDGE = "\"'`?!.,;:()[]{}<>"
_CITATION = re.compile(r"## Source citation\s*\n(\{.*?\})\s*\n", re.S)


def query_terms(question: str, max_terms: int = MAX_TERMS) -> list[str]:
    """Content terms of a question, in order, deduplicated case-insensitively."""
    terms: list[str] = []
    seen: set[str] = set()
    for raw in question.split():
        token = raw.strip(_EDGE)
        if token.lower().endswith(("'s", "\u2019s")):
            token = token[:-2]
        token = token.strip(_EDGE)
        if len(token) < 2 or token.lower() in STOPWORDS:
            continue
        if token.lower() not in seen:
            seen.add(token.lower())
            terms.append(token)
        if len(terms) == max_terms:
            break
    return terms


def build_query(question: str, max_terms: int = MAX_TERMS) -> str:
    """FTS5 expression: a quoted OR of the content terms."""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in query_terms(question, max_terms))


def parse_expected(source: str) -> tuple[str, str]:
    path, _, section = source.partition(" ## ")
    return path.strip(), section.strip()


def citation(body: str) -> dict[str, Any]:
    m = _CITATION.search(body)
    if not m:
        return {}
    try:
        data: dict[str, Any] = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return data


def contains(body: str, needle: str) -> bool:
    text, n = body.lower(), needle.lower()
    return n in text or (n.endswith("s") and n[:-1] in text)


def expected_source(body: str, expected: Iterable[str]) -> str | None:
    """The expected source this page cites, if any."""
    cite = citation(body)
    leaf = str(cite.get("section", "")).split(" > ")[-1].strip()
    for source in expected:
        path, section = parse_expected(source)
        if cite.get("source_path") == path and leaf == section:
            return source
    return None


class Memory(Protocol):
    def query(self, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def read_page(self, workspace: str, project: str, path: str) -> dict[str, Any] | None: ...


class McpMemory:
    """Read-only MCP-over-HTTP client for ai-memory."""

    def __init__(self, endpoint: str, token: str) -> None:
        self._url = endpoint.rstrip("/") + "/mcp"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(timeout=60)

    def _call(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        resp = self._client.post(self._url, headers=self._headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return False, str(data["error"].get("message", ""))
        result = data["result"]
        return not result.get("isError"), result["content"][0]["text"]

    def query(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ok, text = self._call("memory_query", arguments)
        if not ok:
            raise RuntimeError(f"memory_query failed: {text[:300]}")
        parsed: dict[str, Any] = json.loads(text)
        return parsed

    def read_page(self, workspace: str, project: str, path: str) -> dict[str, Any] | None:
        ok, text = self._call(
            "memory_read_page", {"workspace": workspace, "project": project, "path": path}
        )
        if not ok:
            if "not found" in text:
                return None
            raise RuntimeError(f"memory_read_page failed: {text[:300]}")
        page: dict[str, Any] = json.loads(text)
        return page


def evaluate_row(
    memory: Memory, row: dict[str, Any], workspace: str, project: str, global_: bool = False
) -> dict[str, Any]:
    query = build_query(row["question"])
    args: dict[str, Any] = {"query": query, "limit": LIMIT}
    if global_:
        args["global"] = True
    else:
        args.update(workspace=workspace, project=project)
    response = memory.query(args)
    raw_hits = response.get("global_hits", []) if global_ else response.get("hits", [])
    hits = []
    for rank, hit in enumerate(raw_hits[:LIMIT], 1):
        ws = hit.get("workspace_name", workspace)
        proj = hit.get("project_name", project)
        path = str(hit.get("path", ""))
        page = memory.read_page(ws, proj, path) if path else None
        body = str((page or {}).get("body", ""))
        source = expected_source(body, row["expected_sources"])
        missing = [m for m in row["must_contain"] if not contains(body, m)]
        cite = citation(body)
        hits.append(
            {
                "rank": rank,
                "path": path,
                "workspace": ws,
                "project": proj,
                "source_path": cite.get("source_path"),
                "section": cite.get("section"),
                "expected_source": source,
                "missing": missing,
                "matched": bool(source) and not missing,
                "foreign": (ws, proj) != (workspace, project) or page is None,
            }
        )
    first = next((h["rank"] for h in hits if h["matched"]), None)
    return {
        "id": row["id"],
        "category": row.get("category"),
        "question": row["question"],
        "query": query,
        "first_match_rank": first,
        "hit_at_1": first == 1,
        "hit_at_5": first is not None,
        "foreign_hits": sum(h["foreign"] for h in hits),
        "hits": hits,
    }


def evaluate(
    memory: Memory,
    rows: list[dict[str, Any]],
    workspace: str,
    project: str,
    threshold: float,
    global_: bool = False,
) -> dict[str, Any]:
    results = [evaluate_row(memory, row, workspace, project, global_) for row in rows]
    n = len(results)
    hit5 = sum(r["hit_at_5"] for r in results) / n if n else 0.0
    foreign = sum(r["foreign_hits"] for r in results)
    return {
        "workspace": workspace,
        "project": project,
        "global": global_,
        "limit": LIMIT,
        "threshold": threshold,
        "questions": n,
        "hit_at_1": sum(r["hit_at_1"] for r in results) / n if n else 0.0,
        "hit_at_5": hit5,
        "foreign_hits": foreign,
        "questions_with_foreign_hits": sum(1 for r in results if r["foreign_hits"]),
        "pass": n > 0 and hit5 >= threshold and (global_ or foreign == 0),
        "misses": [r["id"] for r in results if not r["hit_at_5"]],
        "results": results,
    }


def load_golden(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None, memory: Memory | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.90)
    parser.add_argument(
        "--global", dest="global_", action="store_true", help="diagnostic: search every project"
    )
    args = parser.parse_args(argv)

    if not args.golden.is_file():
        print(f"golden set not found: {args.golden}", file=sys.stderr)
        return 2
    if memory is None:
        token = os.environ.get("AI_MEMORY_AUTH_TOKEN", "")
        if not token:
            print("AI_MEMORY_AUTH_TOKEN is not set", file=sys.stderr)
            return 2
        memory = McpMemory(args.endpoint, token)
    report = evaluate(
        memory, load_golden(args.golden), args.workspace, args.project, args.threshold, args.global_
    )
    report = {"golden": str(args.golden), "endpoint": args.endpoint, **report}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1) + "\n", "utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("questions", "hit_at_1", "hit_at_5", "foreign_hits", "misses", "pass")
            }
        )
    )
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
