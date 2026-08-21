from pathlib import Path


def _workflow(name: str) -> str:
    return Path(f".github/workflows/{name}").read_text(encoding="utf-8")


def test_pypi_workflow_is_tag_driven_oidc_only():
    workflow = _workflow("pypi-publish.yml")
    assert "workflow_dispatch:" in workflow
    assert "ref: ${{ inputs.tag }}" in workflow
    assert "id-token: write" in workflow
    assert "environment:" in workflow
    assert "PYPI_API_TOKEN" not in workflow
    assert "hatch build" in workflow
    assert "python3 -m build" not in workflow
    assert "password:" not in workflow


def test_release_workflow_has_no_py_pi_token_publisher():
    workflow = _workflow("release.yml")
    assert "PYPI_API_TOKEN" not in workflow


def test_release_workflow_builds_and_attests_python_artifacts_with_oidc():
    workflow = _workflow("release.yml")
    assert "hatch build" in workflow
    assert "actions/attest-build-provenance@v2" in workflow
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


def test_release_workflow_only_packs_existing_npm_workspaces():
    workflow = _workflow("release.yml")
    assert "cd contracts && npm pack --dry-run" in workflow
    assert "cd ../verdict/client-sdk && npm pack --dry-run" in workflow
    assert "verdict-node" not in workflow
