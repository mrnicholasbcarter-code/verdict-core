# Reconciliation Baseline — recorded 2026-08-03

Command: `PYTHONPATH=/home/nick/dev/verdict-core uv run pytest -q` (in /home/nick/dev/verdict-core)
Result: 1092 passed, 1 failed, 1 warning (run 09:2x)
Failure: tests/test_provider_receipts.py::test_provider_receipt_is_deterministic_and_hashes_inputs
  Root cause: NameError `_freeze` — defined at verdict/gateway_adapters.py:417 but the test
  imports only `from verdict.provider_receipts import ProviderReceipt, build_provider_receipt`.
  PRE-EXISTING, unrelated to #256-#261.
Note: a flaky time-dependent test causes 1090/1091/1092 run-to-run variation; the only stable
failure is the `_freeze` test.

Snapshot of the working tree (before reconciliation edits):
  /tmp/verdict-core-reconciliation-20260803-092541.tar.gz  (15.5MB, verified readable)
  Contains: verdict/, tests/, contracts/src/, docs/
