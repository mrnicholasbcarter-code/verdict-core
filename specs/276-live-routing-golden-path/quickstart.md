# Quickstart: Live Routing Golden Path

The demo is **not** a fixture catalog. It must fetch a live gateway/provider listing and run the named check on the selected identity.

## Prerequisites

- `verdict-core` with uv
- Reachable OpenAI-compatible gateway (default OmniRoute `http://localhost:20128/v1`) that serves `/models` and chat completions
- Credentials already configured on that gateway (do not put secrets in Verdict receipts)

## Live run (required for pass)

1. `GET {gateway}/models` (and pricing facts if the gateway exposes them separately). Capture time starts now.
2. Drop `auto/*`, unexpanded aliases, opaque mixes.
3. Classify only from fetched specs. Missing cost/context/tools/modalities → unclassified → drop. Do not infer from names.
4. Probe a bounded sample for health. Unknown/unhealthy is not kept.
5. Select cheaper-first among kept concrete identities.
6. Execute the named check: the selected identity must return only `{"golden_path":"ok"}`. The checker parses JSON and requires `golden_path == "ok"`.
7. Fail over cheaper-first, then paid, unique identities only, until checker pass or exhaustion.
8. Write explanation + receipt naming endpoint, identity, cheaper-vs-paid, attempts, checker result.

If the gateway is down: **blocked**, not passed.

## Commands

After implementation, from `verdict-core`:

```bash
uv run pytest -q tests/test_golden_path_classify.py   # fixtures OK here
uv run pytest -q tests/test_golden_path_live.py       # requires live gateway; skip/xfail is not a pass
```

Live test must fail the job when the gateway is unreachable unless explicitly marked blocked (not success).

## Must fail

- Reporting success from a committed fixture catalog
- Paid selected while a cheaper kept live identity exists
- Name-heuristic qualification
- Opaque mix selected
- Checker failure reported as success
