# llm-gate

<p align="center"><b>Alpha policy and availability primitives for explainable LLM routing.</b></p>

[![CI](https://img.shields.io/github/actions/workflow/status/mrnicholasbcarter-code/llm-gate/ci.yml?style=flat-square&label=CI)](https://github.com/mrnicholasbcarter-code/llm-gate/actions)
[![PyPI](https://img.shields.io/pypi/v/llm-gate?style=flat-square)](https://pypi.org/project/llm-gate/)

`llm-gate` is an alpha Python library and local OpenAI-compatible proxy. It
normalizes a task into a versioned `TaskSpec`, applies deterministic policy,
capability, privacy, budget, and availability gates, and explains the eligible
candidate set. Adaptive intelligence is advisory: it may rank eligible
candidates, but it cannot bypass a hard gate.

> **Status:** The deterministic contracts and availability adapter are usable
> now. End-to-end workflow orchestration, live OmniRoute health/quota
> integration, legal retry/fallback, and managed intelligence remain unfinished
> release gates. The proxy is alpha. This repository does not claim production
> readiness, provider uptime, or a particular routing latency.

## OmniRoute availability adapter boundary

`OmniRouteAvailabilityAdapter` defines local JSON-like protocol aliases. These
names are `llm-gate` adapter operations, not a claim that OmniRoute exposes
same-named functions:

- catalog input: `catalog()` or `list_models()`;
- optional pre-fetched runtime input: `runtime()` or `get_runtime()`;
- optional pre-fetched capability input: `discover_capabilities()`.

A concrete integration may map OmniRoute's documented OpenAI-compatible
`GET /v1/models` response to the catalog input. This repository has not yet
identified a documented OmniRoute source for complete live health, quota,
price, or capability evidence. Those signals therefore remain `unknown` unless
an integration supplies explicit, versioned evidence from a documented
API/CLI/MCP/A2A surface. See the
[memory source index](docs/operations/MEMORY_SOURCE_INDEX.md) for the pinned
documentation observations and trust labels.

Use `StaticOmniRouteTransport` for fixtures, `CallableOmniRouteTransport` to
adapt explicit callables, or `MappingOmniRouteTransport` to adapt a mapping of
pre-fetched operation payloads. Capability discovery is allowlisted: only the
local operation aliases above are honored, and unknown advertised operations
are ignored rather than trusted.

Transport failures are surfaced as typed adapter errors
(`OmniRouteTransportUnsupported`, `OmniRouteTransportTimeout`,
`OmniRouteTransportMalformed`) and converted into failure-isolated availability
reports instead of leaking transport-specific behavior into routing policy.

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
[CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance. Maintainers
continuing the cross-repository release effort should use the
[portfolio continuation runbook](docs/operations/PORTFOLIO_CONTINUATION_RUNBOOK.md),
[ecosystem product vision](docs/operations/ECOSYSTEM_PRODUCT_VISION.md), and
[memory source index](docs/operations/MEMORY_SOURCE_INDEX.md).

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

## Dynamic availability probes

`llm-gate` discovers opaque model IDs from the configured OpenAI-compatible
`/v1/models` catalog. Discovery alone leaves candidates in `unknown` state.
For a bounded usage/availability check, supply the discovered IDs to
`ProbeRunner` and inject an OpenAI-compatible transport:

```python
from llm_gate import ProbePolicy, ProbeRunner, openai_probe_transport

transport = openai_probe_transport("http://127.0.0.1:20128/v1")
observations = ProbeRunner(ProbePolicy(max_models_per_run=8)).run(
    discovered_model_ids,
    transport,
)
```

Each probe sends only a fixed `Return exactly: OK` prompt with `max_tokens=1`,
no tools, no user/project data, and a bounded timeout. Results record only
status, latency, usage counters, redacted error class/message, cooldown, and
quarantine state. A successful HTTP response is `ready` only when it includes
both positive token usage and non-empty assistant output; an empty or
usage-free response remains `degraded`. The runner enforces per-run
count/concurrency bounds, exponential cooldown, and quarantine after repeated
failures. Use the resulting `ProbeObservation.as_runtime_observation()` with
the availability adapter; do not treat vector/RAG memory as live health
authority.

Candidate selection fails closed by default: only `ready` observations are
selectable. Non-protected callers may explicitly set
`CandidateRequirements(allow_degraded=True)` to admit fresh `degraded`
observations; protected work still rejects them. The separate
`unknown_is_eligible` opt-in admits only fresh, internally consistent
`unknown` evidence—never missing, stale, malformed, or contradictory data.
