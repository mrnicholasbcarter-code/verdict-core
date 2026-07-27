"""
Memory Gate for Verdict Core Guidance Control Plane

This module governs all memory writes through the Guidance Control Plane.
Every memory write requires:
- Authority verification
- TTL (Time-To-Live)
- Contradiction detection
- Confidence threshold
- Provenance metadata
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from verdict.guidance_control_plane import GuidanceControlPlane


class AuthorityLevel(Enum):
    """Authority levels for memory writes."""
    SYSTEM = "system"           # Core system components
    VERDICT_CORE = "verdict-core"  # Verdict Core components
    RUFLO = "ruflo"             # Ruflo orchestration
    OMNIROUTE = "omniroute"     # OmniRoute routing
    HUMAN = "human"             # Human operators
    AGENT = "agent"             # AI agents (lowest)


class MemoryNamespace(Enum):
    """Known memory namespaces with their access policies."""
    PATTERNS = "patterns"           # Learned patterns - agents can write with authority
    TRAJECTORIES = "trajectories"   # Execution trajectories - agents write
    DECISIONS = "decisions"         # Routing decisions - system writes
    FEEDBACK = "feedback"           # Human feedback - human writes
    CONFIG = "config"               # Configuration - system only
    EVIDENCE = "evidence"           # Verification evidence - system writes
    AUDIT = "audit"                 # Audit logs - system only


@dataclass
class MemoryWriteRequest:
    """Request to write to memory."""
    namespace: str
    key: str
    value: Any
    authority: str
    authority_level: AuthorityLevel
    ttl_seconds: int | None = None
    confidence: float = 1.0
    provenance: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)
    supersedes: str | None = None  # Key this write supersedes


@dataclass
class MemoryWriteResult:
    """Result of a memory write attempt."""
    allowed: bool
    reason: str
    write_id: str | None = None
    contradiction_detected: bool = False
    contradiction_details: str | None = None
    authority_verified: bool = False
    ttl_set: int | None = None


@dataclass
class MemoryPolicy:
    """Policy for a memory namespace."""
    namespace: str
    min_authority_level: AuthorityLevel
    max_ttl_seconds: int
    default_ttl_seconds: int
    min_confidence: float
    requires_provenance: bool
    allowed_tags: list[str] = field(default_factory=list)
    forbidden_tags: list[str] = field(default_factory=list)
    max_key_length: int = 256
    max_value_size_bytes: int = 1024 * 1024  # 1MB default


class MemoryGate:
    """
    Memory Gate - governs all memory writes through the Guidance Control Plane.

    Every memory write must pass through this gate which enforces:
    - Authority verification
    - TTL limits
    - Contradiction detection
    - Confidence thresholds
    - Provenance requirements
    """

    def __init__(self, guidance_plane: GuidanceControlPlane):
        self.guidance = guidance_plane
        self._policies: dict[str, MemoryPolicy] = {}
        self._authorities: dict[str, AuthorityLevel] = {}
        self._write_history: list[MemoryWriteRequest] = []
        self._contradiction_cache: dict[str, dict] = {}

        self._setup_default_policies()
        self._setup_default_authorities()

    def _setup_default_policies(self):
        """Set up default memory namespace policies."""

        # Patterns - agents can write learned patterns
        self._policies["patterns"] = MemoryPolicy(
            namespace="patterns",
            min_authority_level=AuthorityLevel.AGENT,
            max_ttl_seconds=86400 * 90,  # 90 days
            default_ttl_seconds=86400 * 30,  # 30 days
            min_confidence=0.6,
            requires_provenance=True,
            allowed_tags=["learned", "verified", "deprecated", "experimental"]
        )

        # Trajectories - execution records
        self._policies["trajectories"] = MemoryPolicy(
            namespace="trajectories",
            min_authority_level=AuthorityLevel.AGENT,
            max_ttl_seconds=86400 * 7,  # 7 days
            default_ttl_seconds=86400 * 3,  # 3 days
            min_confidence=0.5,
            requires_provenance=True,
            allowed_tags=["success", "failure", "partial", "replay"]
        )

        # Decisions - routing/eligibility decisions
        self._policies["decisions"] = MemoryPolicy(
            namespace="decisions",
            min_authority_level=AuthorityLevel.VERDICT_CORE,
            max_ttl_seconds=86400 * 365,  # 1 year
            default_ttl_seconds=86400 * 90,  # 90 days
            min_confidence=0.8,
            requires_provenance=True,
            allowed_tags=["routing", "eligibility", "fallback", "override"]
        )

        # Feedback - human feedback
        self._policies["feedback"] = MemoryPolicy(
            namespace="feedback",
            min_authority_level=AuthorityLevel.HUMAN,
            max_ttl_seconds=86400 * 365,  # 1 year
            default_ttl_seconds=86400 * 30,  # 30 days
            min_confidence=0.9,
            requires_provenance=True,
            allowed_tags=["positive", "negative", "correction", "preference"]
        )

        # Config - system configuration
        self._policies["config"] = MemoryPolicy(
            namespace="config",
            min_authority_level=AuthorityLevel.SYSTEM,
            max_ttl_seconds=86400 * 365,  # 1 year
            default_ttl_seconds=86400 * 365,
            min_confidence=1.0,
            requires_provenance=True,
            allowed_tags=["runtime", "feature-flag", "experiment"]
        )

        # Evidence - verification evidence
        self._policies["evidence"] = MemoryPolicy(
            namespace="evidence",
            min_authority_level=AuthorityLevel.SYSTEM,
            max_ttl_seconds=86400 * 365,  # 1 year
            default_ttl_seconds=86400 * 30,  # 30 days
            min_confidence=0.95,
            requires_provenance=True,
            allowed_tags=["verification", "test-result", "benchmark", "audit"]
        )

        # Audit - audit logs
        self._policies["audit"] = MemoryPolicy(
            namespace="audit",
            min_authority_level=AuthorityLevel.SYSTEM,
            max_ttl_seconds=86400 * 365 * 7,  # 7 years
            default_ttl_seconds=86400 * 365 * 7,
            min_confidence=1.0,
            requires_provenance=True,
            allowed_tags=["access", "change", "security", "compliance"]
        )

    def _setup_default_authorities(self):
        """Set up default authority mappings."""
        self._authorities = {
            "system": AuthorityLevel.SYSTEM,
            "verdict-core": AuthorityLevel.VERDICT_CORE,
            "ruflo": AuthorityLevel.RUFLO,
            "omniroute": AuthorityLevel.OMNIROUTE,
            "human": AuthorityLevel.HUMAN,
            "agent": AuthorityLevel.AGENT,
            "coder": AuthorityLevel.AGENT,
            "architect": AuthorityLevel.AGENT,
            "tester": AuthorityLevel.AGENT,
            "reviewer": AuthorityLevel.AGENT,
            "researcher": AuthorityLevel.AGENT,
            "security-auditor": AuthorityLevel.AGENT,
            "performance-engineer": AuthorityLevel.AGENT,
        }

    def register_policy(self, policy: MemoryPolicy):
        """Register a custom memory policy."""
        self._policies[policy.namespace] = policy

    def register_authority(self, authority_id: str, level: AuthorityLevel):
        """Register an authority with its level."""
        self._authorities[authority_id] = level

    def _get_authority_level(self, authority: str) -> AuthorityLevel:
        """Get authority level for an authority ID."""
        return self._authorities.get(authority, AuthorityLevel.AGENT)

    def _get_policy(self, namespace: str) -> MemoryPolicy:
        """Get policy for a namespace, with defaults."""
        return self._policies.get(namespace, MemoryPolicy(
            namespace=namespace,
            min_authority_level=AuthorityLevel.AGENT,
            max_ttl_seconds=86400 * 30,
            default_ttl_seconds=86400 * 7,
            min_confidence=0.5,
            requires_provenance=True
        ))

    async def evaluate_write(self, request: MemoryWriteRequest) -> MemoryWriteResult:
        """
        Evaluate a memory write request against all policies.

        This is the main entry point - all memory writes must call this.
        """
        # 1. Verify authority
        authority_level = self._get_authority_level(request.authority)
        policy = self._get_policy(request.namespace)

        if not self._check_authority(authority_level, policy.min_authority_level):
            return MemoryWriteResult(
                allowed=False,
                reason=f"Authority '{request.authority}' (level: {authority_level.value}) insufficient for namespace '{request.namespace}' (requires: {policy.min_authority_level.value})",
                authority_verified=False
            )

        # 2. Check TTL
        ttl = request.ttl_seconds or policy.default_ttl_seconds
        if ttl > policy.max_ttl_seconds:
            return MemoryWriteResult(
                allowed=False,
                reason=f"TTL {ttl}s exceeds maximum {policy.max_ttl_seconds}s for namespace '{request.namespace}'",
                authority_verified=True
            )

        # 3. Check confidence
        if request.confidence < policy.min_confidence:
            return MemoryWriteResult(
                allowed=False,
                reason=f"Confidence {request.confidence} below minimum {policy.min_confidence} for namespace '{request.namespace}'",
                authority_verified=True
            )

        # 4. Check provenance
        if policy.requires_provenance and not request.provenance:
            return MemoryWriteResult(
                allowed=False,
                reason=f"Provenance required for namespace '{request.namespace}'",
                authority_verified=True
            )

        # 5. Check key length
        if len(request.key) > policy.max_key_length:
            return MemoryWriteResult(
                allowed=False,
                reason=f"Key length {len(request.key)} exceeds maximum {policy.max_key_length}",
                authority_verified=True
            )

        # 6. Check value size
        value_size = len(json.dumps(request.value).encode('utf-8'))
        if value_size > policy.max_value_size_bytes:
            return MemoryWriteResult(
                allowed=False,
                reason=f"Value size {value_size} bytes exceeds maximum {policy.max_value_size_bytes} bytes",
                authority_verified=True
            )

        # 7. Check tags
        for tag in request.tags:
            if tag in policy.forbidden_tags:
                return MemoryWriteResult(
                    allowed=False,
                    reason=f"Tag '{tag}' is forbidden for namespace '{request.namespace}'",
                    authority_verified=True
                )

        # 8. Contradiction detection
        contradiction = await self._detect_contradiction(request)
        if contradiction:
            return MemoryWriteResult(
                allowed=False,
                reason=f"Contradiction detected: {contradiction}",
                authority_verified=True,
                contradiction_detected=True,
                contradiction_details=contradiction
            )

        # 9. All checks passed - generate write ID
        write_id = self._generate_write_id(request)

        return MemoryWriteResult(
            allowed=True,
            reason="All checks passed",
            write_id=write_id,
            authority_verified=True,
            ttl_set=ttl
        )

    def _check_authority(self, actual: AuthorityLevel, required: AuthorityLevel) -> bool:
        """Check if actual authority level meets required level."""
        hierarchy = {
            AuthorityLevel.SYSTEM: 6,
            AuthorityLevel.VERDICT_CORE: 5,
            AuthorityLevel.RUFLO: 4,
            AuthorityLevel.OMNIROUTE: 3,
            AuthorityLevel.HUMAN: 2,
            AuthorityLevel.AGENT: 1
        }
        return hierarchy[actual] >= hierarchy[required]

    async def _detect_contradiction(self, request: MemoryWriteRequest) -> str | None:
        """
        Detect if the new value contradicts existing memory.

        This is a simplified implementation - a full version would query
        the actual memory store and use semantic comparison.
        """
        # Check cache first
        cache_key = f"{request.namespace}:{request.key}"
        if cache_key in self._contradiction_cache:
            existing = self._contradiction_cache[cache_key]
            if self._values_contradict(existing, request.value):
                return f"Contradicts existing value for {cache_key}"

        # For patterns namespace, check for contradictory learned patterns
        if request.namespace == "patterns" and isinstance(request.value, dict):
            if "pattern" in request.value and "confidence" in request.value:
                # Check if we have a similar pattern with different outcome
                pass  # Would query actual memory store

        # For decisions, check for contradictory routing decisions
        if request.namespace == "decisions" and isinstance(request.value, dict):
            if "decision" in request.value:
                # Check for contradictory decisions for same task
                pass

        # Add to cache for future checks
        self._contradiction_cache[cache_key] = request.value

        return None

    def _values_contradict(self, existing: Any, new: Any) -> bool:
        """Check if two values contradict each other."""
        # Simplified - would use semantic comparison in production
        if isinstance(existing, dict) and isinstance(new, dict):
            # Check for contradictory fields
            for key in set(existing.keys()) & set(new.keys()):
                if existing[key] != new[key] and key in ["decision", "verdict", "outcome", "result"]:
                    return True
        return False

    def _generate_write_id(self, request: MemoryWriteRequest) -> str:
        """Generate unique write ID."""
        data = f"{request.namespace}:{request.key}:{request.authority}:{time.time()}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    async def execute_write(self, request: MemoryWriteRequest) -> MemoryWriteResult:
        """
        Execute a memory write after evaluation.

        This evaluates then records in proof chain.
        """
        result = await self.evaluate_write(request)

        if result.allowed:
            # Record in write history
            self._write_history.append(request)

            # Record in proof chain if active
            if self.guidance._proof_chain:
                current_entry = self.guidance._proof_chain[-1]
                current_entry.metadata.setdefault("memory_writes", []).append({
                    "write_id": result.write_id,
                    "namespace": request.namespace,
                    "key": request.key,
                    "authority": request.authority,
                    "authority_level": request.authority_level.value,
                    "ttl_seconds": result.ttl_set,
                    "confidence": request.confidence,
                    "provenance": request.provenance,
                    "tags": request.tags,
                    "supersedes": request.supersedes
                })
                # Recompute hash
                current_entry.hash = self.guidance._compute_entry_hash(current_entry)

        return result

    def get_write_history(self, namespace: str | None = None, limit: int = 100) -> list[MemoryWriteRequest]:
        """Get recent write history."""
        history = self._write_history
        if namespace:
            history = [w for w in history if w.namespace == namespace]
        return history[-limit:]

    def get_policy(self, namespace: str) -> MemoryPolicy:
        """Get policy for a namespace."""
        return self._get_policy(namespace)

    def list_policies(self) -> dict[str, MemoryPolicy]:
        """List all registered policies."""
        return self._policies.copy()

    def list_authorities(self) -> dict[str, AuthorityLevel]:
        """List all registered authorities."""
        return self._authorities.copy()


class MemoryGovernor:
    """
    High-level memory governor that coordinates with Guidance Control Plane
    and provides simplified interface for common operations.
    """

    def __init__(self, guidance_plane: GuidanceControlPlane):
        self.gate = MemoryGate(guidance_plane)

    async def write_pattern(
        self,
        pattern_name: str,
        pattern_data: dict,
        authority: str,
        confidence: float = 0.8,
        ttl_days: int = 30,
        tags: list[str] = None,
        provenance: dict = None
    ) -> MemoryWriteResult:
        """Write a learned pattern."""
        return await self.gate.execute_write(MemoryWriteRequest(
            namespace="patterns",
            key=pattern_name,
            value=pattern_data,
            authority=authority,
            authority_level=self.gate._get_authority_level(authority),
            ttl_seconds=ttl_days * 86400,
            confidence=confidence,
            provenance=provenance or {"source": authority, "timestamp": datetime.now(timezone.utc).isoformat()},
            tags=tags or ["learned"]
        ))

    async def write_trajectory(
        self,
        trajectory_id: str,
        trajectory_data: dict,
        authority: str,
        outcome: str,  # success, failure, partial
        confidence: float = 0.9,
        provenance: dict = None
    ) -> MemoryWriteResult:
        """Write an execution trajectory."""
        return await self.gate.execute_write(MemoryWriteRequest(
            namespace="trajectories",
            key=trajectory_id,
            value=trajectory_data,
            authority=authority,
            authority_level=self.gate._get_authority_level(authority),
            ttl_seconds=86400 * 3,
            confidence=confidence,
            provenance=provenance or {"source": authority, "timestamp": datetime.now(timezone.utc).isoformat()},
            tags=[outcome]
        ))

    async def write_decision(
        self,
        decision_id: str,
        decision_data: dict,
        authority: str,
        confidence: float = 0.95,
        provenance: dict = None
    ) -> MemoryWriteResult:
        """Write a routing/eligibility decision."""
        return await self.gate.execute_write(MemoryWriteRequest(
            namespace="decisions",
            key=decision_id,
            value=decision_data,
            authority=authority,
            authority_level=self.gate._get_authority_level(authority),
            ttl_seconds=86400 * 90,
            confidence=confidence,
            provenance=provenance or {"source": authority, "timestamp": datetime.now(timezone.utc).isoformat()},
            tags=["routing"]
        ))

    async def write_feedback(
        self,
        feedback_id: str,
        feedback_data: dict,
        authority: str = "human",
        confidence: float = 1.0,
        provenance: dict = None
    ) -> MemoryWriteResult:
        """Write human feedback."""
        return await self.gate.execute_write(MemoryWriteRequest(
            namespace="feedback",
            key=feedback_id,
            value=feedback_data,
            authority=authority,
            authority_level=AuthorityLevel.HUMAN,
            ttl_seconds=86400 * 30,
            confidence=confidence,
            provenance=provenance or {"source": authority, "timestamp": datetime.now(timezone.utc).isoformat()},
            tags=["correction"]
        ))

    async def write_evidence(
        self,
        evidence_id: str,
        evidence_data: dict,
        authority: str,
        confidence: float = 0.95,
        provenance: dict = None
    ) -> MemoryWriteResult:
        """Write verification evidence."""
        return await self.gate.execute_write(MemoryWriteRequest(
            namespace="evidence",
            key=evidence_id,
            value=evidence_data,
            authority=authority,
            authority_level=self.gate._get_authority_level(authority),
            ttl_seconds=86400 * 30,
            confidence=confidence,
            provenance=provenance or {"source": authority, "timestamp": datetime.now(timezone.utc).isoformat()},
            tags=["verification"]
        ))


__all__ = [
    "AuthorityLevel",
    "MemoryGate",
    "MemoryGovernor",
    "MemoryNamespace",
    "MemoryPolicy",
    "MemoryWriteRequest",
    "MemoryWriteResult",
]
