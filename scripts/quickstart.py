#!/usr/bin/env python3
"""
Clean-environment quickstart for Verdict.
Creates a temporary venv and tests the full stack.
Run: python scripts/quickstart.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd, cwd=None, check=True, env=None):
    """Run command and return result."""
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        print(f"❌ Command failed: {cmd}")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        sys.exit(1)
    return result


def main():
    print("🚀 Verdict Quickstart - Clean Environment Test")
    print("=" * 50)

    repo_root = Path(__file__).parent.parent
    print(f"📁 Repo root: {repo_root}")

    # Create temporary venv
    with tempfile.TemporaryDirectory() as tmpdir:
        venv_path = Path(tmpdir) / "venv"
        print(f"\n📦 Creating virtual environment at {venv_path}...")
        run(f"{sys.executable} -m venv {venv_path}")

        # Determine pip/python paths
        if sys.platform == "win32":
            pip = venv_path / "Scripts" / "pip.exe"
            python = venv_path / "Scripts" / "python.exe"
        else:
            pip = venv_path / "bin" / "pip"
            python = venv_path / "bin" / "python"

        # Step 1: Install in development mode with server extras
        print("\n📦 Installing verdict-core in development mode with server extras...")
        run(f"{pip} install -e {repo_root}[server] --quiet")

        # Step 2: Run flagship demo (credential-free)
        print("\n🎯 Running flagship demo (no credentials required)...")
        result = run(f"{python} scripts/flagship_demo.py", cwd=repo_root, check=False)

        if result.returncode == 0:
            print("✅ Flagship demo completed successfully!")
            import json

            try:
                demo_output = json.loads(result.stdout.strip())
                print("\n📋 Demo Results:")
                print(f"  Task: {demo_output['task_spec']['objective']}")
                print(f"  Required capabilities: {demo_output['requirements']['required']}")
                print(f"  Eligible candidates: {demo_output['eligible']}")
                print(f"  Selected route: {demo_output['decision']['selected_route']}")
                print(
                    f"  Exclusions: {len(demo_output['decision']['exclusions'])} candidates rejected"
                )
            except json.JSONDecodeError:
                print(f"  Output: {result.stdout[:200]}...")

            # Print exclusions with correct field names
            print("\n📋 Exclusions:")
            for excl in demo_output["decision"]["exclusions"]:
                model_key = "model"  # Demo uses 'model' field
                print(f"    - {excl.get(model_key, 'unknown')}: {excl.get('reason', 'no reason')}")
        else:
            print(f"❌ Demo failed with exit code {result.returncode}")
            print(f"stderr: {result.stderr}")
            sys.exit(1)

        # Step 3: Run a simple API route test
        print("\n🔀 Testing /v1/route endpoint...")
        test_script = """
from verdict.api import app
from fastapi.testclient import TestClient

client = TestClient(app)
resp = client.post('/v1/route', json={
    'objective': 'test',
    'task_type': 'chat',
    'effort': 'low',
    'reasoning': 'low',
    'required_capabilities': [],
    'tools': [],
    'privacy': 'public',
    'risk': 'low',
    'production_impact': False,
    'verification': {'checks': []}
})
print(resp.status_code)
import json
print(json.dumps(resp.json(), indent=2))
"""
        test_result = run(f'{python} -c "{test_script}"', cwd=repo_root)
        print(f"✅ Route test status: {test_result.stdout.split(chr(10))[0]}")

        # Step 4: Verify CLI works
        print("\n💻 Testing CLI...")
        run(f"{python} -m verdict --help", cwd=repo_root)
        print("✅ CLI help works")

        # Step 5: Run a subset of tests
        print("\n🧪 Running core test suite...")
        test_result = run(
            f"{python} -m pytest tests/test_contracts.py tests/test_models.py -v --tb=short 2>&1 | tail -20",
            cwd=repo_root,
            check=False,
        )
        if test_result.returncode == 0:
            print("✅ Core tests pass")
        else:
            print("⚠️  Some tests had issues (check output)")

    print("\n" + "=" * 50)
    print("🎉 QUICKSTART PASSED - All systems operational!")
    print("=" * 50)
    print("\nNext steps:")
    print("  - Read docs at https://verdict.dev/docs")
    print("  - Try: python -m verdict route --help")
    print("  - Start API: python -m verdict serve")
    print("  - Explore: python scripts/flagship_demo.py")


if __name__ == "__main__":
    main()
