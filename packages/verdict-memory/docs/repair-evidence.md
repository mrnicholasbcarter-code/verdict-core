# Transport repair evidence (local only)

Preserved base af0d16097b871a89e9c49ac80532aeb3dd307266. Existing eight commits retained.
Actual API contract captured using authenticated read-only tools/list; no backend writes.

Initial RED command: `timeout 60 .venv/bin/pytest -q tests/test_transport_contract.py`
Raw summary before implementation: `16 failed, 10 passed in 0.41s`.
RED raw host log: `/tmp/bod80-p-transport-red.txt`.
First green: `26 passed in 0.58s`. Extended transport proof: `43 passed in 0.50s`.
Full package before final proof-script tests: `71 passed in 12.24s`, includes wheel
build, isolated install outside checkout, bundled vector data and contract tests.

Original 9f1a residuals are asserted in test_transport_contract.py: persisted envelope,
wrong readback never mirror ACK, direct conflict only one write (including changed
kind), narrower-scope/tenant isolation, normalized delete identity/semantic ACK,
exact proof path and digest with finally cleanup. Tests use httpx.MockTransport
with actual 2.6.6 MCP result shapes, not invented server digest ACK fields.
Existing tests' assertions retained/strengthened; two old mock-only handlers now
use the same real-shape HTTP fixture because put requires pre/post scoped reads.

No merged-main, real harness pair, setup/doctor or hourly acceptance claim.
