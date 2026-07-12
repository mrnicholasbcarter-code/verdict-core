#!/bin/bash
echo "=== RUNNING FUNCTIONAL TEST GRID ==="
pytest tests/test_cli_smoke.py || echo "GRID FAILURE DETECTED"
