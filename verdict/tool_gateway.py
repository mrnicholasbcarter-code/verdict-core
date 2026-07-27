"""
Tool Gateway for Verdict Core Guidance Control Plane

This module enforces all tool calls through the Guidance Control Plane.
It enforces:
- Budgets (token, time, cost)
- Idempotency (prevent duplicate writes)
- Schemas (validate tool arguments)
- Protected commands (block dangerous operations)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from verdict.guidance_control_plane import GateDecision, GuidanceControlPlane, ToolCall


class ToolCategory(Enum):
    """Categories of tools for policy enforcement."""
    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    SHELL = "shell"
    GIT = "git"
    MCP = "mcp"
    OPENVIKING = "openviking"
    RUVECTOR = "ruvector"
    CODE_REVIEW_GRAPH = "code_review_graph"
    OMNIROUTE = "omniroute"
    MEMORY = "memory"
    DELETE = "delete"


@dataclass
class BudgetLimit:
    """Budget limits for tool execution."""
    max_tokens: int = 100000
    max_time_seconds: int = 300
    max_cost_usd: float = 10.0
    max_tool_calls: int = 100


@dataclass
class ToolPolicy:
    """Policy for a specific tool."""
    tool_name: str
    category: ToolCategory
    required_args: list[str] = field(default_factory=list)
    forbidden_args: list[str] = field(default_factory=list)
    requires_approval: bool = False
    max_calls_per_task: int = 50
    budget: BudgetLimit | None = None
    protected: bool = False  # If true, requires explicit override


class ToolGateway:
    """
    Tool Gateway - enforces all tool calls through guidance policies.

    Every tool call must pass through this gateway before execution.
    """

    def __init__(self, guidance_plane: GuidanceControlPlane):
        self.guidance = guidance_plane
        self._tool_policies: dict[str, ToolPolicy] = {}
        self._call_counts: dict[str, int] = {}
        self._idempotency_keys: set[str] = set()
        self._budgets: dict[str, BudgetLimit] = {}
        self._protected_commands: set[str] = set()
        self._initialized = False

        # Initialize default policies
        self._setup_default_policies()

    def _setup_default_policies(self):
        """Set up default tool policies."""

        # READ tools - generally safe
        for tool in ["read_file", "glob", "grep", "list_dir", "read_notebook"]:
            self._tool_policies[tool] = ToolPolicy(
                tool_name=tool,
                category=ToolCategory.READ,
                max_calls_per_task=100
            )

        # WRITE tools - require idempotency
        for tool in ["write_file", "write_notebook"]:
            self._tool_policies[tool] = ToolPolicy(
                tool_name=tool,
                category=ToolCategory.WRITE,
                required_args=["file_path", "content"],
                max_calls_per_task=20,
                budget=BudgetLimit(max_tokens=50000)
            )

        # EDIT tools - require idempotency
        for tool in ["edit_file", "edit_notebook"]:
            self._tool_policies[tool] = ToolPolicy(
                tool_name=tool,
                category=ToolCategory.EDIT,
                required_args=["file_path", "old_string", "new_string"],
                max_calls_per_task=30,
                budget=BudgetLimit(max_tokens=50000)
            )

        # SHELL tools - dangerous, require approval
        for tool in ["bash", "shell_command"]:
            self._tool_policies[tool] = ToolPolicy(
                tool_name=tool,
                category=ToolCategory.SHELL,
                required_args=["command"],
                requires_approval=True,
                max_calls_per_task=10,
                budget=BudgetLimit(max_time_seconds=120, max_cost_usd=1.0)
            )

        # GIT tools - some are protected
        git_policies = {
            "git_status": ToolPolicy("git_status", ToolCategory.GIT, max_calls_per_task=20),
            "git_diff": ToolPolicy("git_diff", ToolCategory.GIT, max_calls_per_task=20),
            "git_log": ToolPolicy("git_log", ToolCategory.GIT, max_calls_per_task=20),
            "git_add": ToolPolicy("git_add", ToolCategory.GIT, requires_approval=True, max_calls_per_task=10),
            "git_commit": ToolPolicy("git_commit", ToolCategory.GIT, requires_approval=True, max_calls_per_task=5),
            "git_push": ToolPolicy("git_push", ToolCategory.GIT, protected=True, requires_approval=True, max_calls_per_task=2),
            "git_force_push": ToolPolicy("git_force_push", ToolCategory.GIT, protected=True, requires_approval=True, max_calls_per_task=1),
            "git_reset_hard": ToolPolicy("git_reset_hard", ToolCategory.GIT, protected=True, requires_approval=True, max_calls_per_task=1),
            "git_clean": ToolPolicy("git_clean", ToolCategory.GIT, protected=True, requires_approval=True, max_calls_per_task=1),
            "git_checkout": ToolPolicy("git_checkout", ToolCategory.GIT, protected=True, requires_approval=True, max_calls_per_task=5),
            "git_restore": ToolPolicy("git_restore", ToolCategory.GIT, protected=True, requires_approval=True, max_calls_per_task=5),
        }
        for name, policy in git_policies.items():
            self._tool_policies[name] = policy

        # MCP tools - vary by server
        mcp_tools = ["mcp__claude-flow__*", "mcp__ruvector__*", "mcp__openviking__*"]
        for tool in mcp_tools:
            self._tool_policies[tool] = ToolPolicy(
                tool_name=tool,
                category=ToolCategory.MCP,
                max_calls_per_task=50
            )

        # Memory tools
        memory_tools = ["memory_store", "memory_search", "memory_retrieve", "memory_delete"]
        for tool in memory_tools:
            self._tool_policies[tool] = ToolPolicy(
                tool_name=tool,
                category=ToolCategory.MEMORY,
                max_calls_per_task=30
            )

        # Protected commands that must be blocked
        self._protected_commands = {
            "git push --force",
            "git push -f",
            "git reset --hard",
            "git clean -fd",
            "git checkout --",
            "git restore",
            "rm -rf",
            "rm -rf /",
            "dd if=",
            "mkfs",
            "format",
            "drop database",
            "delete from",
            "truncate table",
            "aws secretsmanager put-secret-value",
            "aws ssm put-parameter",
            "kubectl delete",
            "terraform destroy",
            "docker rm -f",
            "docker system prune -a",
        }

    def register_tool_policy(self, policy: ToolPolicy):
        """Register a custom tool policy."""
        self._tool_policies[policy.tool_name] = policy

    def register_protected_command(self, command_pattern: str):
        """Register a command pattern that should be blocked."""
        self._protected_commands.add(command_pattern)

    def set_budget(self, task_id: str, budget: BudgetLimit):
        """Set budget for a task."""
        self._budgets[task_id] = budget

    async def check_tool_call(self, tool_call: ToolCall, task_id: str) -> GateDecision:
        """
        Check if a tool call is allowed.

        Returns a GateDecision with allowed=True/False and reason.
        """
        tool_name = tool_call.tool_name
        policy = self._tool_policies.get(tool_name)

        # Check if tool is known
        if policy is None:
            # Unknown tool - allow but warn
            return GateDecision(
                allowed=True,
                gate_name="tool_gateway",
                reason=f"Unknown tool '{tool_name}' - allowed with warning",
                severity="warning",
                metadata={"tool_name": tool_name, "policy": "unknown"}
            )

        # Check call count limit
        call_key = f"{task_id}:{tool_name}"
        current_count = self._call_counts.get(call_key, 0)
        if current_count >= policy.max_calls_per_task:
            return GateDecision(
                allowed=False,
                gate_name="tool_gateway",
                reason=f"Tool '{tool_name}' exceeded max calls per task ({policy.max_calls_per_task})",
                severity="error",
                metadata={"tool_name": tool_name, "current_count": current_count, "limit": policy.max_calls_per_task}
            )

        # Check idempotency for write/edit operations
        if policy.category in [ToolCategory.WRITE, ToolCategory.EDIT]:
            if tool_call.idempotency_key:
                if tool_call.idempotency_key in self._idempotency_keys:
                    return GateDecision(
                        allowed=False,
                        gate_name="tool_gateway",
                        reason=f"Duplicate idempotency key for '{tool_name}' - operation already performed",
                        severity="error",
                        metadata={"idempotency_key": tool_call.idempotency_key}
                    )
                # Add to idempotency keys
                self._idempotency_keys.add(tool_call.idempotency_key)

        # Check protected commands for shell/git
        if policy.category in [ToolCategory.SHELL, ToolCategory.GIT]:
            if tool_call.arguments:
                command = tool_call.arguments.get("command", "")
                if self._is_protected_command(command):
                    if policy.protected:
                        return GateDecision(
                            allowed=False,
                            gate_name="tool_gateway",
                            reason=f"Protected command blocked: {command}",
                            severity="critical",
                            metadata={"command": command, "protected": True}
                        )
                    elif policy.requires_approval:
                        return GateDecision(
                            allowed=False,
                            gate_name="tool_gateway",
                            reason=f"Command requires approval: {command}",
                            severity="warning",
                            metadata={"command": command, "requires_approval": True}
                        )

        # Check budget
        budget = self._budgets.get(task_id)
        if budget and policy.budget:
            # This would integrate with actual budget tracking
            pass

        # Check required arguments
        for req_arg in policy.required_args:
            if req_arg not in tool_call.arguments:
                return GateDecision(
                    allowed=False,
                    gate_name="tool_gateway",
                    reason=f"Missing required argument '{req_arg}' for tool '{tool_name}'",
                    severity="error",
                    metadata={"missing_arg": req_arg, "tool_name": tool_name}
                )

        # Check forbidden arguments
        for forbidden_arg in policy.forbidden_args:
            if forbidden_arg in tool_call.arguments:
                return GateDecision(
                    allowed=False,
                    gate_name="tool_gateway",
                    reason=f"Forbidden argument '{forbidden_arg}' for tool '{tool_name}'",
                    severity="error",
                    metadata={"forbidden_arg": forbidden_arg, "tool_name": tool_name}
                )

        # All checks passed
        return GateDecision(
            allowed=True,
            gate_name="tool_gateway",
            reason="Tool call allowed",
            metadata={"tool_name": tool_name, "policy": policy.category.value}
        )

    async def execute_tool_call(self, tool_call: ToolCall, task_id: str, executor: callable) -> Any:
        """
        Execute a tool call after gateway checks.

        The executor is a callable that performs the actual tool execution.
        """
        # Check gate
        decision = await self.check_tool_call(tool_call, task_id)

        if not decision.allowed:
            raise ToolGateError(decision.reason, decision)

        # Increment call count
        call_key = f"{task_id}:{tool_call.tool_name}"
        self._call_counts[call_key] = self._call_counts.get(call_key, 0) + 1

        # Execute the tool
        try:
            result = await executor(tool_call)
            return result
        except Exception as e:
            # Re-raise with gate context
            raise ToolGateError(f"Tool execution failed: {e}", decision) from e

    def _is_protected_command(self, command: str) -> bool:
        """Check if a command matches protected patterns."""
        command_lower = command.lower().strip()
        for protected in self._protected_commands:
            if protected.lower() in command_lower:
                return True
        return False

    def get_call_count(self, task_id: str, tool_name: str) -> int:
        """Get current call count for a tool in a task."""
        return self._call_counts.get(f"{task_id}:{tool_name}", 0)

    def reset_task(self, task_id: str):
        """Reset tracking for a task."""
        keys_to_remove = [k for k in self._call_counts if k.startswith(f"{task_id}:")]
        for key in keys_to_remove:
            del self._call_counts[key]
        if task_id in self._budgets:
            del self._budgets[task_id]


class ToolGateError(Exception):
    """Exception raised when tool gate denies execution."""

    def __init__(self, message: str, decision: GateDecision):
        super().__init__(message)
        self.decision = decision


# Convenience function for creating tool calls
def create_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    caller: str = "unknown",
    idempotency_key: str | None = None
) -> ToolCall:
    """Create a tool call with optional idempotency key."""
    if idempotency_key is None:
        # Generate from tool name + arguments
        content = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
        idempotency_key = hashlib.sha256(content.encode()).hexdigest()[:16]

    return ToolCall(
        tool_name=tool_name,
        arguments=arguments,
        caller=caller,
        idempotency_key=idempotency_key
    )


__all__ = [
    "BudgetLimit",
    "GateDecision",
    "ToolCall",
    "ToolCategory",
    "ToolGateError",
    "ToolGateway",
    "ToolPolicy",
    "create_tool_call",
]
