import json
import re
from pathlib import Path


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
