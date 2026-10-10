# Support Policy

## Supported versions

The current development series is **0.5.x** (see [pyproject.toml](pyproject.toml)).
Security fixes target the latest released version and the default branch, as
stated in [SECURITY.md](SECURITY.md). Older releases do not have a promised
maintenance window. Upgrade to the latest release before reporting a bug when
practical.

Verdict is in active development. A supported version is not a production
certification or a guarantee of provider availability. Operators must validate
their gateway, harness, network boundary and data handling for their deployment.

## Get help

- Start with the [README](README.md), [getting-started guide](docs/GETTING_STARTED.md)
  and [configuration reference](docs/CONFIGURATION.md).
- Report reproducible bugs using the [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml).
- Propose changes using the [feature request template](.github/ISSUE_TEMPLATE/feature_request.yml).
- Use [GitHub issues](https://github.com/mrnicholasbcarter-code/verdict-core/issues)
  for public questions that the documentation does not answer.

Include the Verdict version or commit, operating system, exact command,
expected result, actual result and a small reproduction. A sanitized
`verdict doctor` report can help. Inspect it before posting. Do not attach API
keys, bearer tokens, private repository content or unsanitized run artifacts.
Provider outages, account access and quota disputes belong with the provider
or gateway operator; Verdict cannot change those limits.

## Security and privacy reports

**Do not report sensitive vulnerabilities in public issues.** Follow
[SECURITY.md](SECURITY.md) for private vulnerability reporting and the required
report details. Privacy concerns use the same private channel; see
[PRIVACY_POLICY.md](PRIVACY_POLICY.md).

## Release and response expectations

Releases and responses are best effort. This project does not promise a fixed
release cadence, response-time SLA, scheduled maintenance window or commercial
support service. See [CHANGELOG.md](CHANGELOG.md) for released changes and
[VERSIONING.md](VERSIONING.md) for the versioning policy.
