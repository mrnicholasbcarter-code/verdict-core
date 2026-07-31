# AI Gateway Assurance Audit

## A practical review of what an AI route can do, may do, and can prove

Verdict is built for teams that have multiple gateways, providers, model
aliases, tools, and fallback paths but cannot confidently answer whether a
route is actually eligible for a task. The AI Gateway Assurance Audit turns
that uncertainty into a scoped engineering report.

This is an engineering review of a configured route set. It is not a promise
that a provider is available, that a model is intelligent, or that a system is
production-ready.

## What the audit covers

1. **Inventory and identity** — enumerate configured gateways, providers,
   connections, endpoints, protocols, model revisions, aliases, and fallback
   edges without collapsing distinct executable routes.
2. **Capability qualification** — review claimed metadata separately from
   direct observations; run only consented, bounded protocol, schema, and tool
   checks against approved test routes.
3. **Policy and failover behavior** — inspect hard capability predicates,
   freshness, quota/cooldown handling, fallback legality, and whether advisory
   ranking can reintroduce an excluded candidate.
4. **Evidence and privacy** — verify that decisions, execution outcomes, and
   qualification reports are scoped, redacted, integrity-linked, and useful to
   an operator without retaining prompts or response bodies.
5. **Remediation plan** — return prioritized gaps, an owner-ready backlog,
   requalification triggers, and a release-evidence checklist.

## Inputs

- A route and provider inventory, or a read-only export of the configured
  gateway catalog.
- The task capabilities and risk classes that must be hard requirements.
- Optional opt-in access to a test environment for bounded probes. Live calls
  are never implied by catalog membership or HTTP success.
- Existing policy, CI, incident, and evidence artifacts that the customer
  wants reviewed.

Credentials and raw prompts are not required for the offline contract review.
If live probes are authorized, credentials remain in the customer-controlled
environment and the public report contains only sanitized diagnostics and
digests.

## Deliverables

- A route identity and capability matrix showing `supported`, `unsupported`,
  and `unknown` states with provenance and expiry.
- A redacted qualification report explaining why each requested capability is
  admitted or rejected.
- A policy/failover findings register with severity, evidence reference,
  reproduction step, remediation, and residual limitation.
- A release-readiness checklist that distinguishes local contract evidence,
  observed provider evidence, and evidence that is still missing.
- A 30-minute handoff walkthrough for the engineering owner.

## Typical timeline

The offline review is designed as a focused one-week engineering engagement:

- **Day 1:** scope, threat model, route inventory, and success criteria.
- **Days 2–3:** contract, identity, policy, and evidence review.
- **Days 3–4:** optional consented qualification fixtures or bounded test-route
  probes.
- **Day 5:** report, remediation backlog, and handoff.

The timeline is a planning target, not a service-level guarantee; provider
access, route count, and review scope determine the actual effort.

## Explicit exclusions

This audit does not certify provider uptime, model quality, security of a
provider’s infrastructure, regulatory compliance, production readiness,
adoption, performance leadership, or a specific business outcome. It does not
publish credentials, private endpoint details, raw prompts, response bodies,
or customer data. Any quantitative claim must have a dated, reproducible
artifact with a defined metric, baseline, environment, and raw-result boundary.

## Evidence boundary

The public Verdict repository demonstrates deterministic local contracts for
eligibility, passports, protocol qualification, structured output, tools,
receipts, and credential-free quickstart behavior. Those contracts are not a
substitute for customer-specific live evidence. The [public proof
matrix](../proof/EVIDENCE_INDEX.md) and [claims ledger](../proof/claims_ledger.v1.json)
show what is verified, observed, partial, or explicitly not approved.

## Contact and next step

Open a scoped request in the repository’s [GitHub issue
tracker](https://github.com/mrnicholasbcarter-code/verdict-core/issues) with
the route count, required capabilities, environment constraints, and desired
deadline. Do not include credentials, raw prompts, customer data, or private
endpoint URLs in the issue.
