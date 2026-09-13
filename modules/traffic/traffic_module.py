from shared.contracts import Incident, ModuleOutput


def calculate_severity(incident: Incident) -> dict:
    """
    Calculate traffic incident severity from the incident inputs.
    """

    inputs = incident.severity_inputs

    traffic_volume = inputs.get("traffic_volume", "medium")
    alternate_route = inputs.get("alternate_route_available", True)
    emergency_access_blocked = inputs.get("emergency_access_blocked", False)

    # Recovery updates should reduce the incident instead of re-applying
    # the original blockage severity.
    recovery_keywords = (
        "normalcy", "normal", "restored", "cleared", "clear",
        "resolved", "reopened", "traffic flowing",
        "road is open", "road reopened"
    )
    if any(keyword in incident.description.lower() for keyword in recovery_keywords):
        return {
            "score": 1,
            "level": "low",
            "reasoning": (
                "Follow-up update indicates that normal traffic conditions "
                "have been restored."
            ),
        }

    score = 0

    if traffic_volume == "high":
        score += 3
    elif traffic_volume == "medium":
        score += 2
    else:
        score += 1

    if not alternate_route:
        score += 2

    if emergency_access_blocked:
        score += 3

    # Shared contract requires severity to stay within 1–5.
    score = max(1, min(5, score))

    if score >= 5:
        level = "critical"
    elif score >= 4:
        level = "high"
    elif score >= 3:
        level = "medium"
    else:
        level = "low"

    return {
        "score": score,
        "level": level,
        "reasoning": (
            f"Traffic volume={traffic_volume}, "
            f"alternate_route_available={alternate_route}, "
            f"emergency_access_blocked={emergency_access_blocked}"
        ),
    }


def block_road(roads: dict, road_id: str) -> bool:
    """
    Mark a road as blocked.

    Returns True if the road was found and blocked,
    otherwise False.
    """

    for edge in roads["edges"]:
        if edge["id"] == road_id:
            edge["blocked"] = True
            return True

    return False


def get_affected_road(incident: Incident) -> str | None:
    """
    Get the road ID affected by a traffic incident.
    """

    road_id = incident.severity_inputs.get("road_id")

    if not road_id:
        return None

    return road_id


def find_alternate_route(roads: dict, incident: Incident):
    """
    Block the affected road and find the best alternate route.

    The incident must provide:
        start_junction
        destination_junction
        road_id
    """

    road_id = get_affected_road(incident)
    start = incident.severity_inputs.get("start_junction")
    destination = incident.severity_inputs.get("destination_junction")

    if not road_id or not start or not destination:
        return None

    block_road(roads, road_id)

    from shared.road_graph import shortest_path

    return shortest_path(roads, start, destination)


