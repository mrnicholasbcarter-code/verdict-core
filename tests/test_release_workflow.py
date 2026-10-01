import json
import re
from pathlib import Path

import pytest


def _workflow(name: str) -> str:
    return Path(f".github/workflows/{name}").read_text(encoding="utf-8")


def _package_manifest(path: str) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_pypi_workflow_is_validation_only():
    workflow = _workflow("pypi-publish.yml")
    assert "workflow_dispatch:" in workflow
    assert "ref: ${{ inputs.tag }}" in workflow
    assert "PYPI_API_TOKEN" not in workflow
    assert "hatch build" in workflow
    assert "python3 -m build" not in workflow
    assert "password:" not in workflow
    assert "gh-action-pypi-publish" not in workflow


def test_release_workflow_has_no_py_pi_token_publisher():
    workflow = _workflow("release.yml")
    assert "PYPI_API_TOKEN" not in workflow


def test_release_workflow_caches_the_root_workspace_lockfile():
    workflow = _workflow("release.yml")

    assert "cache-dependency-path: package-lock.json" in workflow
    assert "cache-dependency-path: contracts/package-lock.json" not in workflow
    assert Path("package-lock.json").is_file()


def test_release_workflow_builds_typescript_workspaces_before_testing_them():
    workflow = _workflow("release.yml")

    build_step = "- name: Build TypeScript packages"
    test_step = "- name: Run TypeScript tests"
    assert workflow.count(build_step) == 1
    assert workflow.count(test_step) == 1
    assert workflow.index(build_step) < workflow.index(test_step)


def test_release_workflow_builds_and_attests_python_artifacts_with_oidc():
    workflow = _workflow("release.yml")
    assert "hatch build" in workflow
    # Pinned to a release tag, but deliberately not to one exact version: a
    # dependency bump must not fail this test. What matters is that the
    # attestation step runs and is not on a moving ref.
    assert re.search(r"actions/attest-build-provenance@v\d+(?:\.\d+)*\b", workflow)
    assert "attestations: write" in workflow
    assert "id-token: write" in workflow
    assert "subject-path: dist/" in workflow
    assert "steps.attest-python.outputs.bundle-path" in workflow
    assert "python-distribution-provenance.intoto.jsonl" in workflow


def test_release_workflow_rejects_existing_release_from_python_artifacts():
    workflow = _workflow("release.yml")
    assert "contents: write" in workflow
    assert 'gh release view "$GITHUB_REF_NAME"' in workflow
    assert "refusing to mutate it" in workflow
    assert 'gh release create "$GITHUB_REF_NAME" release-assets/*' in workflow
    assert "--verify-tag" in workflow
    assert "cp dist/*.whl dist/*.tar.gz release-assets/" in workflow


def test_release_workflow_publishes_the_attested_python_artifacts_once():
    release_workflow = _workflow("release.yml")
    pypi_workflow = _workflow("pypi-publish.yml")
    assert "uses: pypa/gh-action-pypi-publish@release/v1" in release_workflow
    assert "workflow_dispatch:" in pypi_workflow
    assert "tags:" not in pypi_workflow
    assert "gh-action-pypi-publish" not in pypi_workflow


def test_release_workflow_only_packs_existing_npm_workspaces():
    workflow = _workflow("release.yml")
    assert (
        "npm pack --workspace @bodanglin/verdict-contracts --pack-destination release-assets"
        in workflow
    )
    assert (
        "npm pack --workspace @bodanglin/verdict-client --pack-destination release-assets"
        in workflow
    )
    assert "verdict-node" not in workflow


def test_release_workflow_is_the_only_npm_publication_authority():
    release_workflow = _workflow("release.yml")
    contracts_workflow = _workflow("npm-publish-contracts.yml")
    client_workflow = _workflow("npm-publish-client.yml")

    assert "npm publish ./release-assets/bodanglin-verdict-contracts-" in release_workflow
    assert "npm publish ./release-assets/bodanglin-verdict-client-" in release_workflow
    assert "environment: pypi" in release_workflow
    assert "NODE_AUTH_TOKEN" not in release_workflow
    assert "npm install --global npm@11.5.1" in release_workflow
    assert "npm --version | grep" in release_workflow
    for workflow in (contracts_workflow, client_workflow):
        assert "workflow_dispatch:" in workflow
        assert "release:" not in workflow
        assert "npm publish" not in workflow


