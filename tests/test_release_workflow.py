from pathlib import Path


def test_pypi_workflow_is_tag_driven_oidc_only():
    workflow = Path(".github/workflows/pypi-publish.yml").read_text()
    assert 'tags: ["v*"]' in workflow
    assert "id-token: write" in workflow
    assert "environment:" in workflow
    assert "PYPI_API_TOKEN" not in workflow


def test_release_workflow_has_no_py_pi_token_publisher():
    workflow = Path(".github/workflows/release.yml").read_text()
    assert "PYPI_API_TOKEN" not in workflow
