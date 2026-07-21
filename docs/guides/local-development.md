# Local Development Guide

## Prerequisites

- Python 3.10+
- Node.js 18+ (for verdict-node, verdict-cockpit)
- Docker (for OmniRoute)
- Git

## Setup

### 1. Clone the Ecosystem

```bash
# Core (Python control plane)
git clone https://github.com/verdict/verdict-core.git
cd verdict-core

# Optional: Other repos
git clone https://github.com/verdict/verdict-node.git
git clone https://github.com/verdict/verdict-cockpit.git
git clone https://github.com/verdict/verdict-risk.git
git clone https://github.com/verdict/verdict-edge.git
git clone https://github.com/verdict/verdict-backtest.git
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

```bash
# Start OmniRoute for 3,318+ models (90+ free tiers)
docker run -d -p 20128:20128 omnibus/omniroute

# Verify
curl http://localhost:20128/v1/models | jq '.data | length'
# → 3318
```

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
# All tests (320 tests at the time of writing; run the command for the current count)
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
docker ps | grep omniroute
# If not running: docker run -d -p 20128:20128 omnibus/omniroute
```

### Tests failing on import
```bash
PYTHONPATH=. pytest tests/
```
