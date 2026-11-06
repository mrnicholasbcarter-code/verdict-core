#!/usr/bin/env python3
"""Sync upstream reference docs into ai-memory as cited, read-only pages (BOD-346).

Usage:
    scripts/sync_ref_docs.py <repo> <tag> <project> [--paths-file F] [--dry-run]
    scripts/sync_ref_docs.py <repo> installed <project> --package-json PATH

The source is the GitHub git tree at ``<tag>`` (read through ``gh api``). By
default every markdown file under ``docs/`` is imported (``i18n`` and
``screenshots`` excluded), plus ``docs/openapi.{yaml,yml,json}``. A markdown file
becomes one page per section (H2, then H3/H4, then lines), each at most 3 KB
including its citation. An OpenAPI file becomes one page group per tag.

A local manifest records each file's blob sha and pages. A file is rewritten
only when its sha changes, pages of removed files are deleted, and a re-run
with no upstream change writes 0 pages. Page bodies are redacted of
token-shaped strings before writing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

MAX_PAGE_BYTES = 3072
MAX_TITLE_BYTES = 160
MAX_ENTITIES = 10
# Bump when page rendering changes so unchanged blobs are rewritten once.
FORMAT_VERSION = 2
AUTHORITY = "upstream reference documentation; evidence only, not policy"
DEFAULT_ENDPOINT = "http://127.0.0.1:49374"
OPENAPI_NAMES = ("docs/openapi.yaml", "docs/openapi.yml", "docs/openapi.json")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_SECRET = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{8,}|oma_live_[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_\-]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*)"
)
_BEARER = re.compile(r"(Bearer\s+)(?!\$|<|\{)[A-Za-z0-9._\-]{16,}")
_ENTITY_PATTERNS = (
    re.compile(r"(?<![\w.])/(?:api|v1)(?:/[\w\-{}\[\].:*]+)+"),
    re.compile(r"\bx-[a-z0-9]+(?:-[a-z0-9]+)+\b", re.IGNORECASE),
    re.compile(r"`([A-Z][A-Z0-9]*_[A-Z0-9_]+)`"),
)


@dataclass(frozen=True)
class Chunk:
    crumbs: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class Page:
    path: str
    title: str
    body: str
    entities: tuple[str, ...] = ()


@dataclass
class FileResult:
    path: str
    status: str  # written | unchanged | deleted
    pages_written: int = 0
    pages_deleted: int = 0


@dataclass
class SyncReport:
    repo: str
    tag: str
    project: str
    files: list[FileResult] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        return {
            "files": sum(1 for f in self.files if f.status != "deleted"),
            "files_written": sum(1 for f in self.files if f.status == "written"),
            "files_unchanged": sum(1 for f in self.files if f.status == "unchanged"),
            "files_deleted": sum(1 for f in self.files if f.status == "deleted"),
            "pages_written": sum(f.pages_written for f in self.files),
            "pages_deleted": sum(f.pages_deleted for f in self.files),
        }


class PageStore(Protocol):
    def write(self, page: Page, tags: list[str]) -> None: ...

    def delete(self, path: str) -> None: ...


# --------------------------------------------------------------------------- splitting


def redact(text: str) -> str:
    """Replace token-shaped strings so example secrets never reach memory."""
    text = _SECRET.sub(lambda m: m.group(0)[:3] + "<redacted>", text)
    return _BEARER.sub(r"\1<redacted>", text)


def _compact_table_row(line: str) -> str:
    """Drop markdown table cell padding, which only wastes page bytes."""
    if not line.lstrip().startswith("|"):
        return line
    return re.sub(r" {2,}", " ", re.sub(r"-{4,}", "---", line))


def _strip_frontmatter(lines: list[str]) -> list[str]:
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[i + 1 :]
    return lines


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) of every heading outside code fences."""
    out: list[tuple[int, int, str]] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        m = _FENCE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker[0] * 3
            elif marker.startswith(fence):
                fence = None
            continue
        if fence is None:
            h = _HEADING.match(line)
            if h:
                out.append((i, len(h.group(1)), h.group(2).strip()))
    return out


