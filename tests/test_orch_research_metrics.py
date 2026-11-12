"""Content-free research observations and conservative duplication accounting."""


def test_file_normalization_is_lexical_and_content_free():
    from verdict.orchestration.research_metrics import normalize_research_fetch

    first = normalize_research_fetch("read", {"path": "./src//secret.py", "offset": 2, "limit": 8})
    second = normalize_research_fetch(
        "read_file", {"path": "src/secret.py", "offset": 2, "limit": 8}
    )
    assert first == second
    assert first is not None
    assert first["kind"] == "file"
    assert (first["start_line"], first["end_line"]) == (2, 9)
    assert "secret" not in str(first)
    assert first["tokens"] is None


def test_nonfile_identities_preserve_semantics():
    from verdict.orchestration.research_metrics import normalize_research_fetch as n

    assert n("mcp__graph__search_graph", {"query": " Foo ", "project": "a"}) == n(
        "search_graph", {"project": "a", "query": "Foo"}
    )
    assert n("search_graph", {"query": "Foo", "project": "a"}) != n(
        "trace_path", {"query": "Foo", "project": "a"}
    )
    assert n("search_graph", {"query": "Foo", "project": "a"}) != n(
        "search_graph", {"query": "Foo", "project": "b"}
    )
    assert n("web_extract", {"urls": ["HTTPS://Example.COM:443/a?x=1&x=2#frag"]}) == n(
        "fetch_url", {"url": "https://example.com/a?x=1&x=2"}
    )
    assert n("web_fetch", {"url": "https://example.com/a?x=1&x=2"}) != n(
        "web_fetch", {"url": "https://example.com/a?x=2&x=1"}
    )
    assert n("web_fetch", {"url": "file:///secret"}) is None
    for tool in (
        "memory_query",
        "session_search",
        "web_search",
        "memory_read_session_observations",
    ):
        assert n(tool, {"query": " secret ", "project": "a"}) == n(
            tool, {"query": "secret", "project": "a"}
        )
        assert n(tool, {"query": "secret", "project": "a"}) != n(
            tool, {"query": "secret", "project": "b"}
        )
        assert n(tool, {"query": "secret", "filter": "a"}) != n(
            tool, {"query": "secret", "filter": "b"}
        )
    assert n("bash", {"command": "cat secret"}) is None
    assert n("web_extract", {"urls": ["https://a", "https://b"]}) is None


def test_duplicate_accounting_ranges_and_unknown_tokens():
    from verdict.orchestration.research_metrics import measure_research_duplication as measure
    from verdict.orchestration.research_metrics import normalize_research_fetch as n

    def report(base, calls):
        return measure(
            base,
            calls,
            worker_id="w",
            model="m",
            pack_id="p",
            pack_revision="r",
            total_input_tokens=999,
        )

    base = n("read", {"path": "x", "offset": 10, "limit": 10})
    calls = [
        n(
            "read",
            {"path": "./x", "offset": 11, "limit": 2},
            token_count=4,
            token_source="attested",
        ),
        n("read", {"path": "x", "offset": 19, "limit": 3}, token_count=6, token_source="attested"),
    ]
    r = report([base], calls)
    assert r["duplicated_research_tool_calls"] == 1
    assert r["partial_overlap_tool_calls"] == 1
    assert r["duplicated_research_call_share"] == 0.5
    assert r["research_tokens"] == 10
    assert r["duplicated_research_tokens"] == 4
    assert r["duplicated_research_share"] == 0.4
    assert r["token_accounting_status"] == "complete"
    assert r["total_input_tokens"] == 999
    for kwargs in ({"scope": "other"}, {"revision": "new"}):
        assert (
            report([base], [n("read", {"path": "x", "offset": 10, "limit": 10}, **kwargs)])[
                "duplicated_research_tool_calls"
            ]
            == 0
        )
    for path in ("/x", "X"):
        assert report([base], [n("read", {"path": path})])["duplicated_research_tool_calls"] == 0
    unknown = n("read", {"path": "other"})
    r = report([base], [calls[0], unknown])
    assert r["duplicated_research_tokens"] == 4
    assert r["research_tokens"] is None
    assert r["duplicated_research_share"] is None
    assert r["token_accounting_status"] == "partial"
    r = report([base], [n("read", {"path": "x", "offset": 10, "limit": 10})])
    assert r["duplicated_research_tokens"] is None
    assert r["token_accounting_status"] == "unavailable"
    r = report([], [])
    assert r["duplicated_research_share"] == 0
    assert r["duplicated_research_call_share"] == 0
    assert (
        report([n("read", {"path": "x"})], [n("read", {"path": "x", "limit": 1})])[
            "duplicated_research_tool_calls"
        ]
        == 0
    )
    for tool, args in [
        ("search_graph", {"query": "secret"}),
        ("web_fetch", {"url": "https://secret"}),
        ("memory_query", {"query": "secret"}),
    ]:
        fetch = n(tool, args)
        assert report([fetch], [fetch])["duplicated_research_tool_calls"] == 1
        assert "secret" not in str(report([fetch], [fetch]))


def test_invalid_tokens_and_ranges_are_not_evidence():
    from verdict.orchestration.research_metrics import normalize_research_fetch as n

    for tokens in (True, -1, 1.5, "2"):
        assert (
            n("read", {"path": "x"}, token_count=tokens, token_source="harness")["tokens"] is None
        )
    assert n("read", {"path": "x"}, token_count=1, token_source=" ")["tokens"] is None
    for args in ({"path": ""}, {"path": "x", "limit": True}, {"path": "x", "offset": 0}):
        assert n("read", args) is None


def test_structured_transcript_only_and_call_id_deduplication():
    from verdict.orchestration.research_metrics import extract_prime_research_fetches

    call = {
        "type": "toolCall",
        "id": "one",
        "name": "read",
        "arguments": {"path": "secret", "limit": 2},
    }
    conflicting = {**call, "arguments": {"path": "other", "limit": 2}}
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": str(call)}, call]},
        {"role": "assistant", "content": [call, conflicting, {**call, "id": "two"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "urls",
                    "name": "web_extract",
                    "arguments": {"urls": ["https://a", "https://b"]},
                }
            ],
        },
    ]
    observed = extract_prime_research_fetches(messages)
    assert len(observed) == 5
    assert all("secret" not in str(item) and "other" not in str(item) for item in observed)
    assert (
        extract_prime_research_fetches(
            [{"role": "assistant", "content": [{"type": "text", "text": "read secret"}]}]
        )
        == ()
    )
    assert (
        extract_prime_research_fetches(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "id": "bad", "name": "read", "arguments": "bad"}
                    ],
                }
            ]
        )
        == ()
    )
