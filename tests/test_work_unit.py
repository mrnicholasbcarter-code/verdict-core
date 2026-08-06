from __future__ import annotations

import pytest

from verdict.work_unit import WorkUnit, WorkUnitError, normalize_owned_path, parse_work_units


def _unit(**overrides: object) -> WorkUnit:
    kwargs: dict[str, object] = {
        "unit_id": "unit-1",
        "objective": "remove unused imports",
        "owned_files": ("verdict/cli.py",),
        "verification_command": ("ruff", "check", "verdict/cli.py"),
    }
    kwargs.update(overrides)
    return WorkUnit(**kwargs)  # type: ignore[arg-type]


def test_valid_unit_normalizes_and_round_trips() -> None:
    unit = _unit(owned_files=["./verdict/cli.py", " tests/test_cli.py "])
    assert unit.owned_files == ("verdict/cli.py", "tests/test_cli.py")
    assert WorkUnit.from_dict(unit.to_dict()) == unit


def test_empty_owned_files_is_rejected() -> None:
    with pytest.raises(WorkUnitError, match="at least one path"):
        _unit(owned_files=())


def test_missing_verification_command_is_rejected() -> None:
    with pytest.raises(WorkUnitError, match="non-empty argv"):
        _unit(verification_command=())


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "verdict/../../outside.py", "/etc/passwd", "verdict\\cli.py", "   ", "."],
)
def test_escaping_or_malformed_paths_are_rejected(path: str) -> None:
    with pytest.raises(WorkUnitError):
        _unit(owned_files=(path,))


def test_duplicate_owned_paths_are_rejected() -> None:
    with pytest.raises(WorkUnitError, match="duplicate path"):
        _unit(owned_files=("verdict/cli.py", "./verdict/cli.py"))


def test_blank_identifiers_are_rejected() -> None:
    with pytest.raises(WorkUnitError, match="unit_id"):
        _unit(unit_id="  ")
    with pytest.raises(WorkUnitError, match="objective"):
        _unit(objective="")


def test_owns_and_out_of_bounds_report_the_boundary() -> None:
    unit = _unit(owned_files=("verdict/cli.py", "tests/test_cli.py"))
    assert unit.owns("./verdict/cli.py")
    assert not unit.owns("verdict/api.py")
    assert not unit.owns("../escape.py")
    assert unit.out_of_bounds(["verdict/cli.py", "verdict/api.py", "README.md"]) == (
        "README.md",
        "verdict/api.py",
    )


def test_from_dict_rejects_unknown_and_missing_fields() -> None:
    payload = _unit().to_dict()
    with pytest.raises(WorkUnitError, match="unknown field"):
        WorkUnit.from_dict({**payload, "model": "cc/claude-opus-5"})
    del payload["verification_command"]
    with pytest.raises(WorkUnitError, match="missing field"):
        WorkUnit.from_dict(payload)


def test_parse_work_units_requires_a_nonempty_unique_list() -> None:
    payload = _unit().to_dict()
    second = _unit(unit_id="unit-2", owned_files=("verdict/api.py",)).to_dict()
    assert len(parse_work_units([payload, second])) == 2

    with pytest.raises(WorkUnitError, match="no work units"):
        parse_work_units([])
    with pytest.raises(WorkUnitError, match="JSON list"):
        parse_work_units("unit-1")
    with pytest.raises(WorkUnitError, match="duplicate unit_id"):
        parse_work_units([payload, payload])


def test_one_invalid_unit_fails_the_whole_decomposition() -> None:
    good = _unit().to_dict()
    bad = _unit(unit_id="unit-2").to_dict()
    bad["verification_command"] = []
    with pytest.raises(WorkUnitError):
        parse_work_units([good, bad])


def test_normalize_owned_path_collapses_redundant_segments() -> None:
    assert normalize_owned_path("./a/b.py") == "a/b.py"
    assert normalize_owned_path("a/./b.py") == "a/b.py"