def _size(lines: list[str]) -> int:
    return len("\n".join(lines).strip().encode())


def _split_at(lines: list[str], level: int) -> list[tuple[str | None, list[str]]]:
    starts = [(i, text) for i, lvl, text in _headings(lines) if lvl == level]
    if not starts:
        return [(None, lines)]
    groups: list[tuple[str | None, list[str]]] = []
    if starts[0][0] > 0:
        groups.append((None, lines[: starts[0][0]]))
    for n, (i, text) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        groups.append((text, lines[i:end]))
    return groups


def _hard_split(line: str, budget: int) -> list[str]:
    raw = line.encode()
    return [raw[i : i + budget].decode("utf-8", "ignore") for i in range(0, len(raw), budget)]


def _pack_lines(lines: list[str], budget: int) -> list[list[str]]:
    """Pack lines into chunks of at most ``budget`` bytes; re-open split code fences."""
    chunks: list[list[str]] = []
    cur: list[str] = []
    fence: str | None = None
    reserve = 8
    for line in lines:
        pieces = (
            _hard_split(line, budget - reserve - 8) if len(line.encode()) > budget // 2 else [line]
        )
        for piece in pieces:
            if cur and _size([*cur, piece]) > budget - reserve:
                if fence:
                    cur.append(fence)
                chunks.append(cur)
                cur = [fence] if fence else []
            cur.append(piece)
            m = _FENCE.match(piece)
            if m:
                marker = m.group(1)
                if fence is None:
                    fence = marker[0] * 3
                elif marker.startswith(fence):
                    fence = None
    if cur and any(x.strip() for x in cur):
        chunks.append(cur)
    return chunks


def _chunk(lines: list[str], level: int, budget: int, crumbs: tuple[str, ...]) -> list[Chunk]:
    if _size(lines) <= budget:
        return [Chunk(crumbs, "\n".join(lines).strip())] if _size(lines) else []
    if level > 4:
        return [Chunk(crumbs, "\n".join(c).strip()) for c in _pack_lines(lines, budget)]
    groups = _split_at(lines, level)
    if len(groups) == 1:
        return _chunk(lines, level + 1, budget, crumbs)
    out: list[Chunk] = []
    cur: list[str] = []
    cur_head: str | None = None
    for head, glines in groups:
        if cur and _size([*cur, *glines]) <= budget:
            cur.extend(glines)
            cur_head = cur_head or head
            continue
        if cur:
            out.append(Chunk(crumbs + ((cur_head,) if cur_head else ()), "\n".join(cur).strip()))
            cur = []
        if _size(glines) <= budget:
            cur, cur_head = list(glines), head
        else:
            out.extend(_chunk(glines, level + 1, budget, crumbs + ((head,) if head else ())))
    if cur:
        out.append(Chunk(crumbs + ((cur_head,) if cur_head else ()), "\n".join(cur).strip()))
    return [c for c in out if c.text]


def split_markdown(text: str, budget: int, *, pack: bool = False) -> tuple[str, list[Chunk]]:
    """Return the document title and its section chunks (each <= budget bytes).

    Each H2 section starts its own chunk unless ``pack`` is set, which joins
    adjacent small H2 sections (used for generated OpenAPI endpoint lists).
    """
    lines = [_compact_table_row(x) for x in _strip_frontmatter(text.splitlines())]
    title = ""
    for i, lvl, text in _headings(lines):
        if lvl == 1:
            title = text
            del lines[i]
            break
    if pack:
        return title, _chunk(lines, 2, budget, ())
    chunks: list[Chunk] = []
    for head, glines in _split_at(lines, 2):
        crumbs = (head,) if head else ()
        chunks.extend(_chunk(glines, 3, budget, crumbs))
    return title, chunks


