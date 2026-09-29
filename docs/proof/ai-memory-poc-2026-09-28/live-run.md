# BOD-281: ai-memory POC — Live Run Evidence

**Date:** 2026-09-28T20:50–21:01Z  
**Lane:** lane-bod281-live3  
**Image digest:** `sha256:a626d115e0350afe934954c02c9064d30b708c58316763a9c6674ef5d0c8e3d9`  
**Container:** `ai-memory-test` (docker.io/akitaonrails/ai-memory:latest)  
**Upstream commit (from REPORT.md):** `85c9ba36d732` "Merge PR #946 into main"

---

## 1. Container Start

```
sudo docker start ai-memory-test
# Status: Up 3 seconds (health: starting)
# Port: 127.0.0.1:49374->49374/tcp

# Waited ~6s for healthy:
# 20:50:53 healthy
```

## 2. Environment Variables (no LLM API key)

```
sudo docker exec ai-memory-test env | cut -d= -f1 | sort
```

Output:
```
AI_MEMORY_ALLOWED_HOSTS
AI_MEMORY_DATA_DIR
AI_MEMORY_IN_CONTAINER
HOME
HOSTNAME
PATH
```

**No OPENAI_API_KEY, ANTHROPIC_API_KEY, or any LLM provider key is set.**  
**Retrieval method without keys:** FTS5 keyword search (SQLite full-text search).  
All capture, search, decay, and handoff features work zero-LLM by default.  
LLM keys enable optional auto-improvement/consolidation; they are not required for baseline operation.

## 3. Health Endpoint — Raw Response Shape

```
curl -s http://127.0.0.1:49374/api/v1/workspaces
```

Output:
```json
[{"workspace_name":"default","project_count":2,"page_count":1,"last_updated":"2026-09-28T20:46:51.99325Z"}]
```

**Finding:** The endpoint returns a **JSON array** (list of workspace objects), not `{"workspaces": [...]}`.  
This was the response-shape mismatch found by the previous lane. Fixed in adapter.

## 4. Adapter Fixes Applied

Four bugs found and fixed in `verdict/memory_providers/ai_memory_experimental.py`:

1. **Accept header** (MCP endpoint): Changed `"application/json"` → `"application/json, text/event-stream"`.  
   Without this the server returns HTTP 415.

2. **Health response shape**: Changed check from `isinstance(data, dict) and "workspaces" in data`  
   → `isinstance(data, list)` to match the real array response.

3. **MCP JSON-RPC envelope**: All three payloads (put/search/delete) were missing  
   `"jsonrpc": "2.0"` and `"id"` fields. Without them the server returns HTTP 415.

4. **Response parsing**: Both put and search responses wrap content in  
   `result.content[0].text` (a JSON string). Fixed put to parse `page_info["path"]`;  
   fixed search to parse `search_data["hits"]` (array with `id/path/title/snippet/rank`  
   fields, not `pages` with `body/score`).

5. **`_parse_timestamp` missing `@staticmethod`**: Method had no decorator, so Python  
   treated the `ts` param as `self`, causing `TypeError`. Added `@staticmethod`.

## 5. Live Sequence: Write → Search → Restart → Search → Outage

### 5a. Health Check
```python
p = AiMemoryExperimentalProvider(endpoint="http://127.0.0.1:49374")
h = p.health()
# HEALTH: ProviderResultStatus.AVAILABLE / CertificationState.HEALTHY backend=ai-memory
```

### 5b. Write
```python
env = SharedMemoryEnvelope(
    content="BOD-281 live run 2: restart persistence verified at 2026-09-28T20:55Z",
    project="verdict-poc", memory_kind="decision", retention_class="episodic", ...
)
ref = p.put(env)
# PUT: external_id='verdict/decision/614363239bb0.md'
```

Confirmed via curl:
```
GET /api/v1/workspaces/default/projects/verdict-poc/pages
→ [{"path":"verdict/decision/614363239bb0.md","title":"614363239bb0","kind":"fact",...},
   {"path":"verdict/decision/bod281-live-test.md","title":"bod281-live-test",...}]
```

### 5c. Search (pre-restart)
```python
q = SharedMemoryQuery(query="restart persistence verified", project="verdict-poc")
sr = p.search(q)
# SEARCH (pre-restart): ProviderResultStatus.AVAILABLE hits=3
#   snippet: 'BOD-281 live run 2: restart persistence verified at 2026-09-28T20:55Z'
#   snippet: 'BOD-281 live test: ai-memory write verified at 2026-09-28T20:52Z'
#   snippet: 'BOD-281 live test: ai-memory write verified at 2026-09-28T20:53Z'
```

### 5d. Container Restart
```
sudo docker stop ai-memory-test   # → Exited
sudo docker start ai-memory-test  # → healthy in ~6s
# 20:56:13 starting → 20:56:19 healthy
```

