# Local Development Guide

## Prerequisites

- Python 3.10+
- Node.js 18+ (for verdict-node, verdict-cockpit)
- OmniRoute (optional, for live routing/orchestration; installed separately)
- Git

## Setup

### 1. Clone the Ecosystem

```bash
# Core (Python control plane)
git clone https://github.com/mrnicholasbcarter-code/verdict-core.git
cd verdict-core

# Optional: other ecosystem repos (same owner)
git clone https://github.com/mrnicholasbcarter-code/verdict-node.git
git clone https://github.com/mrnicholasbcarter-code/verdict-cockpit.git
```

### 2. Python Environment

```bash
cd verdict-core
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,server,dashboard]'

# Verify
verdict --help
pytest -v
```

### 3. Recommended Tooling

`scripts/dev-start.sh` reports which recommended tools are installed. Install the missing ones with:

```bash
scripts/dev-tooling.sh --install   # Homebrew/Linuxbrew, uv tool, npm
scripts/dev-tooling.sh --strict    # exit 1 if any recommended tool is missing
```

The set covers search (`rg`, `fd`, `jq`), structural code search (`ast-grep`), static analysis
(`semgrep`, `shellcheck`, `actionlint`), hygiene (`typos`, `codespell`, `lychee`, `vulture`,
`deptry`), secret scanning (`gitleaks`), the GitHub CLI (`gh`, preferred over raw REST calls) and
the OpenSpec CLI. None of them is a runtime dependency of Verdict.

### 4. OmniRoute (Local)

OmniRoute is an external gateway and is not bundled with Verdict. Install and start it
per its own documentation, then check that the inventory endpoint answers:

```bash
curl -s http://localhost:20128/v1/models | jq '.data | length'   # live model count
```

For parallel agent workloads, apply the admission settings in
[the orchestration golden path prerequisites](orchestration-golden-path.md#prerequisites).

### 5. Run Verdict Core Server

```bash
# Deliberately anonymous development server: loopback only.
export OMNIROUTE_BASE_URL=http://127.0.0.1:20128
export LLMGATE_ALLOW_ANONYMOUS=true
verdict serve --host 127.0.0.1 --port 8000
```

For a non-loopback bind, configure `LLMGATE_AUTH_TOKEN` and a durable
`VERDICT_RECEIPTS_DB` first. Send the bearer token on each request. Do not expose
anonymous mode; startup rejects it on non-loopback interfaces.
See [SECURITY.md](../../SECURITY.md).

`POST /v1/route` is a decision endpoint, not a completion endpoint. It requires
an `execution_path_request`; a task-only body returns HTTP 400. The following
shows the public contract shape. Replace `provider/model` and the candidate
fields, prices and observation date with real, current evidence for your route.
Non-free candidates require a `price` evidence object. These example fields are
not admission proof and do not guarantee selection. This call can trigger
configured discovery or confirmation probes; run it only with consent and a
budget for that upstream.

```bash
curl -X POST http://127.0.0.1:8000/v1/route \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Write a Python function",
    "criticality": "medium",
    "execution_path_request": {
      "schema_version": "1",
      "trajectory_id": "local-development",
      "slice_id": "function",
      "acceptance_criteria": ["function passes its tests"],
      "proof_criteria": ["selected and served identities match"],
      "candidates": [{
        "strategy": "direct_cheap",
        "route_id": "provider/model",
        "gateway": "verdict-upstream",
        "provider": "omniroute",
        "model": "provider/model",
        "capability_tier": 2,
        "eligible": true,
        "is_free": false,
        "price": {
          "input_usd_per_mtok": "1",
          "output_usd_per_mtok": "1",
          "observed_at": "2026-09-29T00:00:00Z",
          "evidence_id": "replace-with-current-price-evidence"
        },
        "execution_tokens": 256,
        "verification_tokens": 64,
        "certification_state": "ready",
        "certification_freshness": "fresh"
      }]
    }
  }'
```

---

## Running Tests

```bash
# All tests (2907 at the time of writing; run the command for the current count)
pytest -v

# Specific test file
pytest tests/test_availability_cache.py -v

# With coverage
pytest --cov=verdict --cov-report=html

# Type checking
uv run --extra dev --extra dashboard --extra server mypy verdict --strict

# Linting
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server ruff format --check .
```

---

## Frontend Development

### verdict-node (TypeScript)

```bash
cd verdict-node
npm install
npm run typecheck
npm test
npm run build
```

### verdict-cockpit (Next.js)

```bash
cd verdict-cockpit
npm install
npm run dev  # http://localhost:3000
```

---

## Debugging

### Enable Debug Logging

```bash
VERDICT_DEBUG=1 verdict route "test"
export VERDICT_LOG_PATH=./verdict-debug.jsonl
verdict serve
```

### Inspect Decision Log

```bash
cat verdict-decisions.jsonl | jq .
```

---

## Contributing

1. Create feature branch: `git checkout -b feat/your-feature`
2. Make changes with tests
3. Run full test suite: `pytest && mypy verdict --strict && ruff check .`
4. Submit PR

---

## Common Issues

### "Module not found: verdict"
```bash
pip install -e .  # From verdict-core root
```

### OmniRoute connection refused
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:20128/v1/models   # expect 200
# If not 200, start OmniRoute per its own documentation (e.g. its systemd user service).
```

### Tests failing on import
```bash
PYTHONPATH=. pytest tests/
```
