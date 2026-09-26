# OmniRoute workers

OmniRoute is Verdict's gateway boundary for model inventory and model
execution. It is not the orchestration runtime. The current goal-to-receipt
runtime is [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).

## Catalog-backed worker requests

`OmniRouteWorkerClient` fetches `GET /v1/models`, retains a small safe subset
of each catalog row, and sends OpenAI-compatible chat-completions requests to
the same configured base URL. It does not embed a provider key or a provider
allowlist.

```python
from verdict import OmniRouteWorkerClient, WorkerPool, WorkerRequest

client = OmniRouteWorkerClient(
    "http://127.0.0.1:20128/v1",
    max_attempts=3,
)
pool = WorkerPool(client, max_concurrency=4)
results = await pool.run([
    WorkerRequest(
        task_id="explore",
        model="auto/best-free",
        messages=[{"role": "user", "content": "Inspect the target module."}],
    ),
])
```

An explicit model must be present in the fetched catalog. For an `auto/*`
request, the client tries supported catalog aliases and then other advertised
free or `auto/*` models with the requested capabilities. `WorkerPool` limits
concurrency, preserves input order, and returns a redacted error for a failed
item without cancelling unrelated items.

Only transient request failures receive bounded retries: connection failures,
timeouts, HTTP 408, 409, 425, 429, and 5xx responses. Authentication and
malformed-request responses do not retry.

## ADR-036 worker selection and Prime dispatch

The ADR-036 path uses a stricter execution boundary than the general worker
client. Before a node launch, `EligibilityLadder` evaluates live inventory,
provider connection evidence, probe health, cooldowns, task requirements, and
current load. A route must also be visible in Prime's OmniRoute model registry
when that registry is present. The selected concrete route is passed to
`PrimeHeadlessExecutor`, which invokes `prime-agent` with `--model <route>`.

The lower-level `verdict.subagent_selection` and `verdict.worker_runtime`
modules support bounded Prime/RLM worker attempts. They select only the
intersection of live inventory and Prime-visible selectors, cache health
results, validate a child terminal result, and record failure categories before
trying another eligible candidate. They do not make OmniRoute a source of
orchestration policy.

Use the orchestration CLI to see or run this path:

```bash
verdict eligibility --probe --frontier
verdict orchestrate "<goal>" --repo /path/to/repository --max-parallel 3
```

See [orchestration golden path](orchestration-golden-path.md) for the complete
controller, worker, review, and receipt flow.

## Codex configuration

The local Codex provider can use OmniRoute's OpenAI-compatible endpoint:

```toml
model = "cx/gpt-5.6-luna-xhigh"
model_provider = "omniroute"

[model_providers.omniroute]
provider = "openai"
base_url = "http://127.0.0.1:20128/v1"
wire_api = "responses"
```

Model IDs in external harness configuration must be backed by that harness and
by the live gateway catalog. Re-run the harness's own configuration validation
after changing its model configuration.
