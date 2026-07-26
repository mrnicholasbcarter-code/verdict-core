#!/usr/bin/env python3
"""
Generate evidence bundle for Verdict flagship release.
Collects all acceptance gate evidence into a single artifact.
"""

import json
import os
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any


EVIDENCE_DIR = Path(__file__).parent.parent / "evidence_bundle"
BUNDLE_DIR = Path(__file__).parent.parent / "evidence_bundle" / "bundle"


def run_cmd(cmd: list, cwd: Path = None) -> str:
    """Run command and return stdout."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.stdout


def collect_evidence() -> Dict[str, Any]:
    """Collect all evidence artifacts."""
    evidence = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
        "gates": {},
        "artifacts": [],
    }
    
    # Load gates status
    gates_file = EVIDENCE_DIR / "gates_status.json"
    if gates_file.exists():
        evidence["gates"] = json.loads(gates_file.read_text())
    
    # Collect artifact paths
    for root, dirs, files in os.walk(EVIDENCE_DIR):
        for f in files:
            if f.endswith(('.json', '.xml', '.md', '.txt', '.log')):
                path = Path(root) / f
                rel_path = path.relative_to(EVIDENCE_DIR)
                evidence["artifacts"].append({
                    "path": str(rel_path),
                    "size": path.stat().st_size,
                    "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                })
    
    return evidence


def create_bundle(evidence: Dict[str, Any]) -> Path:
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
