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

`executor_gateway_origin` SHALL return `None` when the executor reports nothing. When a dispatch authority is set, a `None` report SHALL be BLOCKED with failure `dispatch_denied` / BLOCK / `"dispatch_denied: executor origin unknown"`: no permit is committed and the executor is not called. `PrimeHeadlessExecutor.gateway_origin_for(node_id, *, route_id, cwd)` SHALL prepare and pin a launch dir keyed by the `route_id` and `cwd` its `run()` will receive, and report `providers[provider].baseUrl` from that dir's private `models.json` copy, the exact file the launch's Prime reads. Every launch SHALL re-read the copy it is about to exec: a checked send launches only with its pinned dir, and that copy must still name the checked origin (a missing or changed pinned copy is a terminal `harness_configuration: executor origin changed` error with no exec); a send whose `route_id`/`cwd` was not checked, on an executor that has reported an origin, gets a fresh dir whose origin must match a checked origin or the same error; an executor with no origin check at all (planner, no dispatch authority) launches with a fresh dir as before. Re-pinning a send removes its previous dir, and when the runtime does not send after the check (origin, permit or admission denial), it SHALL call `release_executor_origin_pin` (forwarded by `MixedExecutor` and `FaultInjectingExecutor`) so the pinned dir is removed. It reports `None` when that copy, provider or URL is missing or unparseable. Executors that never touch the network (fake, offline demo) SHALL declare `LOCAL_EXECUTOR_ORIGIN` (`"local:"`), which matches any bound origin; no executor that sends to a gateway may declare it. `ScriptedExecutor` runs arbitrary code, so it reports no origin unless its constructor is given one (`gateway_origin=`).

#### Scenario: Unknown origin denies
- Proof: `tests/test_orch_gateway_origin.py` — a fake executor reporting no origin: zero calls, BLOCKED `executor origin unknown`, no permit.

#### Scenario: Prime origin comes from models.json
- Proof: `tests/test_orch_gateway_origin.py` — a matching `baseUrl` mints a permit and sends once; another host denies as a mismatch; a missing or unparseable `models.json`, a missing provider or a non-string `baseUrl` denies as unknown; rewriting the source `models.json` after the check leaves the launch on the pinned `baseUrl`; with a changed source, a launch on a cwd other than the checked one, or with a missing pinned dir, fails `executor origin changed` and never execs; a `baseUrl` of `local:` is unknown; three denials for the same send, or a permit denial after the check, leave no launch dir on disk.

#### Scenario: Local sentinel is allowed
- Proof: `tests/test_orch_gateway_origin.py` — a `LOCAL_EXECUTOR_ORIGIN` executor mints a permit and sends; a `ScriptedExecutor` without a declared origin is denied, with `gateway_origin=LOCAL_EXECUTOR_ORIGIN` it is allowed.

### Requirement: Gateway-origin normalization fails closed on a malformed URL

`normalize_gateway_origin(url)` SHALL return `scheme://host:port` with a lower-cased host and an explicit default port, dropping path/query/fragment, and SHALL return `None` for no scheme, no host, an invalid port, or a `urlsplit` `ValueError` (for example a malformed IPv6 literal such as `http://[::1`). `gateway_origin_matches(reported, bound)` SHALL be `True` only when both sides parse and are equal after normalization; a value that fails to parse matches nothing and is denied.

#### Scenario: Malformed origin denies
- Proof: `tests/test_orch_gateway_origin.py` — `:0`, `:99999`, and `http://[::1` cases all FAIL (deny), never send.

### Requirement: Wrapped executors resolve origin per node

`MixedExecutor.gateway_origin_for(node_id)` SHALL resolve the mapped executor's origin or the default executor's origin. `FaultInjectingExecutor.gateway_origin_for` SHALL delegate to its inner executor.

#### Scenario: Wrapper delegation
- Proof: `tests/test_mixed_executor.py`, `tests/test_orch_gateway_origin.py` wrapper cases.
