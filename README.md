# llm-gate

<p align="center"><b>Policy-safe, availability-aware LLM routing and workflow orchestration.</b></p>

[![CI](https://img.shields.io/github/actions/workflow/status/mrnicholasbcarter-code/llm-gate/ci.yml?style=flat-square&label=CI)](https://github.com/mrnicholasbcarter-code/llm-gate/actions)
[![PyPI](https://img.shields.io/pypi/v/llm-gate?style=flat-square)](https://pypi.org/project/llm-gate/)

`llm-gate` is an alpha Python library and local OpenAI-compatible proxy. It
normalizes a task into a versioned `TaskSpec`, applies deterministic policy,
capability, privacy, budget, and availability gates, and explains the eligible
candidate set. Adaptive intelligence is advisory: it may rank eligible
candidates, but it cannot bypass a hard gate.

> **Status:** The deterministic contracts and availability adapter are usable
> now. The proxy and managed intelligence integration remain alpha slices. This
> repository does not claim production readiness, provider uptime, or a
> particular routing latency.

## Five-minute clean-environment quickstart

The fastest clean-room proof is the deterministic flagship demo. It requires no
credentials, makes no network calls, and produces stable JSON output.

From a fresh checkout:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python scripts/flagship_demo.py
python -m pytest -q tests/test_flagship_demo.py
```

For a packaging-style smoke check in an isolated environment, build a wheel and
install that artifact instead of using an editable checkout:

```bash
python -m pip install build
python -m build
python -m venv /tmp/llm-gate-smoke
source /tmp/llm-gate-smoke/bin/activate
pip install dist/llm_gate-*.whl
python -m llm_gate.cli --help
python /path/to/llm-gate/scripts/flagship_demo.py
```

See [the reproducible demo guide](docs/DEMO.md) for the clean-environment
verification flow, expected behavior, and current limitations.

## See the decision, without credentials

The flagship walkthrough is deterministic and makes no network calls:

```bash
python scripts/flagship_demo.py
```

It constructs a `TaskSpec`, evaluates four in-memory runtime observations,
selects one eligible candidate, and reports why the other three were excluded
(missing capability, exhausted quota, and unknown health). The output is stable
across runs.

## Install and use the library

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Compatibility routing remains available through `Gate`; it defaults to the
explicit development/degraded profile:

```python
from llm_gate import Gate

gate = Gate()
decision = gate.route("Rewrite the auth module", criticality="high")
print(decision.model)
print(decision.reason)
```

The `criticality` argument is a compatibility input, not the routing algorithm.
New integrations should use `TaskSpec` and `RoutingDecisionContract` (see
[contract migration](docs/contracts-migration.md)).

## Local proxy (alpha)

The proxy forwards to a configured upstream and is not a bundled model server.
Use a caller token or Unix socket for non-anonymous operation; keep anonymous
mode on loopback for development only:

```bash
export LLMGATE_AUTH_TOKEN='use-a-long-random-token'
export LLMGATE_HOST=127.0.0.1
export LLMGATE_UPSTREAM_BASE_URL=https://api.openai.com/v1
export OPENAI_API_KEY='set-this-in-your-shell-not-in-a-request'
llm-gate serve --host 127.0.0.1 --port 8000
```

```bash
LLMGATE_ALLOW_ANONYMOUS=true llm-gate serve --host 127.0.0.1 --port 8000
```

Anonymous mode is rejected on non-loopback addresses. The proxy owns upstream
configuration and credentials; client-supplied upstream URLs and credentials
are not accepted. Review [SECURITY.md](SECURITY.md) and the
[release acceptance matrix](docs/specs/RELEASE_ACCEPTANCE.md) before connecting
real provider credentials.

## What is implemented

- Versioned, strict contracts: `TaskSpec`, runtime candidates, availability
  snapshots, workflow plans, and explainable routing decisions.
- Protocol-based catalog/runtime adapter with deterministic normalization of
  healthy, degraded, unknown, denied, stale, quota, auth, and timeout states.
- Hard-gate candidate filtering for capabilities, provider/model policy, budget,
  concurrency, freshness, and protected work.
- Explain-only endpoint (`POST /v1/route`), model listing, health/readiness,
  request-size limits, redacted decision events, and transparent proxy fields.
- A deterministic local safety floor and an explicit production readiness check
  for the managed intelligence profile.

## What is not claimed yet

- No guarantee that a provider is available, affordable, fast, or high quality.
- No claim that the alpha proxy is a drop-in production gateway.
- No benchmark result is a service-level objective; benchmark methodology and
  result recording are documented in [BENCHMARKS.md](docs/BENCHMARKS.md).
- No automatic policy mutation from suggestions or learned signals.

## Routing model

```text
Request → TaskSpec → hard gates → eligible candidates → optional ranking
                                      │
                         explain exclusions and selection
```

Hard gates run before ranking. A catalog row is not proof of live eligibility;
runtime evidence is normalized with an explicit freshness window. Decisions are
intended to be deterministic for identical inputs, policy version, catalog
state, and learned-policy snapshot.

## CLI and integrations

| Command | Purpose |
|---|---|
| `llm-gate route <task>` | Compatibility route with explanation |
| `llm-gate serve` | Alpha OpenAI-compatible proxy |
| `llm-gate detect` | Inspect locally discoverable providers |
| `llm-gate stats` | Read local decision-log analytics |
| `llm-gate suggest` | Show read-only evidence-backed suggestions |

The proxy can be paired with OpenAI-compatible clients. See
[`docs/integrations/`](docs/integrations/) for client-specific notes; each
integration page should be read as compatibility guidance, not a production
certification.

## Development and verification

```bash
.venv/bin/python scripts/flagship_demo.py
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy llm_gate --strict
```

The CI workflow also runs package, security, and install smoke checks. See
[CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance.

## Security, privacy, retention, and supply-chain posture

Review [SECURITY.md](SECURITY.md) for vulnerability reporting, proxy security
controls, upstream URL restrictions, and data-handling defaults. Review
[docs/SECURITY_ASSURANCE.md](docs/SECURITY_ASSURANCE.md) for the published
threat model, privacy posture, retention responsibilities, and current
supply-chain evidence.

## Architecture

- `llm_gate/contracts.py` — strict versioned JSON-compatible contracts
- `llm_gate/availability.py` — runtime normalization and eligibility gates
- `llm_gate/intelligence.py` — deterministic floor and managed-adapter boundary
- `llm_gate/api.py` / `llm_gate/proxy.py` — alpha HTTP and upstream transport
- `scripts/flagship_demo.py` — credential-free public evidence fixture

## License

[MIT](LICENSE)
