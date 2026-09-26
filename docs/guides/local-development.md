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

### 3. OmniRoute (Local)

OmniRoute is an external gateway and is not bundled with Verdict. Install and start it
per its own documentation, then check that the inventory endpoint answers:

```bash
curl -s http://localhost:20128/v1/models | jq '.data | length'   # live model count
```

For parallel agent workloads, apply the admission settings in
[the orchestration golden path prerequisites](orchestration-golden-path.md#prerequisites).

### 4. Run Verdict Core Server

```bash
# With OmniRoute integration
export OMNIROUTE_BASE_URL=http://localhost:20128
verdict serve --host 0.0.0.0 --port 8000

# Test
curl -X POST http://localhost:8000/v1/route \
  -H "Content-Type: application/json" \
  -d '{"task": "Write a Python function", "criticality": "medium"}'
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
