# Hydrated dispatch packets

## Why

The runtime packs native context but the standalone queen builder emits incomplete Markdown. A complete supplied queen packet must reach the actual worker input without becoming route authority.

## What changes

- Add `WorkNode.hydration_packet`, a JSON string using `verdict.hydration/v1`.
- Bind packet content to the exact dispatch repository SHA and complete node contract excluding the packet itself.
- Require current cited sections for Linear, memory, code, instructions, specs, PRs and design review; allow explicitly justified empty memory/spec/PR results. Require explicit stop conditions, no unresolved blockers and declared approved independent-family design review.
- Reject malformed, duplicate-key, credential-shaped, placeholder, missing and stale inputs locally without provider cooling.
- Include validated evidence as mandatory text before native context budgeting. Reject full-input overflow rather than dropping evidence; record packet/rendered digests and structural status.
- Production orchestration requires packets. Library callers remain compatible when packets are absent, but supplied packets always validate.
- Provide a repository-owned command to bind supplied evidence to an existing graph using the runtime validator. The global hydration helper delegates to it rather than duplicating validation.

## Boundaries and limitations

Source revisions and declared review families are observations, not authenticated freshness or review attestations. No new admission, spend, recovery, model or health policy. No new memory backend or optimization loop. Binding requires a final node contract; ownership or dependency-base changes require rehydration. This increment does not complete every retained acceptance obligation of the context story.

## Verification

Use the project Python environment and isolated HOME/VERDICT_HOME. Run focused hydration, CLI, planner and resume tests, then ruff check/format, strict mypy, doc links, reachability, proof matrix and full suite under AGENTS.md. Real provider and merged-main acceptance remain separate mandatory proof, not fixture results.

## Impact

Python orchestration contracts/runtime/planner/CLI and hydration module; focused pytest tests; hydration guide and global skill adapter. No dependencies, vendor or gate changes.
