# Portfolio proof matrix

This is the recruiter, hiring-manager, and client-facing index for the
Verdict portfolio. It keeps the conversion story tied to evidence that can be
opened and reproduced.

| Audience | Proof asset | What it demonstrates | Boundary |
| --- | --- | --- | --- |
| Hiring manager | [Credential-free quickstart](../../README.md#run-the-credential-free-flagship-quickstart) | A deterministic local decision flow and explicit hard-gate exclusions | It is a fixture, not live provider execution |
| Platform engineer | [Capability passports](../CAPABILITY_PASSPORTS.md) and [qualification reports](../QUALIFICATION_REPORTS.md) | Exact route identity, provenance, expiry, redaction, and fail-closed admission | A report projects existing evidence; it does not create live evidence |
| Reliability/security reviewer | [Public evidence index](../proof/EVIDENCE_INDEX.md) | Traceability from claims to source, tests, schemas, and limitations | Release readiness remains partial until an exact tagged bundle exists |
| AI platform client | [AI Gateway Assurance Audit](AI_GATEWAY_ASSURANCE_AUDIT.md) | A concrete review scope, deliverables, inputs, exclusions, and handoff | Customer-specific provider findings require customer-authorized evidence |
| Systems/quant reviewer | [Kalshi case study](KALSHI_TRADING_BOTS_CASE_STUDY.md) | Architecture and risk-control narrative | Its quantitative figures are self-reported and not independently verified here |

## Approved positioning

> I build trustworthy decision systems under uncertainty—from AI execution
> assurance to capital and risk systems.

> Verdict tells you what an AI stack can actually do, what it is allowed to
> do, and what evidence proves what happened.

These lines describe the engineering thesis and the repository’s evidence
boundary. They do not claim adoption, production readiness, model quality, or
performance leadership.

## Before publishing a stronger claim

Run the proof validator and inspect the linked row in the claims ledger:

```bash
python scripts/verify_proof_matrix.py
```

Do not promote a claim from `unsupported`, `self_reported`, `aspiration`, or
`partial` until the ledger’s missing evidence is present, reproducible, dated,
and linked to the exact released artifact under discussion.
