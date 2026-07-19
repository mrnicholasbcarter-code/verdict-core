# Release Acceptance Checklist

This document details the mandatory QA criteria and release gates that the `llm-gate` framework and all adjacent repositories must fulfill before any deployment or semantic release.

---

## 1. Static Quality & Test Gates

* **Zero Warning Lints**: Run `ruff check` on Python code. Must exit code `0` with no warnings.
* **Formatting Invariant**: Run `ruff format --check`. Zero formatting regressions.
* **Strict Type Safety**: Run `mypy --strict` or target TypeScript `tsc --noEmit`. No implicitly typed variables or unresolved imports.
* **Unit Test Coverage**: Run `pytest` or Jest tests. Minimum test coverage thresholds:
  * `llm-gate`: 100% core routing modules.
  * `trade-risk-engine`: 100% precision drift and time gate modules.
  * `llm-gate-node`: complete SSE proxy and AbortController listener.

---

## 2. API Proxy & Parity Boundaries

* **Streaming Completeness**: Proxy endpoint `/v1/chat/completions` streaming must preserve chunk headers, payload details, token usage, and byte boundaries without middleware buffering.
* **Status Integrity**: Health routes `/health` and `/ready` must provide clean JSON structures representing remote catalog connections and local availability.
* **Header Transparency**: TS forwarding node middleware must declare `X-Accel-Buffering: no` to avoid response chunk buffering.

---

## 3. Security & Secrets Scanning

* **Pre-commit Gating**: Secret checkers (`trufflehog` or `gitleaks`) must scan files dynamically before commits.
* **Provider Keys**: API credential mapping must not log raw strings to logs or exports.
