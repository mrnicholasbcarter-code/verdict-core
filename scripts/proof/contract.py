"""Proof contract loading for the BOD-89 canonical proof pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ALLOWED_RUNNERS = frozenset({"native"})
ALLOWED_CLASSIFY = frozenset(
    {
        "format",
        "lint",
        "type",
        "schema",
        "test",
        "security",
        "secret",
        "build",
        "acceptance",
        "structural",
        "adversarial",
    }
)


class ContractError(ValueError):
    """Raised when a proof contract is malformed or unsafe."""


@dataclass(frozen=True)
class GateSpec:
    id: str
    phase: str
    classify: str
    command: tuple[str, ...]
    required: bool = True
    when: str | None = None
    targets_placeholder: str | None = None


@dataclass(frozen=True)
class ProofContract:
    schema_version: int
    name: str
    runner: str
    modes: Mapping[str, Mapping[str, Any]]
    gates: tuple[GateSpec, ...]

    def gates_for_mode(self, mode: str, *, llm_boundary: bool = False) -> tuple[GateSpec, ...]:
        if mode not in self.modes:
            raise ContractError(f"unknown proof mode: {mode}")
        phases = self.modes[mode].get("phases")
        if not isinstance(phases, list) or not phases:
            raise ContractError(f"mode {mode} must declare a non-empty phases list")
        phase_set = {str(p) for p in phases}
        selected: list[GateSpec] = []
        for gate in self.gates:
            if gate.phase not in phase_set:
                continue
            if gate.when == "llm_boundary" and not llm_boundary:
                continue
            selected.append(gate)
        return tuple(selected)


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value.strip()


def _parse_gate(raw: object, *, index: int) -> GateSpec:
    if not isinstance(raw, dict):
        raise ContractError(f"gates[{index}] must be an object")
    gate_id = _require_str(raw.get("id"), field=f"gates[{index}].id")
    phase = _require_str(raw.get("phase"), field=f"gates[{index}].phase")
    classify = _require_str(raw.get("classify"), field=f"gates[{index}].classify")
    if classify not in ALLOWED_CLASSIFY:
        raise ContractError(f"gates[{index}].classify unsupported: {classify}")
    command_raw = raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ContractError(f"gates[{index}].command must be a non-empty list")
    command = tuple(_require_str(part, field=f"gates[{index}].command") for part in command_raw)
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ContractError(f"gates[{index}].required must be a bool")
    when = raw.get("when")
    if when is not None:
        when = _require_str(when, field=f"gates[{index}].when")
    targets_placeholder = raw.get("targets_placeholder")
    if targets_placeholder is not None:
        targets_placeholder = _require_str(
            targets_placeholder, field=f"gates[{index}].targets_placeholder"
        )
    return GateSpec(
        id=gate_id,
        phase=phase,
        classify=classify,
        command=command,
        required=required,
        when=when,
        targets_placeholder=targets_placeholder,
    )


def load_contract(path: Path) -> ProofContract:
    """Load and validate a proof contract YAML file."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"cannot read contract: {path}") from exc
    try:
        payload = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ContractError(f"contract is not valid YAML: {path}") from exc
    if not isinstance(payload, dict):
        raise ContractError("contract root must be an object")

    schema_version = payload.get("schema_version")
    if schema_version != 1:
        raise ContractError("schema_version must be 1")
    name = _require_str(payload.get("name"), field="name")
    runner = _require_str(payload.get("runner"), field="runner")
    if runner not in ALLOWED_RUNNERS:
        raise ContractError(
            f"runner must be one of {sorted(ALLOWED_RUNNERS)}; got {runner!r}. "
            "Dagger was evaluated and deferred: native gates already pin versions "
            "via uv/npm and preserve raw exits without a second CI runtime."
        )
    modes = payload.get("modes")
    if not isinstance(modes, dict) or not modes:
        raise ContractError("modes must be a non-empty object")
    gates_raw = payload.get("gates")
    if not isinstance(gates_raw, list) or not gates_raw:
        raise ContractError("gates must be a non-empty list")
    gates = tuple(_parse_gate(item, index=i) for i, item in enumerate(gates_raw))
    ids = [gate.id for gate in gates]
    if len(ids) != len(set(ids)):
        raise ContractError("gate ids must be unique")
    return ProofContract(schema_version=1, name=name, runner=runner, modes=modes, gates=gates)