def test_release_workflow_publishes_npm_tarballs_as_local_paths():
    workflow = _workflow("release.yml")

    assert "npm publish ./release-assets/bodanglin-verdict-contracts-" in workflow
    assert "npm publish ./release-assets/bodanglin-verdict-client-" in workflow
    assert "npm publish release-assets/" not in workflow


def test_release_workflow_requires_one_synchronized_version():
    workflow = _workflow("release.yml")

    assert "python3 scripts/verify_release_versions.py" in workflow
    assert '--tag "$GITHUB_REF_NAME"' in workflow


def test_release_workflow_preflights_every_immutable_target_and_oidc():
    workflow = _workflow("release.yml")

    assert "ACTIONS_ID_TOKEN_REQUEST_URL" in workflow
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in workflow
    assert "npm ping" in workflow
    assert 'npm view "@bodanglin/verdict-contracts@$version"' in workflow
    assert 'npm view "@bodanglin/verdict-client@$version"' in workflow
    assert "https://pypi.org/pypi/verdict-core/$version/json" in workflow
    assert 'gh release view "$GITHUB_REF_NAME"' in workflow


def test_release_workflow_records_fail_closed_partial_recovery_guidance():
    workflow = _workflow("release.yml")
    recovery = Path("docs/release-recovery.md").read_text(encoding="utf-8")

    assert "if: ${{ always() }}" in workflow
    assert "docs/release-recovery.md" in workflow
    assert "never overwrite" in recovery
    assert "Do not blindly rerun" in recovery


def test_client_package_metadata_points_to_the_canonical_repository():
    package = _package_manifest("verdict/client-sdk/package.json")

    assert package["repository"] == {
        "type": "git",
        "url": "git+https://github.com/mrnicholasbcarter-code/verdict-core.git",
    }
    assert package["homepage"] == "https://github.com/mrnicholasbcarter-code/verdict-core#readme"
    assert package["bugs"] == {
        "url": "https://github.com/mrnicholasbcarter-code/verdict-core/issues"
    }


def test_client_package_verification_script_is_present():
    package = _package_manifest("verdict/client-sdk/package.json")

    assert package["scripts"]["verify:package"] == (
        "npm run build && node scripts/verify-package.mjs"
    )
    assert Path("verdict/client-sdk/scripts/verify-package.mjs").is_file()


def test_client_package_lint_command_is_runnable_with_declared_dependencies():
    package = _package_manifest("verdict/client-sdk/package.json")

    assert package["scripts"]["lint"] == "tsc --noEmit"


def test_client_package_verifier_has_required_dev_tooling():
    package = _package_manifest("verdict/client-sdk/package.json")
    dev_dependencies = package["devDependencies"]

    assert "typescript" in dev_dependencies
    assert "@types/node" in dev_dependencies
    assert package["engines"] == {"node": ">=18"}
    assert "vitest" in dev_dependencies


def test_release_candidate_versions_are_unique_and_synchronized():
    python_project = Path("pyproject.toml").read_text(encoding="utf-8")
    contracts = _package_manifest("contracts/package.json")
    client = _package_manifest("verdict/client-sdk/package.json")

    assert 'version = "0.4.1"' in python_project
    assert contracts["version"] == "0.4.1"
    assert client["version"] == "0.4.1"
    assert client["peerDependencies"] == {"@bodanglin/verdict-contracts": "^0.4.1"}


