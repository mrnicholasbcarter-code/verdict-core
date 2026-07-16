# Security Policy

## Supported versions

Security fixes target the latest released version and the default branch.

## Reporting a vulnerability

Please do not open a public issue for sensitive vulnerabilities. Report concerns through GitHub private vulnerability reporting if enabled, or contact the maintainers privately with:

- Affected version or commit.
- Reproduction steps.
- Impact assessment.
- Any suggested mitigation.

## Scope

In scope:

- Unsafe request routing or provider failover behavior.
- Secrets exposure in configuration, logs, or CLI output.
- Dependency vulnerabilities.
- Incorrect criticality classification that could route sensitive production code or data to an unintended model/provider.

Out of scope:

- Provider outages, quota limits, or pricing changes.
- Prompt quality or model output quality issues.
- Vulnerabilities requiring compromised local developer machines.

## Maintainer response target

We aim to acknowledge reports within 3 business days and provide a remediation plan or status update within 10 business days.

## Proxy security defaults

llm-gate's proxy is secure by default. Production startup requires either
`LLMGATE_AUTH_TOKEN` (bearer authentication) or `LLMGATE_UNIX_SOCKET`. The
proxy does not accept caller-supplied upstream URLs or credentials.

### Local development

For a deliberately anonymous server, set `LLMGATE_ALLOW_ANONYMOUS=true` and
bind only to loopback:

```bash
LLMGATE_ALLOW_ANONYMOUS=true llm-gate serve --host 127.0.0.1 --port 8000
```

Anonymous mode is rejected on non-loopback addresses. It is not a production
configuration.

### Upstream SSRF policy

`LLMGATE_UPSTREAM_BASE_URL` accepts only `http` and `https` URLs with a
hostname, no userinfo, query, or fragment. Literal loopback/private/link-local
addresses are rejected unless explicitly listed in
`LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS` (the local development defaults are
`127.0.0.1`, `::1`, and `localhost`). Hostnames are resolved immediately before
transport use and fail closed if they resolve to private, loopback, or
link-local addresses. Redirects are disabled.

Only operators should set the private-host allowlist, and only for an
intentional local/private upstream. Never put an API key in a URL; use
`LLMGATE_UPSTREAM_API_KEY` or the provider's environment variable.

### Redaction and limits

Caller authorization, provider credentials, prompts, completions, and full task
text are not written to decision logs. Upstream configuration and exception
bodies are replaced with safe placeholders in readiness/errors. Request bodies
are bounded by `LLMGATE_MAX_REQUEST_BYTES`, and upstream calls have a timeout.

Run the security checks with:

```bash
.venv/bin/pytest -q tests/test_security.py
.venv/bin/ruff check llm_gate tests
.venv/bin/bandit -q -r llm_gate
```
