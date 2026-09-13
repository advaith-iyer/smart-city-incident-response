from dataclasses import dataclass, field
from typing import Any


@dataclass
class Incident:
    """
    Common incident format used by the Orchestrator
    and all response modules.
    """

    incident_id: str
    incident_type: str
    description: str
    location: dict[str, Any]
    severity_inputs: dict[str, Any] = field(default_factory=dict)
    reporter: str | None = None
    timestamp: str | None = None
    confidence: float = 1.0
    status: str = "reported"


@dataclass
class ModuleOutput:
    """
    Common response format returned by every module.
    """

    incident_id: str
    severity: dict[str, Any]
    actions_taken: list[dict[str, Any]]
    resources_allocated: list[dict[str, Any]]
    escalation_level: str
    status: str
    reporter_updates: list[str]
    log: dict[str, Any]