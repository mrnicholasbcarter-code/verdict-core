# LinkedIn technical positioning and content plan

The content below is a **draft**, not a claim that the branch has shipped on `main`. Link the exact [golden-path certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) when publishing; its local run artifacts under `~/.verdict/evidence/golden-path/` are not publicly accessible from GitHub. Source: [Verdict Core](https://github.com/mrnicholasbcarter-code/verdict-core).

## Headline options

- AI Infrastructure Engineer | Verdict Core | Policy-gated AI execution and verifiable receipts
- Systems Engineer | Capacity-aware AI orchestration | Bounded failover and independent code review
- Python Engineer | Goal-to-receipt developer workflows | Failure classification and testable evidence

## About draft

I build AI execution systems that distinguish model availability from model eligibility and measured outcomes from a claimed success. On Verdict Core's `feat/interview-golden-path` branch, I worked on a goal-to-receipt workflow with frontier planning, concurrent isolated workers, capacity-aware route choice, same-node recovery, integration verification and an independent AI code review gate. Verdict keeps selection authority; OmniRoute supplies inventory and transport, and Prime Agent executes worker processes. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).

The branch's [fresh-clone certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) records **2907 passing tests**, lint and strict type checks, and a `certlive` run that reached `COMPLETE` with a passing review and integrity-linked receipt. Tagged live fault runs include quota exhaustion, auth errors, timeouts and 5xx; the exhausted-pool runs ended `BLOCKED`, not success. This is branch-local proof, not a production-uptime or merged-main CI claim.

## Featured links

1. [Repository: Verdict Core](https://github.com/mrnicholasbcarter-code/verdict-core) — source and issue history.
2. [Interview golden-path certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) — branch SHA, fresh-clone gates, live scenario matrix, and known exclusions. On LinkedIn, use the repository URL for this file.
3. [ADR-036: goal-to-receipt orchestration](../adr/ADR-036-goal-to-receipt-orchestration.md) — architecture and explicit limits. On LinkedIn, use the repository URL for this file.
4. [Interview golden-path guide](../guides/interview-golden-path.md) — prerequisites, CLI examples and evidence run IDs. On LinkedIn, use the repository URL for this file.

## Suggested post sequence

- **Eligibility before ranking:** explain why a model listed by a gateway is not proof that it is entitled, healthy, available or suitable. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).
- **Recovery without silent success:** contrast tagged `live9`/`live12` same-node route changes with `live7`/`live11` fail-closed receipts. [Certification scenario matrix](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).
- **Independent AI review after controller resume:** describe how restored implementer-route exclusions protect review independence, with `live10` as a recorded resumed run and `tests/test_orch_resume.py` as regression coverage. [Certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).
- **Boundaries of proof:** distinguish a credential-free quickstart fixture, a live branch demonstration and production operation. State explicitly that merged-main CI, adaptive concurrency and automatic review remediation are not certified. [Certification limits](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).

Do not cite performance, cost savings, adoption, production readiness or trading results without a separate public proof artifact.