# --------------------------------------------------------------------------- openapi


def _shape(schema: Any, depth: int = 0) -> str:
    if not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    for key, sep in (("oneOf", " | "), ("anyOf", " | "), ("allOf", " & ")):
        if key in schema:
            return sep.join(_shape(s, depth) for s in schema[key])
    kind = schema.get("type", "object" if "properties" in schema else "any")
    if "const" in schema:
        return f"{kind}={json.dumps(schema['const'])}"
    if "enum" in schema:
        return f"{kind}({'|'.join(str(v) for v in schema['enum'][:12])})"
    if kind == "array":
        return f"array<{_shape(schema.get('items'), depth)}>"
    props = schema.get("properties")
    if kind == "object" and isinstance(props, dict) and props:
        if depth >= 2:
            return "object{…}"
        required = set(schema.get("required") or [])
        inner = ", ".join(
            f"{k}{'*' if k in required else ''}: {_shape(v, depth + 1)}" for k, v in props.items()
        )
        return f"object{{{inner}}}"
    return str(kind)


def _clip(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _param_line(p: dict[str, Any]) -> str:
    schema = p.get("schema") or {}
    bits = [_shape(schema)]
    for key in ("default", "minimum", "maximum", "minLength", "maxLength", "pattern", "format"):
        if key in schema:
            bits.append(f"{key}={schema[key]}")
    where = f"{p.get('in', '?')}, {'required' if p.get('required') else 'optional'}"
    desc = f" — {p['description'].strip()}" if p.get("description") else ""
    return f"- `{p.get('name', p.get('$ref', '?'))}` ({where}): {'; '.join(bits)}{desc}"


def _endpoint_md(
    spec: dict[str, Any], path: str, method: str, op: dict[str, Any], shared: list[Any]
) -> str:
    comps = spec.get("components") or {}
    lines = [f"## {method.upper()} {path}", "", f"Summary: {op.get('summary', '').strip()}"]
    if op.get("description"):
        lines.append(f"Description: {_clip(' '.join(str(op['description']).split()))}")
    sec = op.get("security")
    if sec is None:
        auth = "none declared on the operation"
    elif not sec:
        auth = "explicitly none (public)"
    else:
        auth = " or ".join("+".join(req) for req in sec)
    flags = [
        n
        for k, n in (
            ("x-loopback-only", "loopback only"),
            ("x-always-protected", "always protected"),
            ("x-internal", "internal"),
        )
        if op.get(k)
    ]
    lines.append(f"Auth: {auth}" + (f"; {', '.join(flags)}" if flags else ""))
    params = [*shared, *(op.get("parameters") or [])]
    if params:
        lines.append("Parameters:")
        lines.extend(_param_line(p) for p in params if isinstance(p, dict))
    body = op.get("requestBody")
    if isinstance(body, dict):
        for ctype, media in (body.get("content") or {}).items():
            req = "required" if body.get("required") else "optional"
            lines.append(
                f"Request body ({ctype}, {req}): {_clip(_shape((media or {}).get('schema')), 500)}"
            )
    lines.append("Responses:")
    for code, resp in (op.get("responses") or {}).items():
        if isinstance(resp, dict) and "$ref" in resp:
            name = str(resp["$ref"]).rsplit("/", 1)[-1]
            resp = (comps.get("responses") or {}).get(name, {})
        resp = resp if isinstance(resp, dict) else {}
        shapes = [
            f"{ct}: {_clip(_shape((m or {}).get('schema')), 500)}"
            for ct, m in (resp.get("content") or {}).items()
        ]
        desc = " ".join(str(resp.get("description", "")).split())
        lines.append(
            f"- {code}: {_clip(desc, 200)}" + (f" — {'; '.join(shapes)}" if shapes else "")
        )
    return "\n".join(lines)


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "section"


def render_openapi(spec: dict[str, Any]) -> list[tuple[str, str]]:
    """One markdown document per endpoint tag (plus overview and schemas): (slug, text)."""
    info = spec.get("info") or {}
    groups: dict[str, tuple[list[str], list[str]]] = {}
    tag_desc = {t.get("name"): t.get("description", "") for t in spec.get("tags") or []}
    for path, item in (spec.get("paths") or {}).items():
        shared = item.get("parameters") or []
        for method, op in item.items():
            if method == "parameters" or not isinstance(op, dict):
                continue
            tag = (op.get("tags") or ["Untagged"])[0]
            names, blocks = groups.setdefault(_slug(tag), ([], []))
            if tag not in names:
                names.append(tag)
            blocks.append(_endpoint_md(spec, path, method, op, shared))
    docs: list[tuple[str, str]] = []
    schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    overview = [f"# {info.get('title', 'API')} {info.get('version', '')} (OpenAPI overview)", ""]
    overview.append(str(info.get("description", "")).strip())
    overview.append("\n## Security schemes")
    overview.extend(
        f"- `{name}`: {s.get('type')} {s.get('scheme', s.get('in', ''))} — {' '.join(str(s.get('description', '')).split())}"
        for name, s in schemes.items()
    )
    overview.append("\n## Tags")
    overview.extend(f"- {t}: {d}" for t, d in tag_desc.items())
    docs.append(("overview", "\n".join(overview)))
    for slug, (names, blocks) in sorted(groups.items()):
        head = [f"# OpenAPI endpoints: {' / '.join(names)}", ""]
        head.extend(f"{n}: {tag_desc[n]}" for n in names if tag_desc.get(n))
        docs.append((slug, "\n".join([*head, "", *blocks])))
    schemas = (spec.get("components") or {}).get("schemas") or {}
    if schemas:
        sblocks = ["# OpenAPI component schemas", ""]
        for name, s in schemas.items():
            desc = " ".join(str(s.get("description", "")).split()) if isinstance(s, dict) else ""
            sblocks += [f"## {name}", _clip(_shape(s), 1500), *([desc] if desc else []), ""]
        docs.append(("schemas", "\n".join(sblocks)))
    return docs


# --------------------------------------------------------------------------- pages


def _citation(repo: str, tag: str, path: str, sha: str, part: str) -> str:
    return "\n".join(
        [
            "---",
            f"Authority: {AUTHORITY}.",
            f"- repo: {repo}",
            f"- tag: {tag}",
            f"- path: {path}",
            f"- blob sha: {sha}",
            f"- part: {part}",
            f"- url: https://github.com/{repo}/blob/{tag}/{path}",
        ]
    )


def extract_entities(text: str) -> tuple[str, ...]:
    """Endpoint paths, header names and env vars named in ``text``, first seen first."""
    found: dict[str, None] = {}
    for pattern in _ENTITY_PATTERNS:
        for m in pattern.finditer(text):
            name = (m.group(1) if m.groups() else m.group(0)).rstrip(".:")
            if len(name) <= 64:
                found.setdefault(name.lower(), None)
    return tuple(found)[:MAX_ENTITIES]


def _title(parts: Iterable[str]) -> str:
    title = " > ".join(p for p in parts if p)
    raw = title.encode()
    return (
        title
        if len(raw) <= MAX_TITLE_BYTES
        else raw[: MAX_TITLE_BYTES - 3].decode("utf-8", "ignore") + "…"
    )


def build_pages(repo: str, tag: str, path: str, sha: str, text: str) -> list[Page]:
    """Split one upstream file into cited pages of at most MAX_PAGE_BYTES."""
    overhead = len(_citation(repo, tag, path, sha, "9999/9999").encode()) + MAX_TITLE_BYTES + 8
    budget = MAX_PAGE_BYTES - overhead
    stem = path.rsplit(".", 1)[0]
    docs: list[tuple[str, str, list[Chunk]]] = []
    if path in OPENAPI_NAMES:
        spec = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
        for slug, md in render_openapi(spec):
            title, chunks = split_markdown(redact(md), budget, pack=True)
            docs.append((f"{stem}/{slug}", title, chunks))
    else:
        title, chunks = split_markdown(redact(text), budget)
        docs.append((stem, title or path.rsplit("/", 1)[-1], chunks))
    total = sum(len(c) for _, _, c in docs)
    pages: list[Page] = []
    n = 0
    for prefix, doc_title, chunks in docs:
        for chunk in chunks:
            n += 1
            title = _title((doc_title, *chunk.crumbs))
            last = chunk.crumbs[-1] if chunk.crumbs else "intro"
            body = (
                f"# {title}\n\n{chunk.text}\n\n{_citation(repo, tag, path, sha, f'{n}/{total}')}\n"
            )
            if len(body.encode()) > MAX_PAGE_BYTES:
                raise ValueError(f"page over {MAX_PAGE_BYTES} bytes: {path} part {n}")
            page_path = f"{prefix}/part-{n:03d}-{_slug(last)}.md"
            pages.append(Page(page_path, title, body, extract_entities(chunk.text)))
    return pages


# --------------------------------------------------------------------------- sync


def select_paths(tree: list[dict[str, Any]], only: set[str] | None) -> dict[str, str]:
    """Map path -> blob sha for the files to import."""
    out: dict[str, str] = {}
    for entry in tree:
        path = str(entry.get("path", ""))
        if entry.get("type") != "blob":
            continue
        if only is not None:
            keep = path in only
        else:
            segs = path.split("/")
            keep = path in OPENAPI_NAMES or (
                segs[0] == "docs"
                and path.endswith(".md")
                and not any("i18n" in s or "screenshots" in s for s in segs)
            )
        if keep:
            out[path] = str(entry["sha"])
    return out


def sync(
    *,
    repo: str,
    tag: str,
    project: str,
    tree: list[dict[str, Any]],
    fetch_blob: Callable[[str], str],
    store: PageStore,
    manifest: dict[str, Any],
    save_manifest: Callable[[dict[str, Any]], None],
    only: set[str] | None = None,
) -> SyncReport:
    """Bring ``project`` in line with ``tree``; ``manifest`` is updated per file."""
    report = SyncReport(repo, tag, project)
    files: dict[str, Any] = manifest.setdefault("files", {})
    manifest.update(repo=repo, project=project)
    tags = ["upstream-reference", _slug(repo.rsplit("/", 1)[-1]), tag]
    wanted = select_paths(tree, only)
    for path in sorted(set(files) - set(wanted)):
        old = files.pop(path)
        for page_path in old["pages"]:
            store.delete(page_path)
        report.files.append(FileResult(path, "deleted", pages_deleted=len(old["pages"])))
        save_manifest(manifest)
    for path, sha in sorted(wanted.items()):
        old = files.get(path)
        if old and old["sha"] == sha and old.get("format") == FORMAT_VERSION:
            report.files.append(FileResult(path, "unchanged"))
            continue
        pages = build_pages(repo, tag, path, sha, fetch_blob(sha))
        for page in pages:
            store.write(page, tags)
        stale = sorted(set(old["pages"]) - {p.path for p in pages}) if old else []
        for page_path in stale:
            store.delete(page_path)
        files[path] = {
            "sha": sha,
            "tag": tag,
            "format": FORMAT_VERSION,
            "pages": [p.path for p in pages],
        }
        report.files.append(FileResult(path, "written", len(pages), len(stale)))
        save_manifest(manifest)
    manifest["tag"] = tag
    manifest["synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_manifest(manifest)
    return report


def resolve_installed_tag(package_json: Path) -> str:
    """Tag of the installed release: ``v`` + the package.json version."""
    version = json.loads(package_json.read_text("utf-8"))["version"]
    return f"v{version}"


# --------------------------------------------------------------------------- io


class AiMemoryStore:
    """Minimal MCP-over-HTTP writer for one ai-memory workspace/project."""

    def __init__(self, endpoint: str, token: str, workspace: str, project: str) -> None:
        self._url = endpoint.rstrip("/") + "/mcp"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._scope = {"workspace": workspace, "project": project}
        self._client = httpx.Client(timeout=60)
        self._id = 0

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {**arguments, **self._scope}},
        }
        resp = self._client.post(self._url, headers=self._headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result") or {}
        if "error" in data or result.get("isError"):
            raise RuntimeError(f"ai-memory {tool} failed: {json.dumps(data)[:300]}")
        parsed: dict[str, Any] = json.loads(result["content"][0]["text"])
        return parsed

    def write(self, page: Page, tags: list[str]) -> None:
        self.call(
            "memory_write_page",
            {
                "path": page.path,
                "body": page.body,
                "tags": tags,
                "kind": "reference",
                "tier": "semantic",
                "abstract": page.title,
                "entities": list(page.entities),
            },
        )

    def delete(self, path: str) -> None:
        self.call("memory_delete_page", {"path": path})


class DryRunStore:
    def write(self, page: Page, tags: list[str]) -> None:
        return None

    def delete(self, path: str) -> None:
        return None


def _gh(args: list[str]) -> str:
    done = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True)
    return done.stdout