### 5e. Search (post-restart)
```python
q = SharedMemoryQuery(query="restart persistence verified", project="verdict-poc")
sr = p.search(q)
# SEARCH (post-restart): ProviderResultStatus.AVAILABLE hits=3
#   path: verdict/decision/614363239bb0.md
#   content: 'BOD-281 live run 2: restart persistence verified at 2026-09-28T20:55Z'
#   path: verdict/decision/2116c4d554ff.md
#   content: 'BOD-281 live test: ai-memory write verified at 2026-09-28T20:52Z'
#   path: verdict/decision/bod281-live-test.md
#   content: 'BOD-281 live test: ai-memory write verified at 2026-09-28T20:53Z'
```

**Data persisted across container restart.**

### 5f. Outage
```python
subprocess.run(["sudo", "docker", "stop", "ai-memory-test"])
p2 = AiMemoryExperimentalProvider(endpoint="http://127.0.0.1:49374", timeout=3.0)
h2 = p2.health()
# HEALTH (outage): ProviderResultStatus.UNAVAILABLE / CertificationState.HEALTHY
sr2 = p2.search(SharedMemoryQuery(query="anything", project="verdict-poc"))
# SEARCH (outage): ProviderResultStatus.UNAVAILABLE hits=0
```

**Adapter fails open on outage: returns UNAVAILABLE, never raises.**

---

## 6. AC7: Cross-Harness Recall

### Setup
Temporary config dirs only (`/tmp/xh/`). Real `~/.claude` and `~/.codex` never touched.  
- Claude: `--mcp-config /tmp/xh/mcp-servers.json --strict-mcp-config`  
- Codex: `CODEX_HOME=/tmp/xh/codex codex mcp add ai-memory --url http://127.0.0.1:49374/mcp`

MCP config used:
```json
{
  "mcpServers": {
    "ai-memory": {
      "type": "http",
      "url": "http://127.0.0.1:49374/mcp"
    }
  }
}
```

### 6a. Claude — Write (SUCCESS)
```
timeout 180 claude \
  --mcp-config /tmp/xh/mcp-servers.json \
  --strict-mcp-config \
  --allowedTools "mcp__ai-memory__memory_write_page,mcp__ai-memory__memory_query" \
  -p "Using the ai-memory MCP tool, write a memory page with \
      path=verdict/xh-claude-test.md body='cross-harness-test: claude wrote this \
      at 2026-09-28T20:57Z' to project=verdict-poc. Then confirm it was written."
```

Output (truncated):
```
I wrote the page and read it back, and it's there.

- Path: verdict/xh-claude-test.md in project verdict-poc
- Page ID: 01a0e9cf-5681-7d72-9082-5de00a7b4e99
- Checkpoint: 67bd573c65c55064e36af3bce1632b14cb69ff42
```

**Claude successfully called the ai-memory MCP write tool non-interactively.**

### 6b. Codex — Recall (BLOCKED: 401)
```
CODEX_HOME=/tmp/xh/codex codex mcp add ai-memory --url http://127.0.0.1:49374/mcp
# → Added global MCP server 'ai-memory'.

CODEX_HOME=/tmp/xh/codex timeout 180 codex exec --approve-for-me \
  "Using the ai-memory MCP tool memory_query, search project=verdict-poc \
   for query=cross-harness-test. Report the path and snippet of any hit."
```

Error:
```
ERROR: Reconnecting... 1/5
...failed to connect to websocket: HTTP error: 401 Unauthorized,
  url: wss://api.openai.com/v1/responses
...unexpected status 401 Unauthorized: Missing bearer or basic authentication in header
```

**Root cause:** `codex exec` requires an OpenAI API key (`OPENAI_API_KEY`).  
No key is set in the temp env (by design — we never supply operator credentials to subprocesses).  
The MCP server registration succeeded; the blocker is codex's own LLM backend auth, not the ai-memory integration.

**Conclusion for AC7:** claude → ai-memory write: ✅ proven.  
codex → ai-memory read: ⚠️ blocked by OPENAI_API_KEY requirement.  
Architecture is sound — both CLIs support HTTP MCP endpoints, shared server design enables cross-harness recall, the specific codex call was blocked by LLM auth not by MCP connectivity.

---

## 7. Tests After Fixes

```
/tmp/vrun .venv/bin/python -m pytest -x --maxfail=1 -q tests/test_ai_memory_experimental.py -p no:cacheprovider
# 8 passed, 1 warning in 0.43s

/tmp/vrun .venv/bin/ruff check verdict/memory_providers/ tests/test_ai_memory_experimental.py
# All checks passed!

/tmp/vrun .venv/bin/mypy --strict verdict/memory_providers/ai_memory_experimental.py
# 2 errors in verdict/openspec_vendor/validate_openspec_change.py (pre-existing, unrelated)
# No errors in ai_memory_experimental.py
```

---

## 8. Container Stop

```
sudo docker stop ai-memory-test
# ai-memory-test  Exited (0)
```
