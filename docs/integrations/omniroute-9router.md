# OmniRoute transport integration

`llm-gate` can consume OmniRoute's public model catalog and a bounded set of
documented runtime reads. OmniRoute remains the provider and credential plane;
`llm-gate` remains the deterministic policy, eligibility, capacity, and
explanation authority.

This integration is pinned to OmniRoute `release/v3.8.49` source commit
[`a5e5e88`](https://github.com/diegosouzapw/OmniRoute/tree/a5e5e880928886fe47362cdba63ca7c2c2cf55b4)
and its [API
reference](https://github.com/diegosouzapw/OmniRoute/blob/a5e5e880928886fe47362cdba63ca7c2c2cf55b4/docs/reference/API_REFERENCE.md).
It never reads OmniRoute's database, provider-account records, or other private
implementation state.

## Supported HTTP operations

`OmniRouteHTTPTransport` issues `GET` requests only:

| `llm-gate` source | OmniRoute operation | Default | Normalized evidence |
|---|---|---:|---|
| catalog | `/v1/models` | yes | Opaque model IDs, provider, and declared capabilities |
| health | `/api/monitoring/health` | yes | Provider health, breakers, lockouts, and learned headroom when present |
| rate limits | `/api/rate-limits` | no | Model lockouts when present; connection limiter counters are not treated as quota headroom |
| model cooldowns | `/api/resilience/model-cooldowns` | no | Temporary provider/model lockouts |
| budget | `/api/usage/budget?apiKeyId=…` | no | Remaining USD for one configured API-key ID |
| token limits | `/api/usage/token-limits?apiKeyId=…` | no | Most restrictive model/provider/global token headroom |

The catalog is discovery evidence, not readiness. Runtime records are minimized
into the public `RuntimeObservation` fields before they reach the availability
adapter. Unknown, missing, stale, unauthorized, malformed, and unavailable
evidence remains explicit and fails closed under the default policy.

## Minimal loopback configuration

Use environment variables or another secret manager; do not place credentials
in source code or URLs.

```python
import os

from llm_gate import (
    CandidateRequirements,
    OmniRouteAvailabilityAdapter,
    OmniRouteHTTPTransport,
)

transport = OmniRouteHTTPTransport(
    "http://127.0.0.1:20128/v1",
    api_key=os.environ.get("OMNIROUTE_API_KEY"),
    management_token=os.environ.get("OMNIROUTE_MANAGEMENT_TOKEN"),
    allow_private_hosts={"127.0.0.1"},
)

report = OmniRouteAvailabilityAdapter(transport).evaluate(
    CandidateRequirements(required=frozenset({"tools"}))
)
```

Every destination requires an exact `allow_private_hosts` entry despite the
parameter's legacy name. Plain HTTP is accepted only with a loopback IP literal,
which supports an explicitly allowed local service without a DNS-rebinding or
cleartext remote-credential path. Remote deployments must use HTTPS and a
separately reviewed access boundary.
Redirects and encoded responses are rejected, the timeout is bounded, raw
responses default to a 1 MiB limit, and parsed JSON has a fixed 64-container
nesting limit across supported Python versions.

`api_key` is sent only to `/v1/models`. `management_token` is separate and is
sent only to configured runtime reads. A deployment with OmniRoute API-key
enforcement disabled may omit `api_key`; protected management sources still
need an appropriate management credential.

## Opting into management evidence

Management reads are disabled unless named explicitly:

```python
transport = OmniRouteHTTPTransport(
    "http://127.0.0.1:20128/v1",
    api_key=os.environ.get("OMNIROUTE_API_KEY"),
    management_token=os.environ["OMNIROUTE_MANAGEMENT_TOKEN"],
    usage_api_key_id=os.environ["OMNIROUTE_USAGE_API_KEY_ID"],
    runtime_sources={
        "health",
        "rate_limits",
        "model_cooldowns",
        "budget",
        "token_limits",
    },
    allow_private_hosts={"127.0.0.1"},
)
```

`usage_api_key_id` is mandatory when `budget` or `token_limits` is enabled and
is sent as the documented query parameter to both reads. The release source
returns a scoped budget summary and a scoped token-limit list; `llm-gate`
retains only normalized remaining capacity, ignores disabled token limits, and
never exposes the raw payload. An explicit budget denial is a hard global
eligibility denial, even when the response also contains positive-looking
remaining capacity. Use a least-privilege management-scoped key where the
OmniRoute deployment supports one.

## Stable failure contract

The HTTP transport never includes response bodies, credentials, URL secrets,
or underlying exception objects in its exception graph:

| Upstream result | Public transport failure | Availability result |
|---|---|---|
| timeout | `OmniRouteTransportTimeout` | `timeout` |
| 401 or 403 | `OmniRouteTransportUnauthorized` | `unauthorized` |
| 404 | `OmniRouteTransportUnsupported` | unavailable operation |
| invalid or oversized JSON | `OmniRouteTransportMalformed` | `malformed` |
| other HTTP/network failure | `OmniRouteTransportError` | `unavailable` |

The aggregate health endpoint's top-level `status` describes the OmniRoute
service, not every catalog model. A model becomes ready only from matching
provider/model evidence; an otherwise unobserved model remains `unknown`.

## CLI, MCP, A2A, and 9router boundaries

Non-HTTP integrations use `CallableOmniRouteTransport` or
`MappingOmniRouteTransport`. Only operations with configured implementations
are advertised. This keeps CLI, MCP, A2A, and test transports behind the same
typed catalog/runtime contract without pretending that an installed binary or
an advertised method is usable.

9router is not assumed to share OmniRoute's ports, schema, authentication, or
runtime semantics. Connect it only through an explicit adapter that satisfies
the public transport contract and its conformance tests.

See the [routing policy](../specs/ROUTING_POLICY.md), [security
guide](../../SECURITY.md), and [gateway integration](gateways-proxies.md) for
eligibility semantics and the separate request-forwarding path.
