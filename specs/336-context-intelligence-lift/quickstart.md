# Quickstart: Context Intelligence Lift

The demo is **not** a fixture pack. It must retrieve planted docs/code/memory, compile a budgeted pack, and run the named check twice on a live cheaper identity.

## Prerequisites

- `verdict-core` with uv
- Reachable OpenAI-compatible gateway (default OmniRoute `http://localhost:20128/v1`)
- Credentials already configured on that gateway (do not put secrets in Verdict receipts)

## Live paired run (required for pass)

1. Plant a unique non-secret token in local docs, a small code marker, and durable memory under an isolated proof root.
2. Plan deterministic slices (ADRs/docs, matching code, memory query). Refuse repo dumps.
3. Retrieve units with provenance. Record omissions.
4. Fetch the live catalog; select cheaper-first (`local` then `free` then `cheaper`). Unknown context limit → blocked.
5. Compile a pack sized to that identity’s context limit. Keep the planted token. Fail closed if it cannot fit.
6. Execute the named check **unaided** (token not in the prompt).
7. Execute the named check **packed** on the **same** identity (pack as typed slots).
8. Checker: body must be exactly `{"lift_fact":"<token>"}`.
9. Receipt conclusion: `lift` / `no_lift` / `blocked`. No secrets.

If the gateway is down: **blocked**, not passed.

## Commands

After implementation, from this worktree:

```bash
uv run pytest -q tests/test_context_intelligence.py tests/test_context_lift.py
uv run pytest -q tests/test_context_lift_live.py
```

Live test must not treat skip/xfail as a golden pass. Unreachable gateway → blocked/skip with `live_surface_blocked`, not success.

## Must fail

- Reporting lift from fixtures or a swapped identity
- Dumping a whole repository into the pack
- Secrets or transcripts on pack/receipt
- Paid identity as lift subject while a cheaper unused qualified identity remains
- Claiming completeness when a source category was omitted
