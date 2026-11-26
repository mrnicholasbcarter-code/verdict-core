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


ALLOWED_SHARD = frozenset({"split", "leader"})
SHARDED_MODE = "full"


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
    # "split": the gate's tests are divided across the legs of a sharded run.
    # "leader": the gate runs in leg 1 only. None: the gate runs in every leg, unchanged.
    shard: str | None = None
    # Environment variable names the runner removes when it launches this gate.
    env_clear: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProofContract:
    schema_version: int
    name: str
    runner: str
    modes: Mapping[str, Mapping[str, Any]]
    gates: tuple[GateSpec, ...]

    @property
    def shards(self) -> int | None:
        """The number of legs the full proof declares, or None when it declares none."""
        full = self.modes.get(SHARDED_MODE)
        if not isinstance(full, Mapping):
            return None
        declared = full.get("shards")
        return declared if isinstance(declared, int) and not isinstance(declared, bool) else None

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


def gate_extends(leader: GateSpec, split: GateSpec) -> bool:
    """True when ``leader``'s command is ``split``'s plus more arguments: a narrower run of it.

    The loader requires this of a leader gate, and the runner requires it before it
    lets a split gate stand in for a leader that an unsharded run skips.
    """
    return (
        len(leader.command) > len(split.command)
        and leader.command[: len(split.command)] == split.command
    )


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
    shard = raw.get("shard")
    if shard is not None and (not isinstance(shard, str) or shard not in ALLOWED_SHARD):
        raise ContractError(
            f"gates[{index}].shard must be one of {sorted(ALLOWED_SHARD)}; got {shard!r}"
        )
    env_clear_raw = raw.get("env_clear")
    env_clear: tuple[str, ...] = ()
    if env_clear_raw is not None:
        if not isinstance(env_clear_raw, list):
            raise ContractError(f"gates[{index}].env_clear must be a list of strings")
        env_clear = tuple(
            _require_str(name, field=f"gates[{index}].env_clear") for name in env_clear_raw
        )
    return GateSpec(
        id=gate_id,
        phase=phase,
        classify=classify,
        command=command,
        required=required,
        when=when,
        targets_placeholder=targets_placeholder,
        shard=shard,
        env_clear=env_clear,
    )


def _validate_shards(modes: Mapping[str, object]) -> None:
    """Refuse a bad ``shards`` value; only the full mode may declare one."""
    for mode_name, config in modes.items():
        if not isinstance(config, Mapping) or "shards" not in config:
            continue
        declared = config["shards"]
        if mode_name != SHARDED_MODE:
            raise ContractError(f"modes.{mode_name}.shards is only allowed on the full mode")
        if isinstance(declared, bool) or not isinstance(declared, int) or declared < 1:
            raise ContractError(
                f"modes.{mode_name}.shards must be a positive integer; got {declared!r}"
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
    _validate_shards(modes)
    gates_raw = payload.get("gates")
    if not isinstance(gates_raw, list) or not gates_raw:
        raise ContractError("gates must be a non-empty list")
    gates = tuple(_parse_gate(item, index=i) for i, item in enumerate(gates_raw))
    ids = [gate.id for gate in gates]
    if len(ids) != len(set(ids)):
        raise ContractError("gate ids must be unique")
    split = [g for g in gates if g.shard == "split"]
    for gate in gates:
        if gate.shard is not None and gate.classify != "test":
            raise ContractError(f"gate {gate.id}: shard requires classify: test")
        if gate.shard is not None and gate.targets_placeholder is not None:
            raise ContractError(
                f"gate {gate.id}: a sharded gate cannot take a targets placeholder, because "
                "the runner would skip it for want of --targets, outside any leg's accounting"
            )
        if gate.shard == "leader" and not any(gate_extends(gate, s) for s in split):
            raise ContractError(
                f"gate {gate.id}: a leader gate must extend the command of a split gate, "
                "because an unsharded run skips it and relies on that gate to run its tests"
            )
    return ProofContract(schema_version=1, name=name, runner=runner, modes=modes, gates=gates)
