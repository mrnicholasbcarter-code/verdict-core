"""The bundled runtime schema must never silently drift from the design copy.

`verdict/release/evidence.py` validates against a schema copy bundled inside
the package (specs/ is excluded from Docker builds via .dockerignore, so the
design copy under specs/277-security-privacy-gate/contracts/ is not present
at runtime). This test is the only thing keeping the two copies in sync.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_SCHEMA = REPO_ROOT / "verdict" / "release" / "schemas" / "launch_gate_evidence.schema.json"
DESIGN_SCHEMA = (
    REPO_ROOT
    / "specs"
    / "277-security-privacy-gate"
    / "contracts"
    / "launch-gate-evidence.schema.json"
)


def test_bundled_schema_matches_design_schema() -> None:
    assert json.loads(BUNDLED_SCHEMA.read_text()) == json.loads(DESIGN_SCHEMA.read_text())
