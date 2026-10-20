"""BOD-431: OpenCodeReviewer._read_api_key resolves through credentials_store.

Independent review must work wherever ``resolve_gateway_key`` finds a key
(the owner-only key file, credentials.env, Prime auth.json), not only from an
exported ``VERDICT_OMNIROUTE_API_KEY``/``OMNIROUTE_API_KEY`` environment
variable. See tests/test_gateway_key_resolution.py for the same fixture style.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verdict.orchestration.review import OpenCodeReviewer

FILE_KEY = "file-key-fixture"


@pytest.fixture
def key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "verdict").mkdir(parents=True, mode=0o700)
    path = config / "verdict" / "omniroute-api-key"
    path.write_text(FILE_KEY + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))


def _reviewer(tmp_path: Path, **kwargs: object) -> OpenCodeReviewer:
    return OpenCodeReviewer(selector=None, out_dir=tmp_path / "out", **kwargs)  # type: ignore[arg-type]


def test_read_api_key_falls_back_to_owner_only_key_file(
    key_file: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    reviewer = _reviewer(tmp_path)
    assert reviewer._read_api_key() == FILE_KEY


def test_read_api_key_raises_when_nothing_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    reviewer = _reviewer(tmp_path)
    with pytest.raises(KeyError, match="missing API key env"):
        reviewer._read_api_key()
