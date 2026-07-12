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
