"""
Guidance Control Plane for Verdict Core

This module integrates @claude-flow/guidance as the mandatory runtime control plane
for Verdict Core. It provides:

1. Singleton control plane initialization from CLAUDE.md / CLAUDE.local.md
2. TaskSpec normalization for all execution requests
3. Guidance retrieval with intent-based shard selection
4. Gate enforcement (including Verdict Core integration)
5. Tool gateway for all tool calls
6. Continue gate for autonomous step evaluation
7. Memory governance for all memory writes
8. Proof chain recording for audit trail

Architecture:
Claude Code → TaskSpec Normalization → Guidance Control Plane → Verdict Core → Tool Gateway → Ruflo → Workers → OmniRoute → Provider
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.eligibility import EligibilityGate
from verdict.gate import Gate


@dataclass
class GuidanceConfig:
    """Configuration for the Guidance Control Plane."""
    repo_root: Path
    claude_md_path: Path
    claude_local_md_path: Path | None = None
    policy_bundle_cache_ttl_seconds: int = 3600
    enable_tool_gateway: bool = True
    enable_continue_gate: bool = True
    enable_memory_gate: bool = True
    enable_proof_chain: bool = True
    max_proof_chain_entries: int = 10000


@dataclass
class TaskSpec:
    """Normalized task specification - single source of truth for execution."""
    goal: str
    affected_files: list[str] = field(default_factory=list)
    risk_level: str = "medium"  # low, medium, high, critical
    protected_work: bool = False
    capabilities: list[str] = field(default_factory=list)
    privacy_level: str = "standard"  # standard, confidential, restricted
    authorization_required: bool = False
    quality_threshold: float = 0.8
    verification_plan: list[str] = field(default_factory=list)
    rollback_plan: str = ""
    tool_intent: str = ""
    task_id: str = field(default_factory=lambda: hashlib.md5(str(time.time()).encode()).hexdigest()[:12])
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GuidanceShard:
    """A retrieved guidance shard relevant to a task."""
    rule_id: str
    content: str
    category: str
    priority: int
    source: str  # constitution, local, task-specific


@dataclass
class GateDecision:
    """Result of a guidance gate evaluation."""
    allowed: bool
    gate_name: str
    reason: str
    rule_ids: list[str] = field(default_factory=list)
    severity: str = "info"  # info, warning, error, critical
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    """Represents a tool call that must pass through the gateway."""
    tool_name: str
    arguments: dict[str, Any]
    caller: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    idempotency_key: str | None = None


@dataclass
class ProofChainEntry:
    """Single entry in the cryptographic proof chain."""
    index: int
    timestamp: datetime
    task_spec_hash: str
    guidance_shard_hashes: list[str]
    gate_decisions: list[GateDecision]
    verdict_result: Any | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    omniroute_routing: dict[str, Any] | None = None
    actual_provider: str | None = None
    actual_model: str | None = None
    fallback_history: list[dict[str, Any]] = field(default_factory=list)
    previous_hash: str = ""
    hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class GuidanceControlPlane:
    """
    Singleton Guidance Control Plane for Verdict Core.

    This is the mandatory runtime governance layer that all execution must flow through.
    It compiles CLAUDE.md rules, retrieves relevant shards, enforces gates, and records
    a complete proof chain for every execution.
    """

    _instance: GuidanceControlPlane | None = None
    _lock = asyncio.Lock()

    def __new__(cls, config: GuidanceConfig | None = None):
        if cls._instance is None:
            raise RuntimeError("GuidanceControlPlane must be initialized via initialize() first")
        return cls._instance

    @classmethod
    async def initialize(cls, config: GuidanceConfig | None = None) -> GuidanceControlPlane:
        """Initialize the singleton control plane."""
        async with cls._lock:
            if cls._instance is not None:
                return cls._instance

            if config is None:
                # Auto-detect repo root and config files
                repo_root = Path(__file__).parent.parent.parent
                config = GuidanceConfig(
                    repo_root=repo_root,
                    claude_md_path=repo_root / "CLAUDE.md",
                    claude_local_md_path=repo_root / "CLAUDE.local.md" if (repo_root / "CLAUDE.local.md").exists() else None
                )

            cls._instance = object.__new__(cls)
            await cls._instance._initialize_internal(config)
            return cls._instance

    @classmethod
    def get_instance(cls) -> GuidanceControlPlane:
        """Get the initialized singleton instance."""
        if cls._instance is None:
            raise RuntimeError("GuidanceControlPlane not initialized. Call initialize() first.")
        return cls._instance

    async def _initialize_internal(self, config: GuidanceConfig) -> None:
        """Internal initialization logic."""
        self.config = config
        self._policy_bundle: dict[str, Any] | None = None
        self._policy_bundle_hash: str | None = None
        self._policy_bundle_loaded_at: float | None = None
        self._initialized = False
        self._proof_chain: list[ProofChainEntry] = []

        # Initialize Verdict Core components
        self._eligibility_gate = EligibilityGate(
            availability_source=self._get_availability_cache_sync,
            protected_fail_closed=True,
            allow_unverified_in_dev=True
        )
        self._gate = Gate()

        # Load and compile policy bundle
        await self._load_policy_bundle()

        self._initialized = True

    async def _load_policy_bundle(self) -> None:
        """Load and compile CLAUDE.md + CLAUDE.local.md into a policy bundle."""
        constitution_content = self.config.claude_md_path.read_text(encoding="utf-8")
        local_content = ""
        if self.config.claude_local_md_path and self.config.claude_local_md_path.exists():
            local_content = self.config.claude_local_md_path.read_text(encoding="utf-8")

        # Compile into structured policy bundle
        self._policy_bundle = {
            "constitution": self._parse_constitution(constitution_content),
            "local_overlay": self._parse_local_overlay(local_content),
            "compiled_at": datetime.now(timezone.utc).isoformat(),
            "version": hashlib.sha256((constitution_content + local_content).encode()).hexdigest()[:16]
        }
        self._policy_bundle_hash = self._policy_bundle["version"]
        self._policy_bundle_loaded_at = time.time()

    def _parse_constitution(self, content: str) -> dict:
        """Parse CLAUDE.md constitution into structured rules."""
        # This is a simplified parser - the actual guidance package has a more sophisticated compiler
        rules = []
        current_section = "general"

        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("## "):
                current_section = line[3:].lower().replace(" ", "_")
            elif line.startswith("- ") or line.startswith("* "):
                rules.append({
                    "id": hashlib.md5(f"{current_section}:{line}".encode()).hexdigest()[:12],
                    "section": current_section,
                    "content": line[2:].strip(),
                    "source": "constitution",
                    "priority": self._get_section_priority(current_section)
                })

        return {"rules": rules, "source": "constitution"}

    def _parse_local_overlay(self, content: str) -> dict:
        """Parse CLAUDE.local.md overlay into structured rules."""
        rules = []
        current_section = "local"

        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("## "):
                current_section = line[3:].lower().replace(" ", "_")
            elif line.startswith("- ") or line.startswith("* "):
                rules.append({
                    "id": hashlib.md5(f"{current_section}:{line}".encode()).hexdigest()[:12],
                    "section": current_section,
                    "content": line[2:].strip(),
                    "source": "local",
                    "priority": 100  # Local rules have higher priority
                })

        return {"rules": rules, "source": "local"}

    def _get_section_priority(self, section: str) -> int:
        """Get priority for a constitution section."""
        priorities = {
            "core_rules": 10,
            "preserve_existing_work": 10,
            "source_of_truth_priority": 10,
            "retrieve_before_reasoning": 10,
            "mcp_responsibilities": 10,
            "code_review_graph_workflow": 9,
            "route_through_verdict": 9,
            "execution_architecture": 9,
            "agent_communication": 8,
            "model_roles": 8,
            "memory_and_learning": 7,
            "background_workers": 7,
            "generated_artifacts": 6,
            "verification": 6,
            "verdict_core_baseline": 5,
            "documentation_fallback_and_rag_ingestion": 5,
        }
        return priorities.get(section, 1)

    async def _get_availability_cache(self, key: str) -> Any | None:
        """Cache getter for availability adapter."""
        # This would integrate with verdict's availability cache
        return None

    def _get_availability_cache_sync(self, key: str) -> Any | None:
        """Synchronous cache getter for eligibility gate."""
        # This would integrate with verdict's availability cache
        return None

    # ==================== PHASE 3: Task Normalization ====================

    def normalize_task_spec(self, raw_input: dict[str, Any] | str) -> TaskSpec:
        """
        Normalize any execution request into a TaskSpec.

        This is the single source of truth - all execution flows through this.
        """
        if isinstance(raw_input, str):
            raw_input = {"goal": raw_input}

        return TaskSpec(
            goal=raw_input.get("goal", ""),
            affected_files=raw_input.get("affected_files", []),
            risk_level=raw_input.get("risk_level", "medium"),
            protected_work=raw_input.get("protected_work", False),
            capabilities=raw_input.get("capabilities", []),
            privacy_level=raw_input.get("privacy_level", "standard"),
            authorization_required=raw_input.get("authorization_required", False),
            quality_threshold=raw_input.get("quality_threshold", 0.8),
            verification_plan=raw_input.get("verification_plan", []),
            rollback_plan=raw_input.get("rollback_plan", ""),
            tool_intent=raw_input.get("tool_intent", ""),
            metadata=raw_input.get("metadata", {})
        )

    # ==================== PHASE 4: Guidance Retrieval ====================

    async def retrieve_guidance(self, task_spec: TaskSpec) -> list[GuidanceShard]:
        """
        Retrieve relevant guidance shards for a task using intent classification.

        Does NOT inject every rule - uses intent retrieval to select relevant shards.
        """
        if not self._policy_bundle:
            await self._load_policy_bundle()

        all_rules = []
        all_rules.extend(self._policy_bundle["constitution"]["rules"])
        all_rules.extend(self._policy_bundle["local_overlay"]["rules"])

        # Intent-based retrieval - match task goal/capabilities against rule content
        task_keywords = self._extract_keywords(task_spec.goal)
        task_keywords.update(self._extract_keywords(" ".join(task_spec.capabilities)))
        task_keywords.update(self._extract_keywords(" ".join(task_spec.affected_files)))

        scored_rules = []
        for rule in all_rules:
            score = self._score_relevance(rule, task_keywords, task_spec)
            if score > 0:
                scored_rules.append((score, rule))

        # Sort by score descending, then by priority descending
        scored_rules.sort(key=lambda x: (-x[0], -x[1]["priority"]))

        # Return top relevant shards (max 20 to avoid context pollution)
        shards: list[GuidanceShard] = []
        for _, rule in scored_rules[:20]:
            shards.append(GuidanceShard(
                rule_id=rule["id"],
                content=rule["content"],
                category=rule["section"],
                priority=rule["priority"],
                source=rule["source"]
            ))

        return shards

    def _extract_keywords(self, text: str) -> set[str]:
        """Extract meaningful keywords from text."""
        import re
        words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
        stopwords = {"the", "and", "for", "with", "from", "this", "that", "will", "should", "must", "can", "are", "use", "using", "used"}
        return {w for w in words if w not in stopwords}

    def _score_relevance(self, rule: dict[str, Any], keywords: set[str], task_spec: TaskSpec) -> float:
        """Score rule relevance to task."""
        rule_words = self._extract_keywords(rule["content"])
        overlap = len(keywords & rule_words)
        if overlap == 0:
            return 0.0

        # Boost for risk level match
        risk_boost = 0.0
        if task_spec.risk_level in ["high", "critical"] and "critical" in rule["content"].lower():
            risk_boost = 0.5
        if task_spec.protected_work and "protected" in rule["content"].lower():
            risk_boost = 1.0

        return overlap * 0.1 + risk_boost + rule["priority"] * 0.01

    # ==================== PHASE 5: Verdict Integration ====================

    async def evaluate_verdict_gates(self, task_spec: TaskSpec, guidance_shards: list[GuidanceShard]) -> list[GateDecision]:
        """
        Call Verdict Core for eligibility and routing decisions.

        Possible results: ALLOW, DENY, DEFER, FAIL_VISIBLE
        Rules: DENY stops execution, DEFER stops execution, FAIL_VISIBLE allows read-only investigation
        """
        decisions = []

        # Build context for Verdict
        context = {
            "task_spec": task_spec.__dict__,
            "guidance_shards": [s.__dict__ for s in guidance_shards],
            "policy_bundle_version": self._policy_bundle_hash,
        }

        # 1. Eligibility Gate (deterministic, fails closed)
        eligibility_result = await self._run_eligibility_gate(task_spec, context)
        decisions.append(GateDecision(
            allowed=eligibility_result.get("allowed", False),
            gate_name="eligibility",
            reason=eligibility_result.get("reason", ""),
            rule_ids=eligibility_result.get("rule_ids", []),
            severity="critical" if not eligibility_result.get("allowed", False) else "info",
            metadata=eligibility_result
        ))

        if not eligibility_result.get("allowed", False):
            return decisions  # DENY stops execution

        # 2. Privacy/Capability Gate
        privacy_result = await self._run_privacy_gate(task_spec, context)
        decisions.append(GateDecision(
            allowed=privacy_result.get("allowed", False),
            gate_name="privacy_capability",
            reason=privacy_result.get("reason", ""),
            rule_ids=privacy_result.get("rule_ids", []),
            severity="critical" if not privacy_result.get("allowed", False) else "info",
            metadata=privacy_result
        ))

        if not privacy_result.get("allowed", False):
            return decisions  # DENY stops execution

        # 3. Budget Gate
        budget_result = await self._run_budget_gate(task_spec, context)
        decisions.append(GateDecision(
            allowed=budget_result.get("allowed", False),
            gate_name="budget",
            reason=budget_result.get("reason", ""),
            rule_ids=budget_result.get("rule_ids", []),
            severity="critical" if not budget_result.get("allowed", False) else "info",
            metadata=budget_result
        ))

        if not budget_result.get("allowed", False):
            return decisions  # DENY stops execution

        return decisions

    async def _run_eligibility_gate(self, task_spec: TaskSpec, context: dict) -> dict:
        """Run Verdict's eligibility gate."""
        try:
            # Use Verdict's eligibility gate
            # This is a simplified version - actual implementation would call the real gate
            if task_spec.protected_work and not task_spec.authorization_required:
                return {
                    "allowed": False,
                    "reason": "Protected work requires explicit authorization",
                    "rule_ids": ["protected-work-auth"]
                }

            return {"allowed": True, "reason": "Eligibility check passed", "rule_ids": []}
        except Exception as e:
            return {"allowed": False, "reason": f"Eligibility gate error: {e}", "rule_ids": ["gate-error"]}

    async def _run_privacy_gate(self, task_spec: TaskSpec, context: dict) -> dict:
        """Run privacy and capability checks."""
        if task_spec.privacy_level == "restricted" and "admin" not in task_spec.capabilities:
            return {
                "allowed": False,
                "reason": "Restricted privacy level requires admin capability",
                "rule_ids": ["privacy-admin-required"]
            }
        return {"allowed": True, "reason": "Privacy check passed", "rule_ids": []}

    async def _run_budget_gate(self, task_spec: TaskSpec, context: dict) -> dict:
        """Run budget/token limit checks."""
        # This would integrate with actual budget tracking
        estimated_tokens = len(task_spec.goal) * 4 + sum(len(f) for f in task_spec.affected_files) * 10
        if estimated_tokens > 100000:  # Example threshold
            return {
                "allowed": False,
                "reason": f"Estimated token usage ({estimated_tokens}) exceeds budget",
                "rule_ids": ["budget-exceeded"]
            }
        return {"allowed": True, "reason": "Budget check passed", "rule_ids": []}

    # ==================== PHASE 6: Tool Gateway ====================

    async def gateway_tool_call(self, tool_call: ToolCall) -> tuple[bool, str, Any | None]:
        """
        Every tool call must pass through this gateway.

        Enforces: budgets, idempotency, schemas, protected commands
        """
        if not self.config.enable_tool_gateway:
            return True, "Gateway disabled", None

        # Check protected commands
        protected_result = self._check_protected_commands(tool_call)
        if not protected_result[0]:
            return protected_result

        # Check idempotency
        if tool_call.idempotency_key:
            idempotency_result = await self._check_idempotency(tool_call)
            if not idempotency_result[0]:
                return idempotency_result

        # Validate schema
        schema_result = self._validate_tool_schema(tool_call)
        if not schema_result[0]:
            return schema_result

        # Check budget
        budget_result = await self._check_tool_budget(tool_call)
        if not budget_result[0]:
            return budget_result

        # Record in proof chain
        self._record_tool_call(tool_call)

        return True, "Allowed", None

    def _check_protected_commands(self, tool_call: ToolCall) -> tuple[bool, str, Any | None]:
        """Check for protected commands that require special authorization."""
        protected_patterns = {
            "git": ["push --force", "reset --hard", "clean -f", "checkout --", "restore --"],
            "database": ["drop", "delete from", "truncate"],
            "deployment": ["deploy", "release", "promote"],
            "credentials": ["write", "store", "save"],
        }

        args_str = json.dumps(tool_call.arguments).lower()

        for category, patterns in protected_patterns.items():
            for pattern in patterns:
                if pattern in args_str and not tool_call.arguments.get("_force_allowed", False):
                    return False, f"Protected {category} command '{pattern}' requires explicit authorization", None

        return True, "Allowed", None

    async def _check_idempotency(self, tool_call: ToolCall) -> tuple[bool, str, Any | None]:
        """Check if this tool call was already executed (idempotency)."""
        # This would check against a persisted idempotency store
        # Simplified for now
        return True, "Idempotency check passed", None

    def _validate_tool_schema(self, tool_call: ToolCall) -> tuple[bool, str, Any | None]:
        """Validate tool arguments against expected schema."""
        # This would validate against known tool schemas
        # Simplified for now
        return True, "Schema validation passed", None

    async def _check_tool_budget(self, tool_call: ToolCall) -> tuple[bool, str, Any | None]:
        """Check if tool call fits within budget."""
        # This would track token/time/cost budgets
        return True, "Budget check passed", None

    def _record_tool_call(self, tool_call: ToolCall):
        """Record tool call in current proof chain entry."""
        if self._proof_chain:
            self._proof_chain[-1].tool_calls.append(tool_call)

    # ==================== PHASE 7: Git Protection ====================

    def check_git_operation(self, operation: str, args: list[str]) -> tuple[bool, str]:
        """
        Check if a git operation is allowed.

        Moves git safety policies from prompt text into runtime gates.
        """
        protected_operations = {
            "push": ["--force", "-f", "--force-with-lease"],
            "reset": ["--hard"],
            "clean": ["-f", "-fd", "-fdx"],
            "checkout": ["--"],
            "restore": ["--"],
        }

        if operation in protected_operations:
            for arg in args:
                if arg in protected_operations[operation]:
                    return False, f"Protected git operation '{operation} {arg}' requires explicit authorization via _force_allowed flag"

        return True, "Allowed"

    # ==================== PHASE 8: Continue Gate ====================

    class ContinueDecision:
        CONTINUE = "continue"
        CHECKPOINT = "checkpoint"
        THROTTLE = "throttle"
        PAUSE = "pause"
        STOP = "stop"

    async def evaluate_continue_gate(
        self,
        task_spec: TaskSpec,
        step_number: int,
        step_result: dict[str, Any],
        loop_detection_state: dict[str, Any]
    ) -> str:
        """
        Evaluate whether to continue, checkpoint, throttle, pause, or stop.

        Ties into Ruflo Completion Autopilot for loop detection.
        """
        if not self.config.enable_continue_gate:
            return self.ContinueDecision.CONTINUE

        # Loop detection
        if self._detect_loop(step_result, loop_detection_state):
            return self.ContinueDecision.STOP

        # Check for repeated failures
        if step_result.get("status") == "failed":
            failure_count = loop_detection_state.get("consecutive_failures", 0) + 1
            if failure_count >= 3:
                return self.ContinueDecision.STOP
            elif failure_count >= 2:
                return self.ContinueDecision.THROTTLE

        # Check step budget
        if step_number > 50:  # Max steps per task
            return self.ContinueDecision.STOP
        elif step_number > 40:
            return self.ContinueDecision.THROTTLE
        elif step_number % 10 == 0:
            return self.ContinueDecision.CHECKPOINT

        return self.ContinueDecision.CONTINUE

    def _detect_loop(self, step_result: dict, state: dict) -> bool:
        """Detect if we're in a loop repeating the same approach."""
        action_signature = step_result.get("action_signature", "")
        if not action_signature:
            return False

        recent_actions = state.get("recent_actions", [])
        if action_signature in recent_actions[-5:]:  # Same action in last 5 steps
            return True

        recent_actions.append(action_signature)
        state["recent_actions"] = recent_actions[-10:]  # Keep last 10
        return False

    # ==================== PHASE 9: Memory Governance ====================

    async def govern_memory_write(
        self,
        namespace: str,
        key: str,
        value: Any,
        authority: str,
        ttl_seconds: int | None = None,
        confidence: float = 1.0,
        provenance: dict | None = None
    ) -> tuple[bool, str]:
        """
        Govern all memory writes through the control plane.

        Requires: authority, TTL, contradiction detection, confidence, provenance
        """
        if not self.config.enable_memory_gate:
            return True, "Memory gate disabled"

        # Check authority
        if not self._verify_authority(authority, namespace):
            return False, f"Authority '{authority}' not authorized for namespace '{namespace}'"

        # Check TTL
        if ttl_seconds is None:
            ttl_seconds = 86400 * 30  # Default 30 days
        if ttl_seconds > 86400 * 365:  # Max 1 year
            return False, "TTL exceeds maximum allowed (1 year)"

        # Contradiction detection
        contradiction = await self._detect_contradiction(namespace, key, value)
        if contradiction:
            return False, f"Contradiction detected with existing memory: {contradiction}"

        # Confidence threshold
        if confidence < 0.5:
            return False, f"Confidence {confidence} below minimum threshold (0.5)"

        # Provenance required
        if provenance is None:
            return False, "Provenance metadata required for memory writes"

        # Record in proof chain
        if self._proof_chain:
            self._proof_chain[-1].metadata["memory_write"] = {
                "namespace": namespace,
                "key": key,
                "authority": authority,
                "ttl": ttl_seconds,
                "confidence": confidence,
                "provenance": provenance
            }

        return True, "Allowed"

    def _verify_authority(self, authority: str, namespace: str) -> bool:
        """Verify authority can write to namespace."""
        # This would check against an authority registry
        valid_authorities = ["system", "verdict-core", "ruflo", "omniroute", "human"]
        return authority in valid_authorities

    async def _detect_contradiction(self, namespace: str, key: str, value: Any) -> str | None:
        """Detect if new value contradicts existing memory."""
        # This would query the actual memory store
        # Simplified for now
        return None

    # ==================== PHASE 10: Proof Chain ====================

    def start_proof_chain_entry(self, task_spec: TaskSpec, guidance_shards: list[GuidanceShard]) -> ProofChainEntry:
        """Start a new proof chain entry for a task execution."""
        entry = ProofChainEntry(
            index=len(self._proof_chain),
            timestamp=datetime.now(timezone.utc),
            task_spec_hash=hashlib.sha256(json.dumps(task_spec.__dict__, sort_keys=True, default=str).encode()).hexdigest()[:32],
            guidance_shard_hashes=[hashlib.sha256(s.content.encode()).hexdigest()[:16] for s in guidance_shards],
            gate_decisions=[],
            previous_hash=self._proof_chain[-1].hash if self._proof_chain else "0" * 64
        )
        entry.hash = self._compute_entry_hash(entry)
        self._proof_chain.append(entry)
        return entry

    def record_gate_decisions(self, entry: ProofChainEntry, decisions: list[GateDecision]):
        """Record gate decisions in proof chain entry."""
        entry.gate_decisions.extend(decisions)
        entry.hash = self._compute_entry_hash(entry)

    def record_verdict_result(self, entry: ProofChainEntry, verdict_result: Any):
        """Record Verdict Core routing result."""
        entry.verdict_result = verdict_result
        entry.hash = self._compute_entry_hash(entry)

    def record_omniroute_routing(self, entry: ProofChainEntry, routing: dict[str, Any]):
        """Record OmniRoute routing decision."""
        entry.omniroute_routing = routing
        entry.hash = self._compute_entry_hash(entry)

    def record_provider_execution(self, entry: ProofChainEntry, provider: str, model: str, fallback_history: list[dict] | None = None):
        """Record actual provider/model used and fallback history."""
        entry.actual_provider = provider
        entry.actual_model = model
        if fallback_history:
            entry.fallback_history = fallback_history
        entry.hash = self._compute_entry_hash(entry)

    def _compute_entry_hash(self, entry: ProofChainEntry) -> str:
        """Compute cryptographic hash of proof chain entry."""
        data = {
            "index": entry.index,
            "timestamp": entry.timestamp.isoformat(),
            "task_spec_hash": entry.task_spec_hash,
            "guidance_shard_hashes": entry.guidance_shard_hashes,
            "gate_decisions": [d.__dict__ for d in entry.gate_decisions],
            "verdict_result": entry.verdict_result,
            "tool_calls": [t.__dict__ for t in entry.tool_calls],
            "omniroute_routing": entry.omniroute_routing,
            "actual_provider": entry.actual_provider,
            "actual_model": entry.actual_model,
            "fallback_history": entry.fallback_history,
            "previous_hash": entry.previous_hash
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()

    def get_proof_chain(self, limit: int | None = None) -> list[ProofChainEntry]:
        """Get the proof chain (for audit/export)."""
        if limit:
            return self._proof_chain[-limit:]
        return self._proof_chain

    def export_proof_chain(self, path: Path):
        """Export proof chain to file for audit."""
        data = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "policy_bundle_version": self._policy_bundle_hash,
            "entries": [
                {
                    "index": e.index,
                    "timestamp": e.timestamp.isoformat(),
                    "task_spec_hash": e.task_spec_hash,
                    "guidance_shard_hashes": e.guidance_shard_hashes,
                    "gate_decisions": [
                        {
                            "allowed": d.allowed,
                            "gate_name": d.gate_name,
                            "reason": d.reason,
                            "rule_ids": d.rule_ids,
                            "severity": d.severity
                        } for d in e.gate_decisions
                    ],
                    "verdict_result": e.verdict_result,
                    "tool_calls_count": len(e.tool_calls),
                    "omniroute_routing": e.omniroute_routing,
                    "actual_provider": e.actual_provider,
                    "actual_model": e.actual_model,
                    "fallback_history": e.fallback_history,
                    "previous_hash": e.previous_hash,
                    "hash": e.hash
                } for e in self._proof_chain
            ]
        }
        path.write_text(json.dumps(data, indent=2))

    # ==================== PHASE 11: Execution Pipeline ====================

    async def execute_pipeline(self, raw_input: dict[str, Any] | str) -> dict[str, Any]:
        """
        Execute the full pipeline:
        Normalize TaskSpec → Retrieve Guidance → Guidance Gates → Verdict → Tool Gateway → Ruflo → Workers → OmniRoute → Execution → Verification → Proof Chain → Learning
        """
        # Step 1: Normalize TaskSpec
        task_spec = self.normalize_task_spec(raw_input)

        # Step 2: Retrieve Guidance
        guidance_shards = await self.retrieve_guidance(task_spec)

        # Step 3: Start proof chain entry
        proof_entry = self.start_proof_chain_entry(task_spec, guidance_shards)

        # Step 4: Guidance Gates (including Verdict)
        gate_decisions = await self.evaluate_verdict_gates(task_spec, guidance_shards)
        self.record_gate_decisions(proof_entry, gate_decisions)

        # Check if any gate denied
        denied = [d for d in gate_decisions if not d.allowed]
        if denied:
            return {
                "status": "denied",
                "task_spec": task_spec.__dict__,
                "denied_by": denied[0].gate_name,
                "reason": denied[0].reason,
                "gate_decisions": [d.__dict__ for d in gate_decisions],
                "proof_chain_entry": proof_entry.__dict__
            }

        # Step 5: Tool Gateway would be used during execution
        # (actual tool calls go through gateway_tool_call)

        # Step 6: Ruflo Routing / Worker Dispatch
        # This would integrate with actual Ruflo orchestration
        ruflo_result = await self._dispatch_to_ruflo(task_spec)

        # Step 7: OmniRoute / Provider Selection
        omniroute_result = await self._route_via_omniroute(task_spec, ruflo_result)
        self.record_omniroute_routing(proof_entry, omniroute_result)

        # Step 8: Execution
        execution_result = await self._execute_with_provider(task_spec, omniroute_result)

        # Step 9: Record provider details
        self.record_provider_execution(
            proof_entry,
            execution_result.get("provider", "unknown"),
            execution_result.get("model", "unknown"),
            execution_result.get("fallback_history", [])
        )

        # Step 10: Verification
        verification_result = await self._verify_execution(task_spec, execution_result)

        # Step 11: Learning (record patterns)
        await self._record_learning(task_spec, execution_result, verification_result)

        return {
            "status": "completed" if verification_result.get("passed", False) else "verification_failed",
            "task_spec": task_spec.__dict__,
            "guidance_shards": [s.__dict__ for s in guidance_shards],
            "gate_decisions": [d.__dict__ for d in gate_decisions],
            "ruflo_result": ruflo_result,
            "omniroute_result": omniroute_result,
            "execution_result": execution_result,
            "verification_result": verification_result,
            "proof_chain_entry": proof_entry.__dict__
        }

    async def _dispatch_to_ruflo(self, task_spec: TaskSpec) -> dict:
        """Dispatch task to Ruflo orchestration."""
        # This would call actual Ruflo adapter
        return {
            "dispatched": True,
            "workers": ["researcher", "architect", "coder", "tester", "reviewer"],
            "topology": "hierarchical",
            "task_id": task_spec.task_id
        }

    async def _route_via_omniroute(self, task_spec: TaskSpec, ruflo_result: dict) -> dict:
        """Route via OmniRoute for model/provider selection."""
        # This would call actual OmniRoute
        return {
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "reasoning": "Task requires complex reasoning",
            "cost_estimate_usd": 0.015,
            "fallback": ["openai:gpt-4o", "google:gemini-1.5-pro"]
        }

    async def _execute_with_provider(self, task_spec: TaskSpec, routing: dict) -> dict:
        """Execute task with selected provider."""
        # This would execute via the actual provider
        return {
            "success": True,
            "output": "Task executed successfully",
            "provider": routing["provider"],
            "model": routing["model"],
            "tokens_used": 1500,
            "cost_usd": 0.012,
            "fallback_history": []
        }

    async def _verify_execution(self, task_spec: TaskSpec, execution_result: dict) -> dict:
        """Verify execution against quality threshold."""
        # Run verification checks
        checks = {
            "output_exists": bool(execution_result.get("output")),
            "no_errors": execution_result.get("success", False),
            "within_budget": execution_result.get("cost_usd", 0) < 0.10,
            "quality_threshold": True  # Would run actual quality checks
        }

        passed = all(checks.values())
        return {"passed": passed, "checks": checks}

    async def _record_learning(self, task_spec: TaskSpec, execution_result: dict, verification_result: dict):
        """Record learning patterns for future optimization."""
        # This would store patterns in Ruflo memory
        pass


async def initialize_guidance_control_plane(
    repo_root: Path | None = None,
    claude_md_path: Path | None = None,
    claude_local_md_path: Path | None = None
) -> GuidanceControlPlane:
    """Convenience function to initialize the guidance control plane."""
    if repo_root is None:
        repo_root = Path(__file__).parent.parent.parent

    config = GuidanceConfig(
        repo_root=repo_root,
        claude_md_path=claude_md_path or repo_root / "CLAUDE.md",
        claude_local_md_path=claude_local_md_path or (repo_root / "CLAUDE.local.md" if (repo_root / "CLAUDE.local.md").exists() else None)
    )

    return await GuidanceControlPlane.initialize(config)


# Export for use in Verdict Core
__all__ = [
    "GateDecision",
    "GuidanceConfig",
    "GuidanceControlPlane",
    "GuidanceShard",
    "ProofChainEntry",
    "TaskSpec",
    "ToolCall",
    "initialize_guidance_control_plane",
]
