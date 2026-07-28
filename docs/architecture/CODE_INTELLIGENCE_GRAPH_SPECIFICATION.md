# Code Intelligence Graph Specification

## Overview

The Verdict **Hybrid Code Intelligence Graph Engine** (`verdict/code_graph.py`) provides 100% offline, native Python AST code parsing, dependency tracking, architectural graph analytics (betweenness centrality bridge nodes and degree centrality hub hotspots), and deterministic query patterns.

It replaces heavy external code graphing tools and avoids storing raw external graph dumps in external databases. Instead, it extracts structural relationships directly from repository source code and syncs symbol summaries into the local Verdict `MemoryPlane` (`.verdict/memory.db`).

---

## Architecture & Node/Edge Taxonomy

### 1. Node Kinds (`NodeKind`)
- `File`: Source code module file.
- `Class`: Object-oriented class definition.
- `Function`: Function or method definition.
- `Type`: Type definition or alias.
- `Test`: Test function or test class (`test_*` pattern or inside `tests/` directory).

### 2. Edge Kinds (`EdgeKind`)
- `contains`: Parent-child containment (File -> Class/Function, Class -> Method).
- `calls`: Invocation relationship (Caller Function -> Callee Function/Symbol).
- `imports`: Module/symbol import relationship (File -> Imported Module/Symbol).
- `inherits`: Object-oriented inheritance (Child Class -> Base Class).
- `tests_for`: Quality assurance binding (Test Function -> Target Function/Class).
- `triggers`: Event or decorator triggering relationship.

---

## Core Query Patterns

1. **`callers_of(symbol_name)`**: Identifies all functions or test entities that invoke `symbol_name`.
2. **`callees_of(symbol_name)`**: Identifies all functions or external symbols invoked by `symbol_name`.
3. **`imports_of(target)`**: Lists modules and symbols imported by the target file or symbol.
4. **`importers_of(target)`**: Identifies all files or entities that import the specified target module or symbol.
5. **`tests_for(target)`**: Finds test functions associated with the target symbol.
6. **`inheritors_of(class_name)`**: Lists all subclasses that inherit from `class_name`.
7. **`file_summary(file_path)`**: Summarizes all class, function, and type entities contained within `file_path`.
8. **`get_impact_radius(changed_files, max_depth=2)`**: Performs Breadth-First Search (BFS) starting from `changed_files` up to `max_depth` hops to calculate blast radius before code modification.
9. **`get_affected_flows(changed_files)`**: Extracts execution flows and call chains passing through `changed_files`.
10. **`find_large_functions(min_lines=50)`**: Identifies functions or classes exceeding line count thresholds for code smell and refactoring audits.

---

## Architectural Analytics

### 1. Hub Nodes (`hub_nodes(top_n=10)`)
Calculates degree centrality ($k = k_{in} + k_{out}$) for all non-File entities to identify architectural hotspots—symbols with high connectivity whose modification carries high risk.

### 2. Bridge Nodes (`bridge_nodes(top_n=10)`)
Calculates shortest-path betweenness centrality to discover architectural chokepoints—symbols that bridge otherwise isolated code modules or communities.

---

## MemoryPlane Integration

The `CodeGraphEngine` syncs parsed nodes and analytical summaries directly into the local `MemoryPlane` under the `code_graph` namespace. This allows cross-tool and cross-session semantic search across codebase symbols using RuVector vector embeddings without requiring external network access or OpenViking services.