def test_every_release_version_location_matches_pyproject():
    """One release version everywhere it is written, including both lock files."""
    import verdict

    python_project = Path("pyproject.toml").read_text(encoding="utf-8")
    project_version = re.search(r'^version = "([^"]+)"', python_project, re.MULTILINE)
    assert project_version is not None
    version = project_version.group(1)
    caret = f"^{version}"

    assert verdict.__version__ == version
    assert _package_manifest("package.json")["version"] == version
    assert _package_manifest("contracts/package.json")["version"] == version
    client = _package_manifest("verdict/client-sdk/package.json")
    assert client["version"] == version
    assert client["peerDependencies"] == {"@bodanglin/verdict-contracts": caret}
    assert client["devDependencies"]["@bodanglin/verdict-contracts"] == caret  # type: ignore[index]

    lock = json.loads(Path("package-lock.json").read_text(encoding="utf-8"))
    packages = lock["packages"]
    assert lock["version"] == version
    assert packages[""]["version"] == version
    assert packages["contracts"]["version"] == version
    assert packages["verdict/client-sdk"]["version"] == version
    for field in ("peerDependencies", "devDependencies"):
        assert packages["verdict/client-sdk"][field]["@bodanglin/verdict-contracts"] == caret

    uv_lock = Path("uv.lock").read_text(encoding="utf-8")
    assert f'name = "verdict-core"\nversion = "{version}"' in uv_lock

    readme = Path("README.md").read_text(encoding="utf-8")
    assert (
        f"[![version {version}](https://img.shields.io/badge/version-{version}-blue.svg)]" in readme
    )
    assert f"- **Version {version}, active development.**" in readme


def test_release_workflow_publishes_a_smoke_tested_attested_container_image():
    """BOD-250: build and smoke-test before any registry write; push, attest, then move :latest."""
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    preflight = workflow.index("- name: Preflight immutable publication targets")
    build = workflow.index("- name: Build and smoke-test the container image")
    npm = workflow.index("- name: Publish to npm")
    pypi = workflow.index("- name: Publish Python package to PyPI")
    gh_release = workflow.index("- name: Create GitHub Release")
    push = workflow.index("- name: Publish the container image version tag to GHCR")
    attest = workflow.index("- name: Attest the container image")
    latest = workflow.index("- name: Move the latest tag to the attested image")
    # The image is proven before ANY registry write, and :latest moves only after attestation.
    assert preflight < build < min(npm, pypi, gh_release)
    assert max(npm, pypi, gh_release) < push < attest < latest
    smoke = workflow[build:npm]
    assert "--network none" in smoke
    assert "run_finished +outcome=COMPLETE" in smoke
    assert "Receipt integrity verified" in smoke
    push_step = workflow[push:attest]
    assert ":latest" not in push_step, "only the version tag is pushed before attestation"
    assert "subject-digest: ${{ steps.push-image.outputs.digest }}" in workflow[attest:latest]
    assert "push-to-registry: true" in workflow[attest:latest]
    assert 'docker push "$IMAGE_REF:latest"' in workflow[latest:]


def _release_step_script(name: str) -> str:
    import yaml

    data = yaml.safe_load(Path(".github/workflows/release.yml").read_text(encoding="utf-8"))
    for step in data["jobs"]["release"]["steps"]:
        if step.get("name") == name:
            return str(step["run"])
    raise AssertionError(f"step not found: {name}")


_FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  login) cat >/dev/null; exit 0;;
  manifest) case "$FAKE_INSPECT" in
      exists) echo '{"schemaVersion":2}'; exit 0;;
      missing) echo 'manifest unknown' >&2; exit 1;;
      missing_long) echo 'manifest unknown: manifest unknown' >&2; exit 1;;
      nosuch) echo "no such manifest: $3" >&2; exit 1;;
      nosuch_other) echo 'no such manifest: ghcr.io/other/image:1.0' >&2; exit 1;;
      autherr) echo 'unauthorized: authentication required' >&2; exit 1;;
      denied) echo "Get \"https://ghcr.io/v2/owner/verdict-core/manifests/0.9.9\": denied" >&2; exit 1;;
      mixed) printf 'manifest unknown\\nunauthorized: authentication required\\n' >&2; exit 1;;
      notfound_other) echo 'Error response from daemon: not found' >&2; exit 1;;
      empty) exit 1;;
      blank_after) printf 'manifest unknown\\n\\nunauthorized: authentication required\\n' >&2; exit 1;;
      blank_between) printf '\\nmanifest unknown\\n' >&2; exit 1;;
    esac;;
  push) case "$FAKE_PUSH" in
      ok) echo "0.9.9: digest: sha256:$(printf 'a%.0s' $(seq 1 64)) size: 1234"; exit 0;;
      nodigest) echo "0.9.9: pushed"; exit 0;;
      fail) echo "denied: requested access to the resource is denied" >&2; exit 1;;
    esac;;
