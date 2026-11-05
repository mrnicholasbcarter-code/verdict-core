# Quickstart explicit route authority

## Why

The fail-closed BOD-439 parent check is retained, but its child still sends a legacy objective/task_type envelope. The current `verdict/api.py` RouteRequest requires `task`, the serve path requires execution-path authority, and middleware requires server-owned bearer authentication. A clean environment is not evidence of an admitted route.

## What Changes

- Replace the embedded legacy payload with an explicit operator-owned JSON RouteRequest file named by `VERDICT_QUICKSTART_ROUTE_REQUEST`.
- Require a non-empty task and an execution_path_request object before probing. The API remains responsible for validating the full execution-path contract and applying admission and optimization.
- Forward the existing `LLMGATE_AUTH_TOKEN` as bearer authentication. Never generate a token, enable anonymous mode, or construct eligible candidates.
- Run TestClient lifespan so startup security checks and service initialization execute. Isolate probe HOME, VERDICT_HOME and receipts in the quickstart temporary directory.
- Preserve the existing parent failure predicates and CLI/core-test commands. Failed configuration and requests remain failures, without dumping input files or credentials.

This follow-up supersedes only the earlier BOD-439 promise to leave the probe payload and lifespan unchanged. API auth and admission policy remain unchanged.

## Scope and Evidence Boundary

Only quickstart, its regressions and this follow-up spec change. Offline integration tests exercise actual API middleware, request validation, IntelligenceService admission and optimizer using explicit test snapshots and fresh fixture passports with external network blocked. Fixture evidence is not production admission proof. Installed-PyPI acceptance, G7.1, operational readiness and whole-MVP completion remain open.