def build_module_output(
    incident: Incident,
    severity: dict,
    affected_road: str | None,
    alternate_route: dict | None,
) -> ModuleOutput:
    """
    Build the standard response returned by the Traffic module.
    """

    actions_taken = []
    resources_allocated = []
    reporter_updates = []

    recovery_keywords = (
        "normalcy", "normal", "restored", "cleared", "clear",
        "resolved", "reopened", "traffic flowing",
        "road is open", "road reopened"
    )
    is_recovery = any(
        keyword in incident.description.lower()
        for keyword in recovery_keywords
    )

    if is_recovery:
        actions_taken.append({
            "action": "Verified that normal traffic conditions have been restored",
            "timestamp": None,
        })
        reporter_updates.append(
            "Normal traffic conditions have been restored. Incident resolved."
        )
        return ModuleOutput(
            incident_id=incident.incident_id,
            severity=severity,
            actions_taken=actions_taken,
            resources_allocated=[],
            escalation_level="local",
            status="resolved",
            reporter_updates=reporter_updates,
            log={
                "incident_id": incident.incident_id,
                "response_time_sec": 0.0,
                "final_severity": severity.get("score"),
                "escalation_level": "local",
                "resources_used": 0,
                "bottlenecks": ["None"],
            },
        )

    if affected_road:
        actions_taken.append({"action": f"Marked road {affected_road} as blocked", "timestamp": None})

    if alternate_route:
        path = " -> ".join(alternate_route["path"])
        distance = alternate_route["distance_km"]

        actions_taken.append({
            "action": f"Calculated alternate route: {path}",
            "timestamp": None,
        })

        reporter_updates.append(
            f"Alternate route available: {path} ({distance:.1f} km)"
        )
    else:
        actions_taken.append({"action": "No alternate route available", "timestamp": None})
        reporter_updates.append(
            "No alternate route is currently available"
        )

    if severity["level"] == "critical":
        escalation_level = "district_traffic_authority"
    elif severity["level"] == "high":
        escalation_level = "road_clearing_crew"
    else:
        escalation_level = "local_traffic_point"

    resources_allocated.append({
        "resource_type": "traffic_control_point",
        "resource_id": "traffic_control_point",
        "eta": "n/a",
    })

    status = "rerouted" if alternate_route else "escalated"

    # Standard ModuleOutput log object, matching the shared contract used
    # by Medical/Fire/Factory. (Previously a list of plain strings, which
    # was a real drift from the other 3 modules -- fixed so all 4 modules'
    # logs are one shape.)
    bottlenecks = []
    if not alternate_route and affected_road:
        bottlenecks.append("No alternate route available around the blocked road.")
    log = {
        "incident_id": incident.incident_id,
        "response_time_sec": 0.0,
        "final_severity": severity.get("score"),
        "escalation_level": escalation_level,
        "resources_used": len(resources_allocated),
        "bottlenecks": bottlenecks if bottlenecks else ["None"],
    }

    return ModuleOutput(
        incident_id=incident.incident_id,
        severity=severity,
        actions_taken=actions_taken,
        resources_allocated=resources_allocated,
        escalation_level=escalation_level,
        status=status,
        reporter_updates=reporter_updates,
        log=log,
    )


def handle_incident(incident: Incident, world: dict) -> ModuleOutput:
    """
    Main entry point for the Traffic module.

    The Orchestrator calls this function with an Incident
    and the shared world state.
    """

    severity = calculate_severity(incident)

    # Recovery updates resolve the incident; do not block the road again.
    recovery_keywords = (
        "normalcy", "normal", "restored", "cleared", "clear",
        "resolved", "reopened", "traffic flowing",
        "road is open", "road reopened"
    )
    if any(keyword in incident.description.lower() for keyword in recovery_keywords):
        return build_module_output(
            incident=incident,
            severity=severity,
            affected_road=None,
            alternate_route=None,
        )

    affected_road = get_affected_road(incident)

    roads = world["roads"]

    alternate_route = None

    if affected_road:
        alternate_route = find_alternate_route(roads, incident)

    return build_module_output(
        incident=incident,
        severity=severity,
        affected_road=affected_road,
        alternate_route=alternate_route,
    )


def process_road_blockage(incident: Incident, world: dict) -> ModuleOutput:
    """
    Process a road blockage and update the provided world state.
    """

    severity = calculate_severity(incident)

    # Recovery updates resolve the incident; do not block the road again.
    recovery_keywords = (
        "normalcy", "normal", "restored", "cleared", "clear",
        "resolved", "reopened", "traffic flowing",
        "road is open", "road reopened"
    )
    if any(keyword in incident.description.lower() for keyword in recovery_keywords):
        return build_module_output(
            incident=incident,
            severity=severity,
            affected_road=None,
            alternate_route=None,
        )

    affected_road = get_affected_road(incident)

    if not affected_road:
        return build_module_output(
            incident=incident,
            severity=severity,
            affected_road=None,
            alternate_route=None,
        )

    alternate_route = find_alternate_route(world["roads"], incident)

    return build_module_output(
        incident=incident,
        severity=severity,
        affected_road=affected_road,
        alternate_route=alternate_route,
    )


def handle_and_save_incident(incident: Incident) -> ModuleOutput:
    """
    Load the shared world, process the traffic incident,
    and persist the updated world state.
    """

    from shared.world_state import load_world, save_world

    world = load_world()

    output = process_road_blockage(
        incident=incident,
        world=world,
    )

    save_world(world)

    return output
