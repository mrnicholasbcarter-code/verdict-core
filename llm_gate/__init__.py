"""llm-gate: Route LLM tasks by criticality.

Never send prod code to a cheap model. Never burn $20/hr on formatting.
"""

from llm_gate.availability import (
    AvailabilityCandidate,
    AvailabilityReport,
    AvailabilityState,
    CandidateRequirements,
    OmniRouteAvailabilityAdapter,
    OmniRouteTransport,
    RuntimeObservation,
    StaticOmniRouteTransport,
)
from llm_gate.contracts import (
    AvailabilitySnapshot,
    CapabilityRequirement,
    ContractValidationError,
    FallbackAttempt,
    LearningEvent,
    OutcomeEvent,
    RoutingDecisionContract,
    RuntimeCandidate,
    TaskSpec,
    VerificationPlan,
    WorkflowPlan,
)
from llm_gate.gate import Gate
from llm_gate.intelligence import IntelligenceService, ReadinessReport
from llm_gate.models import ModelInfo, ProviderConfig, RoutingDecision

__all__ = [
    "AvailabilityCandidate",
    "AvailabilityReport",
    "AvailabilitySnapshot",
    "AvailabilityState",
    "CandidateRequirements",
    "CapabilityRequirement",
    "ContractValidationError",
    "FallbackAttempt",
    "Gate",
    "IntelligenceService",
    "LearningEvent",
    "ModelInfo",
    "OmniRouteAvailabilityAdapter",
    "OmniRouteTransport",
    "OutcomeEvent",
    "ProviderConfig",
    "ReadinessReport",
    "RoutingDecision",
    "RoutingDecisionContract",
    "RuntimeCandidate",
    "RuntimeObservation",
    "StaticOmniRouteTransport",
    "TaskSpec",
    "VerificationPlan",
    "WorkflowPlan",
]
__version__ = "0.1.0"
