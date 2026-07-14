"""llm-gate: Route LLM tasks by criticality.

Never send prod code to a cheap model. Never burn $20/hr on formatting.
"""

from llm_gate.gate import Gate
from llm_gate.intelligence import IntelligenceService, ReadinessReport
from llm_gate.models import ModelInfo, ProviderConfig, RoutingDecision

__all__ = [
    "Gate",
    "IntelligenceService",
    "ModelInfo",
    "ProviderConfig",
    "ReadinessReport",
    "RoutingDecision",
]
__version__ = "0.1.0"
