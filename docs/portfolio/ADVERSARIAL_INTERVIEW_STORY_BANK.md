# Adversarial Interview Story Bank & Counterpoints

This story bank follows the STAR/SCAR framework (Situation, Task, Action, Result / Situation, Challenge, Action, Result) with explicit adversarial counterpoints and objections designed for technical interviews with Staff/Principal engineers, hiring managers, and quantitative recruiters.

---

## Story 1: Eliminating Cloud Dependency by Building a Local Memory Plane

### Situation
Our AI coding swarm relied on external cloud embedding providers and OpenViking for cross-session context recall. Frequent API rate limits, network outages, and schema drift caused agent workflows to stall or fail unpredictably.

### Challenge
We needed a local, zero-network memory architecture that could store millions of vector embeddings and document chunks on developer machines while keeping search latency under 5 milliseconds.

### Action
I designed and built Verdict's `MemoryPlane` using native SQLite and an HNSW vector index (`ruvector.db`). I implemented a transactional migration engine (`memory_migration.py`) that archived legacy OpenViking stores, ingested their records, and purged obsolete files.

### Result
- Reduced context lookup latency from ~450ms (cloud API) to <4ms (local SQLite + HNSW).
- Eliminated 100% of OpenViking network calls and embedding quota errors.
- Unified memory access across 9 AI agent tools (Codex, Claude Code, Pi, Ruflo, Cursor, Hermes).

### Adversarial Objections & Responses
- **Objection**: *Why build your own SQLite + HNSW wrapper instead of using ChromaDB or Qdrant?*
- **Response**: ChromaDB and Qdrant introduce heavy C++ or Rust daemon dependencies, python package bloat, and port management issues. A lightweight, embedded SQLite + C/Python HNSW index gives us single-file database portability, zero daemon overhead, and zero port conflicts across multi-process swarms.

---

## Story 2: Operating Automated Prediction Market Bots on Kalshi

### Situation
Kalshi prediction markets offer event-driven binary contracts, but orderbooks fluctuate rapidly around high-impact news releases (economic CPI, Fed rate decisions). Manual trading cannot capture microsecond pricing dislocations.

### Challenge
Standard Python JSON parsers and HTTP clients were too slow to process high-volume L2 WebSocket orderbook streams and update bid/ask spreads before adverse selection hit.

### Action
I built an asynchronous Python SDK (`prediction-market-sdk`) utilizing `msgspec` zero-allocation struct parsing. I engineered a real-time risk gate enforcing hard position caps, slippage bounds, and automated drawdown circuit breakers.

### Result
- Processed over 100,000 WebSocket orderbook deltas per second with sub-10ms event-to-order latency.
- Maintained automated strategy execution over multi-month live trading windows with zero risk bound breaches.

### Adversarial Objections & Responses
- **Objection**: *Python is inherently slow for HFT. Why didn't you write the bot in C++ or Rust?*
- **Response**: For prediction markets like Kalshi, API network latency (~20-50ms REST/WS) dominates microsecond execution engine latency. Using Python AsyncIO with `msgspec` C-extensions gave us microsecond-level parsing speed while preserving rapid strategy iteration, seamless data science library integration, and clean asynchronous IO concurrency.

---

## Story 3: Designing a Harness-Agnostic Guidance Enforcement Boundary

### Situation
Different AI agent tools (Codex, Claude Code, Pi, Cursor) format instructions differently, leading to inconsistent safety enforcement where an agent in one tool might execute dangerous shell commands or violate project boundaries.

### Challenge
We required a unified, harness-agnostic guidance boundary that could enforce safety rules across any AI tool without locking the user into a single vendor's CLI format.

### Action
I created `verdict/guidance.py` and integrated rules enforcement into a 6-category lifecycle hook matrix (`on_prompt`, `pre_edit`, `pre_command`, `session_end`). I built `verdict doctor --fix` for auto-repair and `verdict uninstall` for clean, reversible header stripping.

### Result
- Unified rules enforcement across 9 tool ecosystems.
- 100% test coverage with zero code disruption during installation or removal.

### Adversarial Objections & Responses
- **Objection**: *Can't an LLM simply ignore system prompt guidance if its prompt injection vulnerability is triggered?*
- **Response**: Prompt guidance alone is insufficient. That's why Verdict pairs prompt guidance with deterministic programmatic lifecycle hooks (`pre_command` shell parsing, `pre_edit` path validation). Even if an LLM is prompt-injected, the programmatic hook layer intercepts and blocks non-compliant file writes or dangerous shell executions before they hit the OS kernel.
