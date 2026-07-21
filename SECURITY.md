# Security Policy

## Supported versions

Security fixes target the latest released version and the default branch. The
project is alpha; deployment operators remain responsible for validating their
upstream, network boundary, provider policy, and retention controls.

## Reporting a vulnerability

Please do not open a public issue for sensitive vulnerabilities. Report concerns
through GitHub private vulnerability reporting if enabled, or contact the
maintainers privately with:

- Affected version or commit.
- Reproduction steps.
- Impact assessment.
- Any suggested mitigation.

## Scope

In scope:

- Unsafe request routing or provider failover behavior.
- Secrets exposure in configuration, logs, or CLI output.
- Dependency vulnerabilities.
- Incorrect task-policy or capability decisions that could route sensitive work
  to an unintended model/provider.
- SSRF, caller-authentication, request-size, and redaction regressions in the
  alpha proxy.

Out of scope:

- Provider outages, quota limits, or pricing changes.
- Prompt quality or model output quality issues.
- Vulnerabilities requiring compromised local developer machines.

## Proxy security defaults

The proxy is an alpha component, not a production security certification. Its
startup policy requires either `LLMGATE_AUTH_TOKEN` (bearer authentication) or
`LLMGATE_UNIX_SOCKET` unless anonymous development mode is explicitly enabled.
The proxy does not accept caller-supplied upstream URLs or credentials.

For a deliberately anonymous server, set `LLMGATE_ALLOW_ANONYMOUS=true` and bind
only to loopback:

```bash
LLMGATE_ALLOW_ANONYMOUS=true verdict serve --host 127.0.0.1 --port 8000
```

Anonymous mode is rejected on non-loopback addresses and is not a production
configuration. Review the [release acceptance matrix](docs/specs/RELEASE_ACCEPTANCE.md)
before using real provider credentials.

## Upstream and transport controls

`LLMGATE_UPSTREAM_BASE_URL` accepts only `http` and `https` URLs with a hostname,
no userinfo, query, or fragment. Literal loopback/private/link-local addresses
are rejected unless explicitly listed in `LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS`.
Hostnames are resolved immediately before transport use and fail closed if they
resolve to private, loopback, or link-local addresses. Redirects are disabled.

Only operators should set the private-host allowlist, and only for an intentional
local/private upstream. Never put an API key in a URL; use
`LLMGATE_UPSTREAM_API_KEY` or a provider environment variable.

## Data handling and retention

By default, decision events are append-only JSONL records containing routing
metadata and redacted candidate explanations. Caller authorization, provider
credentials, full prompts, completions, and full task text are not written to
decision logs. The default logger may still retain identifiers, model/provider
names, policy fields, and safe error categories.

Retention is an operator responsibility: `verdict` does not provide a hosted
retention service, automatic deletion schedule, encryption-at-rest guarantee, or
compliance certification. Choose a restrictive `log_path`, filesystem access
policy, rotation, backup, and deletion schedule appropriate to your data. Keep
`log_full_task` disabled unless the task text is known to be safe for the chosen
retention period. Inspect and scrub existing logs before sharing bug reports or
benchmark artifacts.

Request bodies are bounded by `LLMGATE_MAX_REQUEST_BYTES`, and upstream calls
have a timeout. Upstream providers may retain request data under their own
terms; configure provider-side retention separately.

The historical threat-model, privacy, retention, and supply-chain snapshot is
retained in [`docs/archive/SECURITY_ASSURANCE.md`](docs/archive/SECURITY_ASSURANCE.md).
Do not treat that archived snapshot as a current production certification.

Run the security checks with:

```bash
.venv/bin/pytest -q tests/test_security.py
.venv/bin/ruff check verdict tests
.venv/bin/bandit -q -r verdict
```
