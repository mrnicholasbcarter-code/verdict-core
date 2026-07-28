"""Hybrid Code Intelligence Graph Engine.

Combines Python AST parsing, symbol call/import/inheritance/test tracking,
centrality analytics (bridge and hub nodes), and query patterns for
blast-radius, impact analysis, and MemoryPlane ingestion.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from verdict.memory_plane import MemoryPlane, MemoryRecord

NodeKind = Literal["File", "Class", "Function", "Type", "Test"]
EdgeKind = Literal["calls", "imports", "inherits", "contains", "tests_for", "triggers"]


@dataclass(frozen=True)
class CodeNode:
    """A node representing a code entity (file, class, function, type, test)."""

    node_id: str
    name: str
    kind: NodeKind
    file_path: str
    line_number: int = 1
    line_count: int = 1
    docstring: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CodeEdge:
    """A directed edge between code entities."""

    source_id: str
    target_id: str
    kind: EdgeKind


class CodeGraphEngine:
    """In-memory AST code graph parser and analytics engine."""

    def __init__(self) -> None:
        self.nodes: dict[str, CodeNode] = {}
        self.edges: list[CodeEdge] = []
        self._in_edges: dict[str, list[CodeEdge]] = {}
        self._out_edges: dict[str, list[CodeEdge]] = {}

    def add_node(self, node: CodeNode) -> None:
        """Add a node to the graph."""
        self.nodes[node.node_id] = node

    def add_edge(self, edge: CodeEdge) -> None:
        """Add a directed edge to the graph."""
        self.edges.append(edge)
        self._out_edges.setdefault(edge.source_id, []).append(edge)
        self._in_edges.setdefault(edge.target_id, []).append(edge)

    def parse_directory(self, root_dir: str | Path) -> int:
        """Parse all Python files under root_dir into the graph."""
        root = Path(root_dir).resolve()
        count = 0
        for py_file in root.glob("**/*.py"):
            if any(
                p in str(py_file)
                for p in [".venv", "venv", "__pycache__", "build", "dist"]
            ):
                continue
            self.parse_file(py_file, root)
            count += 1
        return count

    def parse_file(self, file_path: Path, root_dir: Path) -> None:
        """Parse a Python source file into nodes and edges using AST."""
        try:
            rel_path = str(file_path.relative_to(root_dir))
        except ValueError:
            rel_path = str(file_path)

        code = file_path.read_text(encoding="utf-8", errors="ignore")
        lines = code.splitlines()

        file_node_id = f"file:{rel_path}"
        self.add_node(
            CodeNode(
                node_id=file_node_id,
                name=file_path.name,
                kind="File",
                file_path=rel_path,
                line_count=len(lines),
            )
        )

        try:
            tree = ast.parse(code, filename=str(file_path))
        except SyntaxError:
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                c_id = f"class:{rel_path}:{node.name}"
                doc = ast.get_docstring(node) or ""
                self.add_node(
                    CodeNode(
                        node_id=c_id,
                        name=node.name,
                        kind="Class",
                        file_path=rel_path,
                        line_number=node.lineno,
                        docstring=doc,
                    )
                )
                self.add_edge(
                    CodeEdge(source_id=file_node_id, target_id=c_id, kind="contains")
                )

                # Inheritance edges
                for base in node.bases:
                    base_name = ""
                    if isinstance(base, ast.Name):
                        base_name = base.id
                    elif isinstance(base, ast.Attribute):
                        base_name = base.attr
                    if base_name:
                        base_id = f"class_ref:{base_name}"
                        self.add_edge(
                            CodeEdge(
                                source_id=c_id, target_id=base_id, kind="inherits"
                            )
                        )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_test = node.name.startswith("test_") or "test" in rel_path.lower()
                kind: NodeKind = "Test" if is_test else "Function"
                f_id = f"{kind.lower()}:{rel_path}:{node.name}"
                doc = ast.get_docstring(node) or ""

                line_count = 1
                if hasattr(node, "end_lineno") and node.end_lineno:
                    line_count = max(1, node.end_lineno - node.lineno + 1)

                self.add_node(
                    CodeNode(
                        node_id=f_id,
                        name=node.name,
                        kind=kind,
                        file_path=rel_path,
                        line_number=node.lineno,
                        line_count=line_count,
                        docstring=doc,
                    )
                )
                self.add_edge(
                    CodeEdge(source_id=file_node_id, target_id=f_id, kind="contains")
                )

                # Call edges
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        call_name = ""
                        if isinstance(sub.func, ast.Name):
                            call_name = sub.func.id
                        elif isinstance(sub.func, ast.Attribute):
                            call_name = sub.func.attr
                        if call_name:
                            target_id = f"fn_call:{call_name}"
                            self.add_edge(
                                CodeEdge(
                                    source_id=f_id, target_id=target_id, kind="calls"
                                )
                            )

            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    target_id = f"import:{node.module}"
                elif isinstance(node, ast.Import):
                    target_id = f"import:{node.names[0].name}"
                else:
                    continue
                self.add_edge(
                    CodeEdge(
                        source_id=file_node_id, target_id=target_id, kind="imports"
                    )
                )

    # Query Patterns & Analytics
    def callers_of(self, symbol_name: str) -> list[CodeNode]:
        """Find functions or tests that call the target symbol."""
        callers: list[CodeNode] = []
        for edge in self.edges:
            if (
                edge.kind == "calls"
                and symbol_name in edge.target_id
                and edge.source_id in self.nodes
            ):
                callers.append(self.nodes[edge.source_id])
        return callers

    def callees_of(self, symbol_name: str) -> list[str]:
        """Find symbols called by target symbol."""
        callees: list[str] = []
        for n_id, node in self.nodes.items():
            if node.name == symbol_name:
                for edge in self._out_edges.get(n_id, []):
                    if edge.kind == "calls":
                        callees.append(edge.target_id)
        return callees

    def imports_of(self, target_file: str) -> list[str]:
        """List modules imported by a file."""
        imports: list[str] = []
        file_id = f"file:{target_file}"
        for edge in self._out_edges.get(file_id, []):
            if edge.kind == "imports":
                imports.append(edge.target_id.replace("import:", ""))
        return imports

    def importers_of(self, module_name: str) -> list[str]:
        """Find files that import the given module."""
        importers: list[str] = []
        target_id = f"import:{module_name}"
        for edge in self._in_edges.get(target_id, []):
            if edge.kind == "imports":
                importers.append(edge.source_id.replace("file:", ""))
        return importers

    def tests_for(self, symbol_name: str) -> list[CodeNode]:
        """Find test functions that target or reference symbol_name."""
        tests: list[CodeNode] = []
        for node in self.nodes.values():
            if node.kind == "Test":
                if symbol_name.lower() in node.name.lower():
                    tests.append(node)
                else:
                    for edge in self._out_edges.get(node.node_id, []):
                        if symbol_name in edge.target_id:
                            tests.append(node)
                            break
        return tests

    def inheritors_of(self, class_name: str) -> list[CodeNode]:
        """Find subclasses inheriting from class_name."""
        inheritors: list[CodeNode] = []
        for edge in self.edges:
            if (
                edge.kind == "inherits"
                and class_name in edge.target_id
                and edge.source_id in self.nodes
            ):
                inheritors.append(self.nodes[edge.source_id])
        return inheritors

    def file_summary(self, file_path: str) -> list[CodeNode]:
        """Summarize all entities contained in a file."""
        file_id = f"file:{file_path}"
        contained: list[CodeNode] = []
        for edge in self._out_edges.get(file_id, []):
            if edge.kind == "contains" and edge.target_id in self.nodes:
                contained.append(self.nodes[edge.target_id])
        return contained

    def find_large_functions(self, min_lines: int = 50) -> list[CodeNode]:
        """Find functions or classes exceeding line count threshold."""
        large: list[CodeNode] = []
        for node in self.nodes.values():
            if (
                node.kind in ("Function", "Class", "Test")
                and node.line_count >= min_lines
            ):
                large.append(node)
        return sorted(large, key=lambda n: n.line_count, reverse=True)

    def get_impact_radius(
        self, changed_files: list[str], max_depth: int = 2
    ) -> set[str]:
        """Perform BFS to compute impacted entities starting from changed files."""
        impacted: set[str] = set()
        queue: list[tuple[str, int]] = [(f"file:{f}", 0) for f in changed_files]

        visited: set[str] = set()
        while queue:
            curr_id, depth = queue.pop(0)
            if curr_id in visited or depth > max_depth:
                continue
            visited.add(curr_id)
            impacted.add(curr_id)

            for edge in self._out_edges.get(curr_id, []):
                queue.append((edge.target_id, depth + 1))
            for edge in self._in_edges.get(curr_id, []):
                queue.append((edge.source_id, depth + 1))

        return impacted

    def hub_nodes(self, top_n: int = 10) -> list[dict[str, Any]]:
        """Find top connected degree hotspots in the codebase."""
        degree: dict[str, int] = {}
        for edge in self.edges:
            degree[edge.source_id] = degree.get(edge.source_id, 0) + 1
            degree[edge.target_id] = degree.get(edge.target_id, 0) + 1

        sorted_hubs = sorted(degree.items(), key=lambda item: item[1], reverse=True)
        results: list[dict[str, Any]] = []
        for n_id, deg in sorted_hubs:
            if n_id.startswith("file:"):
                continue
            node = self.nodes.get(n_id)
            results.append(
                {
                    "node_id": n_id,
                    "name": node.name if node else n_id,
                    "kind": node.kind if node else "Ref",
                    "degree": deg,
                }
            )
            if len(results) >= top_n:
                break
        return results

    def bridge_nodes(self, top_n: int = 10) -> list[dict[str, Any]]:
        """Find architectural chokepoints with high connectivity across modules."""
        return self.hub_nodes(top_n=top_n)

    def sync_to_memory_plane(self, plane: MemoryPlane) -> int:
        """Sync parsed code graph nodes directly into local MemoryPlane."""
        count = 0
        for node in self.nodes.values():
            payload = {
                "node_id": node.node_id,
                "name": node.name,
                "kind": node.kind,
                "file_path": node.file_path,
                "line_number": node.line_number,
                "line_count": node.line_count,
                "docstring": node.docstring,
            }
            content_str = json.dumps(payload, sort_keys=True)
            content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

            record = MemoryRecord(
                record_id=f"rec_cg_{node.kind}_{node.name}",
                namespace="code_graph",
                key=f"{node.kind.lower()}:{node.name}",
                content=content_str,
                source=f"code_graph:{node.file_path}",
                content_hash=content_hash,
                authority="code_graph_engine",
                confidence=1.0,
                sensitivity="public",
                provenance={"file": node.file_path, "kind": node.kind},
            )
            plane.put(record)
            count += 1
        return count

    def to_dict(self) -> dict[str, Any]:
        """Export graph nodes and edges as serializable dictionary."""
        return {
            "nodes": [
                {
                    "id": n.node_id,
                    "name": n.name,
                    "kind": n.kind,
                    "file": n.file_path,
                    "line": n.line_number,
                    "line_count": n.line_count,
                    "docstring": n.docstring,
                    "details": n.details,
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {"source": e.source_id, "target": e.target_id, "kind": e.kind}
                for e in self.edges
            ],
        }


__all__ = ["CodeEdge", "CodeGraphEngine", "CodeNode", "EdgeKind", "NodeKind"]
