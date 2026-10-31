from shared.contracts import Incident, ModuleOutput
from orchestrator.cascade import generate_cascades

MODULE_MAP = {
    "road_blockage": "traffic",
    "accident": "traffic",
    "flooding": "traffic",
    "medical": "medical",
    "injury": "medical",
    "fire": "fire",
    "industrial_fire": "fire",
    "factory": "factory",
}

MULTI_MODULE_RULES = {
    "requires_fire": "fire",
    "requires_medical": "medical",
    "requires_factory": "factory",
    "requires_traffic": "traffic",
}

def route_incident(incident: Incident) -> str | None:
    """
    Determine which response module should handle an incident.
    """

    return MODULE_MAP.get(incident.incident_type)


def dispatch_to_module(incident: Incident, world: dict):
    """
    Dispatch an incident to its primary module and any additional
    modules requested through severity_inputs.
    """

    primary_module = route_incident(incident)
    if is_false_report(incident):
        return create_false_report_response(incident)

    modules = []

    if primary_module:
        modules.append(primary_module)

    for flag, module_name in MULTI_MODULE_RULES.items():
        if incident.severity_inputs.get(flag, False):
            if module_name not in modules:
                modules.append(module_name)

    if not modules:
        return None

    results = []

    for module_name in modules:
        if module_name == "traffic":
            from modules.traffic.traffic_module import handle_incident
            result = handle_incident(incident, world)

        elif module_name == "medical":
            from modules.medical.medical_module import handle_incident
            result = handle_incident(incident, world)

        elif module_name == "fire":
            from modules.fire.fire_module import handle_incident
            result = handle_incident(incident, world)

        elif module_name == "factory":
            from modules.factory.factory_module import handle_incident
            result = handle_incident(incident, world)

        else:
            continue

        results.append(result)

    # Normal single-module incident
    if len(results) == 1:
        return results[0]

    # Multi-module incident
    return results


def create_unhandled_response(incident: Incident) -> dict:
    """
    Create a response when no module can handle the incident.
    """

    return {
        "incident_id": incident.incident_id,
        "status": "unhandled",
        "message": (
            f"No response module is registered for "
            f"incident type '{incident.incident_type}'"
        ),
    }

def create_false_report_response(incident: Incident) -> dict:
    """
    Low-confidence reports are held for verification instead of being
    dispatched, so a likely-false report never ties up real resources.
    """

    message = (
        f"Reporter confidence too low ({incident.confidence:.2f} < "
        f"{FALSE_REPORT_THRESHOLD}); no resources dispatched. "
        f"Held for verification."
    )
    return {
        "incident_id": incident.incident_id,
        "status": "false_report",
        "severity": {"score": 0, "level": "unverified", "reasoning": message},
        "actions_taken": [
            {"action": "Flagged as possible false report", "timestamp": None},
            {"action": "Dispatch withheld pending verification", "timestamp": None},
        ],
        "resources_allocated": [],
        "escalation_level": "verification_required",
        "reporter_updates": [
            "Thanks for your report. We could not verify it yet - please "
            "send an update with more detail so we can dispatch help."
        ],
        "message": message,
        "log": {
            "incident_id": incident.incident_id,
            "response_time_sec": 0.0,
            "final_severity": 0,
            "escalation_level": "verification_required",
            "resources_used": 0,
            "bottlenecks": ["None"],
        },
    }


FALSE_REPORT_THRESHOLD = 0.3


def is_false_report(incident: Incident) -> bool:
    """
    Detect incidents with very low reporter confidence.
    """

    return incident.confidence < FALSE_REPORT_THRESHOLD


def priority_score(incident: Incident) -> int:
    """
    Assign a priority score based on incident type and severity inputs.
    Higher score means higher priority.
    """

    emergency_access_blocked = incident.severity_inputs.get(
        "emergency_access_blocked", False
    )
    traffic_volume = incident.severity_inputs.get(
        "traffic_volume", "medium"
    )

    score = 0

    if emergency_access_blocked:
        score += 5

    if traffic_volume == "high":
        score += 3
    elif traffic_volume == "medium":
        score += 2
    else:
        score += 1

    if incident.incident_type == "medical":
        score += 4
    elif incident.incident_type in {"fire", "industrial_fire"}:
        score += 4

    return score


def prioritize_incidents(incidents: list[Incident]) -> list[Incident]:
    """
    Return incidents ordered from highest to lowest priority.
    """

    return sorted(
        incidents,
        key=priority_score,
        reverse=True,
    )


def process_incidents(incidents: list[Incident], world: dict):
    """
    Process incidents in priority order and automatically process
    secondary incidents generated by the cascade engine.
    """

    queue = list(prioritize_incidents(incidents))
    results = []
    processed_ids = set()

    while queue:
        incident = queue.pop(0)

        # Prevent duplicate/infinite processing.
        if incident.incident_id in processed_ids:
            continue

        processed_ids.add(incident.incident_id)

        result = dispatch_to_module(incident, world)

        if result is None:
            result = create_unhandled_response(incident)

        results.append(result)

        # Generate downstream incidents only from successful
        # primary module responses.
        cascades = generate_cascades(
            incident,
            result,
            world,
        )

        # Add new incidents to the queue according to priority.
        if cascades:
            queue.extend(cascades)
            queue = prioritize_incidents(queue)

    return results


def run_response(incidents: list[Incident], world: dict) -> dict:
    """
    Run the incident response pipeline and summarize the results.
    Supports both single-module and multi-module responses.
    """

    results = process_incidents(incidents, world)

    # Statistics describe the incidents submitted by the caller.
    # Cascade incidents are downstream actions and must not inflate the
    # top-level handled/unhandled counts.
    original_ids = {incident.incident_id for incident in incidents}
    handled = 0
    unhandled = 0

    for result in results:
        if isinstance(result, list):
            original_results = [
                item for item in result
                if isinstance(item, ModuleOutput)
                and item.incident_id in original_ids
            ]

            if original_results:
                handled += 1
            continue

        if isinstance(result, dict) and result.get("incident_id") in original_ids:
            if result.get("status") in {"unhandled", "false_report"}:
                unhandled += 1
            else:
                handled += 1
        elif isinstance(result, ModuleOutput) and result.incident_id in original_ids:
            handled += 1

    return {
        "total_incidents": len(incidents),
        "handled": handled,
        "unhandled": unhandled,
        "results": results,
    }

def serialize_result(result):
    """
    Convert a ModuleOutput or unhandled response into JSON-friendly data.
    """

    if isinstance(result, ModuleOutput):
        return {
            "incident_id": result.incident_id,
            "severity": result.severity,
            "actions_taken": result.actions_taken,
            "resources_allocated": result.resources_allocated,
            "escalation_level": result.escalation_level,
            "status": result.status,
            "reporter_updates": result.reporter_updates,
            "log": result.log,
        }

    return result
