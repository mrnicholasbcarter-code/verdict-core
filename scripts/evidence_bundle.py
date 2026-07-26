#!/usr/bin/env python3
"""
Collect and bundle evidence for release.
"""
import json
import os
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
BUNDLE_DIR = Path(__file__).parent.parent / "evidence_bundle"


def run_cmd(cmd: list, cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.stdout


def collect_evidence() -> dict[str, Any]:
    """Collect all evidence artifacts."""
    evidence = {"artifacts": []}

    # Collect artifact paths
    for root, _, files in os.walk(EVIDENCE_DIR):
        for f in files:
            if f.endswith((".json", ".md", ".txt", ".log", ".xml")):
                path = Path(root) / f
                rel_path = path.relative_to(EVIDENCE_DIR)
                evidence["artifacts"].append({
                    "path": str(rel_path),
                    "size": path.stat().st_size,
                    "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                })

    return evidence


def create_bundle(evidence: dict[str, Any]) -> Path:
    """Create tar.gz evidence bundle."""
    BUNDLE_DIR.mkdir(exist_ok=True)

    bundle_name = f"verdict-evidence-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.tar.gz"
    bundle_path = BUNDLE_DIR / bundle_name

    with tarfile.open(bundle_path, "w:gz") as tar:
        for artifact in evidence["artifacts"]:
            src = EVIDENCE_DIR / artifact["path"]
            if src.exists():
                tar.add(src, arcname=f"evidence/{artifact['path']}")

    return bundle_path


def main():
    print("📦 Collecting evidence...")
    evidence = collect_evidence()

    print(f"  Found {len(evidence['artifacts'])} artifacts")

    # Write manifest
    manifest_path = EVIDENCE_DIR / "evidence_manifest.json"
    manifest_path.write_text(json.dumps(evidence, indent=2))
    print(f"  Manifest written to {manifest_path}")

    print("📦 Creating bundle...")
    bundle_path = create_bundle(evidence)
    print(f"  Bundle created: {bundle_path}")
    print(f"  Size: {bundle_path.stat().st_size / 1024 / 1024:.2f} MB")

    # Verify bundle
    with tarfile.open(bundle_path, "r:gz") as tar:
        members = tar.getmembers()
        print(f"  Verified: {len(members)} files in bundle")

    print("✅ Evidence bundle complete!")


if __name__ == "__main__":
    main()