def _fetch_blob(repo: str) -> Callable[[str], str]:
    def fetch(sha: str) -> str:
        data = json.loads(_gh([f"repos/{repo}/git/blobs/{sha}"]))
        return base64.b64decode(data["content"]).decode("utf-8")

    return fetch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="GitHub owner/name, e.g. cheaper-inference/OmniRoute")
    parser.add_argument("tag", help="git tag, or 'installed' to read --package-json")
    parser.add_argument("project", help="ai-memory project, e.g. ref-omniroute")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--package-json", type=Path)
    parser.add_argument("--paths-file", type=Path, help="import only these repo paths")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--endpoint", default=os.environ.get("AI_MEMORY_URL", DEFAULT_ENDPOINT))
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    tag = args.tag
    if tag == "installed":
        if args.package_json is None:
            parser.error("'installed' needs --package-json")
        tag = resolve_installed_tag(args.package_json)
    home = Path(os.environ.get("VERDICT_HOME") or Path.home() / ".verdict")
    manifest_path = args.manifest or home / "ref-docs" / f"{args.workspace}--{args.project}.json"
    manifest: dict[str, Any] = (
        json.loads(manifest_path.read_text("utf-8")) if manifest_path.is_file() else {}
    )

    def save(m: dict[str, Any]) -> None:
        if args.dry_run:
            return
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1, sort_keys=True), "utf-8")
        tmp.replace(manifest_path)

    store: PageStore
    if args.dry_run:
        store = DryRunStore()
    else:
        token = (
            args.token_file.read_text("utf-8").strip()
            if args.token_file
            else os.environ.get("AI_MEMORY_AUTH_TOKEN", "")
        )
        if not token:
            parser.error("set AI_MEMORY_AUTH_TOKEN or pass --token-file")
        store = AiMemoryStore(args.endpoint, token, args.workspace, args.project)
    tree = json.loads(_gh([f"repos/{args.repo}/git/trees/{tag}?recursive=1"]))
    if tree.get("truncated"):
        raise SystemExit("git tree is truncated; refusing a partial sync")
    only = set(args.paths_file.read_text("utf-8").split()) if args.paths_file else None
    report = sync(
        repo=args.repo,
        tag=tag,
        project=args.project,
        tree=tree["tree"],
        fetch_blob=_fetch_blob(args.repo),
        store=store,
        manifest=manifest,
        save_manifest=save,
        only=only,
    )
    out = {
        "repo": report.repo,
        "tag": report.tag,
        "project": report.project,
        "dry_run": args.dry_run,
        "totals": report.totals(),
        "files": [vars(f) for f in report.files],
    }
    sys.stdout.write(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
