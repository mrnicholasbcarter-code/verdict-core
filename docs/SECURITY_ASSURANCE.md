# Security assurance evidence

Evidence snapshot for issue #68, Slice 35.3.

`llm-gate` is still an alpha library and local proxy. This document publishes the current threat model, privacy and retention posture, and supply-chain evidence so operators and reviewers can evaluate the project using explicit claims instead of implied guarantees.

## Scope and status

This is an evidence document, not a certification.

What it covers now:

- threat model assumptions and primary abuse cases;
- privacy and retention defaults that are implemented or documented;
- current supply-chain controls visible in source control and CI.

What it does **not** claim:

- hosted service controls;
- formal compliance certification;
- production-readiness of every proxy or intelligence slice;
- zero vulnerability status for all transitive dependencies at all times.

Primary source documents:

- [`README.md`](../README.md)
- [`SECURITY.md`](../SECURITY.md)
- [`docs/specs/RELEASE_ACCEPTANCE.md`](specs/RELEASE_ACCEPTANCE.md)
- [`docs/specs/ENFORCEMENT_AND_LEARNING.md`](specs/ENFORCEMENT_AND_LEARNING.md)
- [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
- [`.github/workflows/codeql.yml`](../.github/workflows/codeql.yml)
- [`pyproject.toml`](../pyproject.toml)

## 1. Threat model

### 1.1 Assets

The main assets in scope are:

- task text and prompts submitted for routing;
- provider credentials and operator-owned authentication tokens;
- routing policy, model catalog, and availability evidence;
- decision logs and privacy-safe learning/outcome records;
- local proxy availability and integrity.

### 1.2 Trust boundaries

`llm-gate` operates across these boundaries:

1. **Caller to local proxy or CLI.** Untrusted input may contain secrets, oversized bodies, malformed JSON, or attempts to influence routing.
2. **Proxy to upstream provider.** Upstream destinations and credentials must remain operator-controlled rather than caller-controlled.
3. **Deterministic policy to adaptive intelligence.** Adaptive ranking may advise but must not override hard safety gates.
4. **Runtime state to local logs/events.** Observability must avoid leaking full prompts or credentials by default.
5. **Repository to build/install environment.** Dependency and automation changes can alter shipped behavior even when core Python source is unchanged.

### 1.3 Security assumptions

This repository currently assumes:

- operators control the host filesystem, process environment, and network egress rules;
- operators decide whether to run with loopback-only anonymous development mode or authenticated mode;
- upstream providers enforce their own account, storage, and retention policies outside this repository;
- development-mode degraded behavior is not treated as production-ready.

### 1.4 Primary threats and current mitigations

| Threat | Why it matters | Current mitigation evidence |
|---|---|---|
| Sensitive work routed to the wrong model/provider | Could violate privacy, policy, or capability expectations | README states deterministic policy, privacy, budget, and availability gates run before ranking; release acceptance requires adaptive suggestions to be unable to bypass denial, privacy, capability, or protected-task gates. |
| Prompt or credential leakage through logs | Logs are often copied into bug reports, CI artifacts, or benchmarks | `SECURITY.md` states decision logs omit provider credentials, full prompts, completions, and full task text by default; `ENFORCEMENT_AND_LEARNING.md` requires privacy-safe outcome records with redacted summaries and no raw prompts by default. |
| SSRF or caller-selected upstream exfiltration | A malicious caller could force requests to internal hosts or attacker infrastructure | `SECURITY.md` documents URL validation, hostname requirements, rejection of userinfo/query/fragment, private-host blocking by default, fresh DNS resolution checks, and disabled redirects. |
| Unauthorized local proxy use | A local or network-adjacent caller could send requests through operator credentials | `SECURITY.md` documents startup requirements for bearer auth or Unix socket unless explicit loopback-only anonymous development mode is enabled. |
| Request-size or timeout exhaustion | Large bodies or hanging upstreams can affect availability | `SECURITY.md` documents bounded request size via `LLMGATE_MAX_REQUEST_BYTES` and upstream timeouts. |
| Supply-chain compromise through dependencies or CI changes | Compromised packages or automation could alter runtime behavior or leak secrets | CI includes Bandit, pip-audit, and CodeQL; dependencies are declared in `pyproject.toml`; release claims remain alpha and evidence-based rather than implying hardening not yet proven. |

### 1.5 Threats explicitly not solved by this repository alone

These remain operator or upstream responsibilities:

- disk encryption, backup retention, and secure log transport;
- endpoint protection and compromise of the local machine running `llm-gate`;
- provider-side data retention and training policies;
- network segmentation, firewalling, TLS termination policy, and reverse-proxy hardening;
- secret rotation, vaulting, and enterprise identity controls.

## 2. Privacy posture

### 2.1 Default privacy stance

The current public posture is privacy-minimizing rather than privacy-eliminating.

Implemented and documented defaults include:

- deterministic safety gates run before any adaptive ranking;
- decision logs omit full prompts, completions, caller authorization, and provider credentials by default;
- privacy-safe learning/outcome episodes are designed to store redacted previews, stable fingerprints, and summary metadata rather than raw prompt/context blobs;
- privacy opt-out is part of the release acceptance criteria for adaptive learning data.

Relevant evidence:

- `README.md` describes privacy gates as a first-class routing input.
- `SECURITY.md` documents redacted decision logging defaults and operator retention responsibility.
- `docs/specs/ENFORCEMENT_AND_LEARNING.md` defines privacy-safe outcome episodes and states raw prompts, credential material, and full context blobs must not be persisted by default.
- `docs/specs/RELEASE_ACCEPTANCE.md` requires privacy opt-out and redaction behavior before release.

### 2.2 Privacy limitations

Current limitations that should be read literally:

- this repository is not a hosted privacy boundary;
- upstream providers may retain submitted request data under their own terms;
- operators can still choose unsafe logging or storage practices, including enabling fuller task logging;
- alpha status means the implemented proxy and intelligence slices should be reviewed before handling regulated or highly sensitive workloads.

## 3. Retention posture

Retention is currently **operator-managed**.

The repository publishes these retention-relevant facts:

- `llm-gate` writes append-only JSONL decision events by default when configured to log;
- default decision logs exclude full prompts, completions, and provider credentials;
- there is no built-in hosted retention service, automatic deletion scheduler, or compliance guarantee;
- operators must choose log path, filesystem permissions, rotation, backup policy, and deletion schedule;
- `log_full_task` should remain disabled unless the task text is known to be safe for the chosen retention window;
- upstream provider retention must be configured separately with the provider.

This means retention evidence is currently a combination of:

1. **product defaults** that reduce stored sensitive content; and
2. **explicit documentation** that deletion and storage controls are not outsourced to the project.

## 4. Supply-chain evidence

### 4.1 Declared dependencies

Runtime and development dependencies are declared in [`pyproject.toml`](../pyproject.toml), including a small core runtime set and optional extras for server and UI surfaces. This makes the package surface auditable from source control rather than depending on undocumented bootstrap scripts.

### 4.2 CI security checks

The repository currently publishes automated supply-chain and static-analysis evidence in GitHub Actions:

- **Bandit** runs against `llm_gate` in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
- **pip-audit** runs in the same workflow to inspect installed dependency vulnerabilities.
- **CodeQL** runs on push, pull request, and a weekly schedule in [`.github/workflows/codeql.yml`](../.github/workflows/codeql.yml).
- standard test, lint, and strict mypy checks run alongside security checks, reducing the chance that packaging or refactors silently break security-sensitive paths.

### 4.3 Contributor-side controls

The repository also publishes:

- a `.pre-commit-config.yaml` with local lint/security hooks, including Bandit;
- contribution instructions in [`CONTRIBUTING.md`](../CONTRIBUTING.md) requiring tests, Ruff, and strict mypy before PRs;
- a public `SECURITY.md` describing how to report vulnerabilities and which classes are in scope.

### 4.4 Current supply-chain gaps

The current evidence set is useful but incomplete. As of this slice, the repository does **not** yet publish all of the following in-tree artifacts:

- a committed SBOM;
- signed release provenance or SLSA-style attestations;
- hash-pinned transitive lockfiles for every supported installation path;
- a formal third-party dependency review inventory.

These are gaps, not hidden features.

## 5. How to verify the published evidence

From a clean project environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,server]'
.venv/bin/python -m pytest -q tests/test_security.py
.venv/bin/python -m ruff check llm_gate tests
.venv/bin/python -m bandit -q -r llm_gate
```

To review the CI configuration directly, inspect:

- `.github/workflows/ci.yml`
- `.github/workflows/codeql.yml`
- `pyproject.toml`
- `SECURITY.md`
- `docs/specs/RELEASE_ACCEPTANCE.md`

## 6. Reviewer checklist

A reviewer validating this slice should be able to answer yes to the following:

- Is there a published threat model with assets, boundaries, assumptions, and key threats?
- Are privacy defaults and limitations described without claiming certification?
- Is retention responsibility clearly assigned to operators and upstream providers where appropriate?
- Is there public evidence of supply-chain scanning or analysis in CI?
- Are missing artifacts, such as SBOM/provenance attestations, disclosed as gaps rather than implied complete?

## 7. Change policy

Update this document whenever any of the following changes:

- logging defaults or redaction behavior;
- proxy authentication or upstream URL validation behavior;
- CI security workflow contents;
- dependency management model;
- release posture for privacy, retention, or managed intelligence.
