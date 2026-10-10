# Repository metadata (for the maintainer to apply)

This file holds the GitHub repository settings that the docs refer to. A maintainer applies
them in **Settings → General** (About) and **Topics**. Docs changes do not change GitHub
settings.

## About line

Current (to replace): "Fail-closed LLM routing..."

New:

> Control plane for AI coding work: model admission, bounded recovery, a separate review route and verifiable run receipts.

Website: leave empty until a docs site exists.

## Topics

Add:

`ai-agents`, `coding-agents`, `control-plane`, `orchestration`, `llm`, `model-admission`,
`audit-trail`, `receipts`, `fail-closed`, `omniroute`, `prime-agent`, `python`, `cli`

Remove if present:

`gateway`, `middleware`, `cost-optimization`, `llm-routing`, `llm-gateway`

Reason: Verdict is a control plane for coding work (plan, admit, run, recover, review,
receipt). It is not an LLM gateway, and no cost saving is claimed
([`docs/benchmarks/README.md`](benchmarks/README.md)).

## Social preview

Do not upload a social preview made from fixture media. A 1280×640 image from the live hero
run's COMPLETE frame is planned for Train B (WS1 1.12).