esac
exit 0
"""

# Real Docker CLI 29 output, observed against ghcr.io and docker.io: a missing tag prints exactly
# "manifest unknown" (ghcr.io, logged in) or "no such manifest: <ref>" (docker.io); an
# unauthenticated ghcr.io lookup prints 'Get "https://ghcr.io/v2/.../manifests/<tag>": denied'.
_ABSENCE_CASES = [
    ("missing", True),
    ("missing_long", True),
    ("nosuch", True),
    ("exists", False),
    ("autherr", False),
    ("denied", False),
    ("mixed", False),
    ("notfound_other", False),
    ("nosuch_other", False),
    ("empty", False),
    ("blank_after", False),
    ("blank_between", False),
]


def _run_with_fake_docker(tmp_path: Path, script: str, inspect: str, push: str) -> tuple[int, str]:
    import os
    import shutil
    import subprocess

    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("")
    docker_log = tmp_path / "docker_calls.log"
    docker_log.write_text("")
    env = {
        "FAKE_DOCKER_LOG": str(docker_log),
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY_OWNER": "Owner",
        "IMAGE_REF": "ghcr.io/owner/verdict-core",
        "IMAGE_VERSION": "0.9.9",
        "GH_TOKEN": "x",
        "FAKE_INSPECT": inspect,
        "FAKE_PUSH": push,
    }
    body = script.replace("${{ github.actor }}", "ci")
    proc = subprocess.run(
        ["bash", "-e", "-c", body], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    _run_with_fake_docker.calls = docker_log.read_text().splitlines()  # type: ignore[attr-defined]
    return proc.returncode, output.read_text()


@pytest.mark.parametrize(("inspect", "expect_ok"), _ABSENCE_CASES)
def test_image_tag_absent_script_accepts_only_an_exact_unmixed_absence(
    tmp_path, inspect, expect_ok
):
    script = 'scripts/image_tag_absent.sh "$IMAGE_REF:$IMAGE_VERSION"'
    code, _ = _run_with_fake_docker(tmp_path, script, inspect, "ok")
    assert (code == 0) is expect_ok, (inspect, code)


@pytest.mark.parametrize(
    ("inspect", "push", "expect_ok"),
    # Every absence case with a good push, plus a bad push and a push with no digest.
    [(inspect, "ok", ok) for inspect, ok in _ABSENCE_CASES]
    + [("missing", "nodigest", False), ("missing", "fail", False)],
)
def test_image_push_step_fails_closed(tmp_path, inspect, push, expect_ok):
    script = _release_step_script("Publish the container image version tag to GHCR")
    code, output = _run_with_fake_docker(tmp_path, script, inspect, push)
    calls = _run_with_fake_docker.calls  # type: ignore[attr-defined]
    pushes = [c for c in calls if c.startswith("push ")]
    assert (code == 0) is expect_ok, (inspect, push, code)
    absent = dict(_ABSENCE_CASES)[inspect]
    if absent:
        # Exactly one push, of the immutable version tag only; never :latest here.
        assert pushes == ["push ghcr.io/owner/verdict-core:0.9.9"], calls
    else:
        # A tag that exists, or an unconfirmed absence, must never reach docker push.
        assert pushes == [], calls
    if expect_ok:
        assert output.strip() == "digest=sha256:" + "a" * 64
    else:
        assert "digest=" not in output


@pytest.mark.parametrize(("inspect", "expect_ok"), _ABSENCE_CASES)
def test_image_preflight_requires_a_confirmed_absence(tmp_path, inspect, expect_ok):
    full = _release_step_script("Preflight immutable publication targets")
    image_part = full[full.index('image="ghcr.io') :]
    code, _ = _run_with_fake_docker(
        tmp_path, "set -euo pipefail\nversion=0.9.9\n" + image_part, inspect, "ok"
    )
    assert (code == 0) is expect_ok, (inspect, code)
    calls = _run_with_fake_docker.calls  # type: ignore[attr-defined]
    assert not [c for c in calls if c.startswith("push ")], calls
    assert "manifest inspect ghcr.io/owner/verdict-core:0.9.9" in calls
