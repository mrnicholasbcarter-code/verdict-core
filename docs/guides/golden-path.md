# Live routing golden path

Fetch a live OpenAI-compatible catalog (default OmniRoute `http://localhost:20128/v1`), classify from published specs and `/api/pricing`, select cheaper-first, and run the named check `{"golden_path":"ok"}` on a real identity.

Fixture catalogs cannot emit a pass receipt. If the gateway is down, the result is `live_surface_blocked`, not success.

```bash
uv run python -m pytest -q tests/test_live_routing_classify.py tests/test_live_routing_live.py
```

Usage probes read `~/.codex/auth.json` and `~/.claude/.credentials.json` when present. They never write those files and never put tokens on receipts. Cookie probes are a later phase (US6).
