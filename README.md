# llm-gate

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](docs/specs/RELEASE_ACCEPTANCE.md)

`llm-gate` is a Python routing library and local OpenAI-compatible proxy. It evaluates request criticality, applies deterministic safety heuristics, selects a configured model, and forwards standard chat-completion traffic to an upstream such as OmniRoute.

> **Honest status:** the proxy and catalog slices are implemented and tested, but this project is not yet a production-ready release. Live upstream availability, local authentication, legal retry/fallback, managed intelligence, and end-to-end OmniRoute evidence remain tracked release gates.

## What is implemented

- `Gate.route()` for direct, explainable criticality-based routing.
- `POST /v1/chat/completions` with minimal model rewriting.
- Unknown request fields, tools, response-format options, usage, errors, and streamed bytes are preserved by the transport layer.
- Server-owned upstream authentication. Client-provided authorization is not forwarded.
- `GET /v1/models` with local allow/deny filtering through `LLMGATE_MODEL_ALLOWLIST` and `LLMGATE_MODEL_DENYLIST`.
- Explicit `llm_gate.availability_state: "unknown"` metadata so catalog rows are not presented as live or healthy.
- `GET /health`, upstream-aware `GET /ready`, and the existing `POST /v1/route` explain-only API.
- Request-size enforcement through `LLMGATE_MAX_REQUEST_BYTES`.

## Install and run

For the library and CLI:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,server]'
```

Start the development proxy with an explicit upstream:

```bash
export LLMGATE_UPSTREAM_BASE_URL=http://127.0.0.1:20132/v1
export OMNIROUTE_API_KEY=your-local-key
llm-gate serve --host 127.0.0.1 --port 8000
```

The proxy routes to `LLMGATE_PRIMARY` when no discovered provider candidate is eligible. The default remains `anthropic/claude-3-opus-20240229` for compatibility with the original routing API. Set it explicitly for a local OmniRoute catalog, for example:

```bash
export LLMGATE_PRIMARY=cx/gpt-5.4-mini
```

Smoke test the process and proxy surface:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/v1/models
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"client-model","messages":[{"role":"user","content":"hello"}]}'
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLMGATE_UPSTREAM_BASE_URL` | `http://127.0.0.1:20132/v1` | Fixed upstream OpenAI-compatible base URL |
| `LLMGATE_UPSTREAM_API_KEY` | empty | Server-owned upstream bearer token |
| `OMNIROUTE_API_KEY` | empty | Convenience fallback for OmniRoute deployments |
| `LLMGATE_PRIMARY` | `anthropic/claude-3-opus-20240229` | Safe primary model fallback |
| `LLMGATE_MODEL_ALLOWLIST` | empty | Comma-separated exact model IDs to expose |
| `LLMGATE_MODEL_DENYLIST` | empty | Comma-separated exact model IDs to hide |
| `LLMGATE_MAX_REQUEST_BYTES` | `2097152` | Maximum request body size |
| `LLMGATE_UPSTREAM_TIMEOUT_MS` | `30000` | Upstream request timeout |
| `LLMGATE_LOG_PATH` | `llm-gate-decisions.jsonl` | Redacted decision log path |

The upstream URL is process configuration, not a per-request field. Do not put credentials in request JSON or commit them to the repository.

## Direct routing API

The original explain-only API remains available without proxying model bytes:

```bash
llm-gate route "deploy the production database" --criticality critical
llm-gate route "format this JSON" --criticality low --terse
```

The HTTP equivalent is `POST /v1/route` with `{ "task": "...", "criticality": "..." }`.

## Verification

Use the declared environment for reproducible checks:

```bash
python -m pytest -q
ruff check llm_gate tests
ruff format --check llm_gate tests
mypy llm_gate
python -m build
python -m twine check dist/*
```

The current clean-environment checkpoint is 71 tests passing, with Ruff, format, mypy, wheel/sdist build, and `twine check` passing. Warnings from the FastAPI/Starlette test-client compatibility layer do not fail the suite.

## Release scope and open gates

The v0.2 product specification and acceptance matrix are normative:

- [Product specification](docs/specs/PRODUCT_SPEC_V0.2.md)
- [Release acceptance matrix](docs/specs/RELEASE_ACCEPTANCE.md)
- [Routing policy](docs/specs/ROUTING_POLICY.md)

Before calling the project production-ready, the implementation still needs local auth, SSRF-safe URL validation, filtered dispatch tied to live capability and headroom state, legal idempotent fallback, required Ruflo/RuVector intelligence, raw HTTP and SDK compatibility smoke tests, and a configured OmniRoute live test.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and narrowly scoped pull requests are welcome. Please include the exact command output used to validate behavior and avoid sharing API keys or raw prompts.

## License

MIT. See [LICENSE](LICENSE).
