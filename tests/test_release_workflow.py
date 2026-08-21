from pathlib import Path


def _workflow(name: str) -> str:
    return Path(f".github/workflows/{name}").read_text(encoding="utf-8")


def test_pypi_workflow_is_tag_driven_oidc_only():
    workflow = _workflow("pypi-publish.yml")
    assert 'tags: ["v*"]' in workflow
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


def test_release_workflow_creates_non_overwriting_release_from_python_artifacts():
    workflow = _workflow("release.yml")
    assert "contents: write" in workflow
    assert "fail_on_unmatched_files: true" in workflow
    assert "overwrite_files: false" in workflow
    assert "dist/*.whl" in workflow
    assert "dist/*.tar.gz" in workflow
