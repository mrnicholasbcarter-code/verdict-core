"""Native Prime kernel bridge for Verdict's external, owned worker controller.

Load this small stdlib-only integration with runpy. All Verdict discovery, imports,
selection and execution policy run in the checkout's .venv, not Prime's kernel.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any


def write_json(path: Path, value: object) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, default=str))
    temp.replace(path)


class PrimeWorkerOperation:
    def __init__(self, rlm: Any, bash: Any, repo: str, prompt: str, *,
                 task: dict[str, Any] | None = None, budget: dict[str, Any] | None = None) -> None:
        self.rlm = rlm
        self.repo = Path(repo).resolve()
        git_path = self.repo / ".git"
        if git_path.is_file():
            git_path = (self.repo / git_path.read_text().strip().removeprefix("gitdir: ")).resolve()
        common_file = git_path / "commondir"
        if common_file.exists():
            git_path = (git_path / common_file.read_text().strip()).resolve()
        self.directory = git_path / "verdict-prime" / "worker-runs" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        write_json(self.directory / "config.json", {"prompt": prompt, "task": task or {},
                                                  "budget": budget or {}})
        self.handle = bash(f"cd {shlex.quote(str(self.repo))} && "
            f".venv/bin/python -m verdict.worker_runtime {shlex.quote(str(self.directory))}")
        self.bridge = asyncio.create_task(self.serve())

    async def serve(self) -> None:
        seen: set[str] = set()
        owned: dict[str, Any] = {}
        try:
            while self.handle.running:
                path = self.directory / "request.json"
                if not path.exists():
                    await asyncio.sleep(0.05)
                    continue
                request = json.loads(path.read_text())
                request_id = request["id"]
                if request_id in seen:
                    await asyncio.sleep(0.05)
                    continue
                seen.add(request_id)
                try:
                    method = request["method"]
                    if method == "spawn":
                        # Exact selector is mandatory on every attempt. Never inherit controller.
                        model = request["model"]
                        if model.removeprefix("omniroute/") in {"cx/gpt-5.6-sol", "cx/gpt-6-astra"}:
                            raise ValueError("controller cannot be a worker")
                        child = await self.rlm.spawn(request["prompt"], name=request["name"], model=model)
                        owned[child.rlm_child_id] = child
                        value: Any = asdict(child)
                        write_json(self.directory / f"admission-{child.rlm_child_id}.json", value)
                    elif method == "collect":
                        child_id = request["child_id"]
                        if child_id not in owned:
                            raise ValueError("child not owned by this operation")
                        value = [asdict(item) for item in await self.rlm.collect(child_id, timeout_ms=0)]
                    elif method == "delete":
                        child_id = request["child_id"]
                        if child_id not in owned:
                            child_id = next((key for key, child in owned.items()
                                             if child.name == child_id), child_id)
                        if child_id not in owned:
                            raise ValueError("child not owned by this operation")
                        await self.rlm.delete_subagent(child_id)
                        owned.pop(child_id)
                        value = True
                    else:
                        raise ValueError("unsupported bridge request")
                    write_json(self.directory / "response.json", {"id": request_id, "value": value})
                except Exception as exc:
                    write_json(self.directory / "response.json", {"id": request_id,
                        "error": f"{type(exc).__name__}: {exc}"})
        finally:
            # Includes late admission after controller timeout. Never leak an owned writer.
            for child_id in owned:
                try:
                    await self.rlm.delete_subagent(child_id)
                except Exception as exc:
                    write_json(self.directory / "cleanup-error.json", {"child_id": child_id,
                                                                       "error": str(exc)})

    def result(self) -> dict[str, Any]:
        if self.handle.running:
            raise RuntimeError("operation still running; end turn and wait for bash completion")
        path = self.directory / "outcome.json"
        if path.exists():
            return json.loads(path.read_text())
        return {"state": "FAIL_CLOSED", "output": "", "diagnostic":
            "controller exited without an outcome; inspect process output and events.jsonl", "candidate": None}


def start(rlm: Any, bash: Any, repo: str, prompt: str, **kwargs: Any) -> PrimeWorkerOperation:
    return PrimeWorkerOperation(rlm, bash, repo, prompt, **kwargs)
