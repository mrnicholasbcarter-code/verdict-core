"""Content-free research identities and duplication accounting."""

import hashlib
import json
import posixpath
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

_GRAPH = {
    "search_graph",
    "trace_path",
    "get_code_snippet",
    "search_code",
    "find_symbol",
    "find_referencing_symbols",
}
_SEARCH = {"memory_query", "memory_read_session_observations", "session_search", "web_search"}
_URL = {"web_extract", "web_fetch", "fetch_url"}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def normalize_research_fetch(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    scope: str = "",
    revision: str | None = None,
    token_count: int | None = None,
    token_source: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a bounded research call without retaining raw arguments."""
    tool_name = tool_name.split("__")[-1]
    start = end = None
    identity: Any
    if tool_name in {"read", "read_file"}:
        kind = "file"
        path, start, limit = (
            arguments.get("path"),
            arguments.get("offset", 1),
            arguments.get("limit"),
        )
        if not isinstance(path, str) or not path or type(start) is not int or start < 1:
            return None
        if limit is not None and (type(limit) is not int or limit < 1):
            return None
        end = start + limit - 1 if limit is not None else None
        identity = posixpath.normpath(path)
    elif tool_name in _GRAPH | _SEARCH:
        kind = "graph" if tool_name in _GRAPH else "search"
        args = dict(arguments)
        for name in ("query", "search"):
            if isinstance(args.get(name), str):
                args[name] = args[name].strip()
        if kind == "search" and not (args.get("query") or args.get("search")):
            return None
        identity = [kind, tool_name, args]
    elif tool_name in _URL:
        kind = "url"
        url = arguments.get("url")
        if url is None:
            urls = arguments.get("urls")
            if not isinstance(urls, list) or len(urls) != 1:
                return None
            url = urls[0]
        if not isinstance(url, str):
            return None
        try:
            parts = urlsplit(url)
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                return None
            host = parts.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            if parts.port is not None and (parts.scheme.lower(), parts.port) not in {
                ("http", 80),
                ("https", 443),
            }:
                host += f":{parts.port}"
            if "@" in parts.netloc:
                host = parts.netloc.rsplit("@", 1)[0] + "@" + host
            identity = urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, ""))
        except ValueError:
            return None
    else:
        return None
    try:
        resource = _digest([kind, scope, identity, revision])
    except (TypeError, ValueError):
        return None
    known = (
        type(token_count) is int
        and token_count >= 0
        and isinstance(token_source, str)
        and bool(token_source.strip())
    )
    return {
        "kind": kind,
        "key": _digest([resource, start, end]),
        "resource_key": resource,
        "start_line": start,
        "end_line": end,
        "revision": revision,
        "tokens": token_count if known else None,
        "token_source": token_source if known else None,
    }


def extract_prime_research_fetches(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Extract structured research calls from an authoritative transcript."""
    seen: dict[str, str] = {}
    result: list[dict[str, Any]] = []
    for message in messages:
        blocks = message.get("content")
        if not isinstance(blocks, list):
            blocks = message.get("tool_calls")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "toolCall":
                name, args, call_id = block.get("name"), block.get("arguments"), block.get("id")
            elif block.get("type") == "function" and isinstance(block.get("function"), Mapping):
                fn = block["function"]
                name, args, call_id = fn.get("name"), fn.get("arguments"), block.get("id")
            else:
                continue
            if not isinstance(name, str) or not isinstance(args, Mapping):
                continue
            arg_sets = [args]
            if name.split("__")[-1] == "web_extract" and isinstance(args.get("urls"), list):
                arg_sets = [{"url": url} for url in args["urls"]]
            normalized_calls = tuple(
                filter(None, (normalize_research_fetch(name, item) for item in arg_sets))
            )
            marker = json.dumps(normalized_calls, sort_keys=True)
            if isinstance(call_id, str) and call_id and call_id in seen and seen[call_id] == marker:
                continue
            if isinstance(call_id, str) and call_id:
                seen[call_id] = marker
            result.extend(normalized_calls)
    return tuple(result)


def measure_research_duplication(
    baseline: Sequence[Mapping[str, Any]],
    fetches: Sequence[Mapping[str, Any]],
    *,
    worker_id: str,
    model: str,
    pack_id: str,
    pack_revision: str,
    total_input_tokens: int | None = None,
) -> dict[str, Any]:
    """Measure strict identity/subset duplication; aggregate usage is not allocated."""
    by_resource: dict[str, list[Mapping[str, Any]]] = {}
    for item in baseline:
        by_resource.setdefault(str(item["resource_key"]), []).append(item)
    duplicates: list[Mapping[str, Any]] = []
    partial = 0
    for fetch in fetches:
        is_dup = is_partial = False
        for base in by_resource.get(str(fetch["resource_key"]), []):
            if fetch.get("key") == base.get("key"):
                is_dup = True
                break
            bs, be, fs, fe = (
                base.get("start_line"),
                base.get("end_line"),
                fetch.get("start_line"),
                fetch.get("end_line"),
            )
            if all(type(x) is int for x in (bs, be, fs, fe)):
                bs_i, be_i, fs_i, fe_i = (cast(int, value) for value in (bs, be, fs, fe))
                if bs_i <= fs_i and fe_i <= be_i:
                    is_dup = True
                    break
                if max(bs_i, fs_i) <= min(be_i, fe_i):
                    is_partial = True
        if is_dup:
            duplicates.append(fetch)
        elif is_partial:
            partial += 1
    total = len(fetches)
    all_known = total == 0 or all(type(f.get("tokens")) is int for f in fetches)
    dup_known = not duplicates or all(type(f.get("tokens")) is int for f in duplicates)
    research_tokens = sum(int(f["tokens"]) for f in fetches) if all_known else None
    duplicate_tokens = sum(int(f["tokens"]) for f in duplicates) if dup_known else None
    if all_known and research_tokens is not None:
        token_share = (
            duplicate_tokens / research_tokens
            if duplicate_tokens is not None and research_tokens
            else 0.0
        )
        status = "complete"
    elif dup_known and duplicate_tokens is not None:
        token_share, status = None, "partial"
    else:
        token_share, status = None, "unavailable"
    return {
        "worker_id": worker_id,
        "model": model,
        "pack_id": pack_id,
        "pack_revision": pack_revision,
        "baseline_fetches": len(baseline),
        "research_tool_calls": total,
        "duplicated_research_tool_calls": len(duplicates),
        "partial_overlap_tool_calls": partial,
        "duplicated_research_call_share": len(duplicates) / total if total else 0.0,
        "research_tokens": research_tokens,
        "duplicated_research_tokens": duplicate_tokens,
        "duplicated_research_share": token_share,
        "token_accounting_status": status,
        "total_input_tokens": total_input_tokens,
    }
