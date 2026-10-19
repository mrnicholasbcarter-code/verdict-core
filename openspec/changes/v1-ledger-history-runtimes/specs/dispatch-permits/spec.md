# Spec Delta

## Purpose

Adds executor gateway-origin attestation to permit commit: the dispatch path denies when the executor reports a gateway origin that does not match the bound dispatch scope, or reports none.

## ADDED Requirements

### Requirement: Dispatch denies on an executor-reported gateway-origin mismatch

Immediately before `_commit_permit`, the dispatch path SHALL compare the executor's reported gateway origin (`executor_gateway_origin(executor, node_id)`) against the bound `DispatchScope.gateway_origin`, both normalized (`normalize_gateway_origin`). When the executor reports a non-`None` origin that does not match, the node SHALL be BLOCKED with failure `dispatch_denied` / BLOCK / `"dispatch_denied: executor origin mismatch"`. No permit SHALL be committed and the executor SHALL NOT be called; there is no reselect or retry for this failure.

#### Scenario: Mismatch denies dispatch
- Proof: `tests/test_orch_gateway_origin.py` — other origin: zero sends, BLOCKED, one `dispatch_denied` failure, no permit for the attempt.

#### Scenario: Equivalent origin forms match
- Proof: same origin with a plain URL, a trailing slash, `/v1`, or an upper-case host plus explicit `:80` all mint a permit and send once.

### Requirement: An executor reporting no gateway origin is denied (U1)

`executor_gateway_origin` SHALL return `None` when the executor reports nothing. When a dispatch authority is set, a `None` report SHALL be BLOCKED with failure `dispatch_denied` / BLOCK / `"dispatch_denied: executor origin unknown"`: no permit is committed and the executor is not called. `PrimeHeadlessExecutor` SHALL report its provider's `baseUrl` from the `models.json` of the Prime agent dir its launch mirrors (`default_prime_agent_dir(child_env)/models.json`, `providers[provider].baseUrl`), and `None` when that file, provider or URL is missing or unparseable. Executors that never touch the network (scripted, fake, offline demo) SHALL declare `LOCAL_EXECUTOR_ORIGIN` (`"local:"`), which matches any bound origin; no executor that sends to a gateway may declare it.

#### Scenario: Unknown origin denies
- Proof: `tests/test_orch_gateway_origin.py` — a fake executor reporting no origin: zero calls, BLOCKED `executor origin unknown`, no permit.

#### Scenario: Prime origin comes from models.json
- Proof: `tests/test_orch_gateway_origin.py` — a matching `baseUrl` mints a permit and sends once; another host denies as a mismatch; a missing or unparseable `models.json`, a missing provider or a non-string `baseUrl` denies as unknown.

#### Scenario: Local sentinel is allowed
- Proof: `tests/test_orch_gateway_origin.py` — a `LOCAL_EXECUTOR_ORIGIN` executor mints a permit and sends; `ScriptedExecutor` declares it.

### Requirement: Gateway-origin normalization fails closed on a malformed URL

`normalize_gateway_origin(url)` SHALL return `scheme://host:port` with a lower-cased host and an explicit default port, dropping path/query/fragment, and SHALL return `None` for no scheme, no host, an invalid port, or a `urlsplit` `ValueError` (for example a malformed IPv6 literal such as `http://[::1`). `gateway_origin_matches(reported, bound)` SHALL be `True` only when both sides parse and are equal after normalization; a value that fails to parse matches nothing and is denied.

#### Scenario: Malformed origin denies
- Proof: `tests/test_orch_gateway_origin.py` — `:0`, `:99999`, and `http://[::1` cases all FAIL (deny), never send.

### Requirement: Wrapped executors resolve origin per node

`MixedExecutor.gateway_origin_for(node_id)` SHALL resolve the mapped executor's origin or the default executor's origin. `FaultInjectingExecutor.gateway_origin_for` SHALL delegate to its inner executor.

#### Scenario: Wrapper delegation
- Proof: `tests/test_mixed_executor.py`, `tests/test_orch_gateway_origin.py` wrapper cases.
