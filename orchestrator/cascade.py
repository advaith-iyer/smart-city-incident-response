from shared.contracts import Incident


def generate_cascades(incident: Incident, result, world: dict) -> list[Incident]:
    """
    Generate deterministic secondary incidents caused by a primary incident.

    The LLM is NOT responsible for deciding these consequences.
    The cascade engine uses the primary incident + its assessed severity
    to create predictable downstream events.
    """

    cascades: list[Incident] = []

    severity = _get_severity(result)
    incident_type = incident.incident_type.lower()
    description = incident.description.lower()

    # A module the orchestrator already ran for this incident (via a
    # requires_* flag) must not be dispatched a second time as a cascade.
    medical_already_dispatched = bool(
        incident.severity_inputs.get("requires_medical")
    )
    traffic_already_dispatched = bool(
        incident.severity_inputs.get("requires_traffic")
    )

    # ---------------------------------------------------------
    # CASCADE 1:
    # Severe factory incident -> traffic disruption
    # ---------------------------------------------------------
    if (
        incident_type == "factory"
        and severity >= 4
        and not traffic_already_dispatched
    ):
        road = _find_nearest_open_road(incident.location, world)

        if road:
            cascades.append(
                Incident(
                    incident_id=f"{incident.incident_id}-TRAFFIC",
                    incident_type="road_blockage",
                    description=(
                        f"Automatic consequence of {incident.incident_id}: "
                        f"hazard near the industrial incident has blocked "
                        f"road {road['id']}."
                    ),
                    location=incident.location,
                    severity_inputs={
                        "road_id": road["id"],
                        "start_junction": road["from"],
                        "destination_junction": road["to"],
                        "traffic_volume": "high",
                        "emergency_access_blocked": True,
                        "cascade_source": incident.incident_id,
                    },
                    reporter="system_cascade",
                    confidence=1.0,
                )
            )

    # ---------------------------------------------------------
    # CASCADE 2:
    # Factory incident mentioning injuries -> medical response
    # ---------------------------------------------------------
    injury_keywords = (
        "injured",
        "injury",
        "unconscious",
        "collapsed",
        "bleeding",
        "not breathing",
        "worker down",
        "coughing",
    )

    if (
        incident_type == "factory"
        and severity >= 4
        and not medical_already_dispatched
        and any(keyword in description for keyword in injury_keywords)
    ):
        cascades.append(
            Incident(
                incident_id=f"{incident.incident_id}-MEDICAL",
                incident_type="medical",
                description=(
                    f"Medical emergency caused by {incident.incident_id}. "
                    f"Original report: {incident.description}. "
                    f"Workers may have been exposed and require medical assessment."
                ),
                location=incident.location,
                severity_inputs={
                    "cascade_source": incident.incident_id,
                    "emergency_access_blocked": True,
                    "exposure_related": True,
                },
                reporter="system_cascade",
                confidence=1.0,
            )
        )

    # ---------------------------------------------------------
    # CASCADE 3:
    # Severe fire -> medical response
    # ---------------------------------------------------------
    if (
        incident_type in {"fire", "industrial_fire"}
        and severity >= 4
        and not medical_already_dispatched
    ):
        people_at_risk = any(
            keyword in description
            for keyword in ("trapped", "injured", "people", "unconscious", "casualt")
        )
        # Give the Medical module concrete clinical signals to assess.
        condition = (
            "Occupants may have smoke inhalation with difficulty breathing "
            "and burn injuries."
            if people_at_risk
            else "Bystanders may have minor burn injuries."
        )
        cascades.append(
            Incident(
                incident_id=f"{incident.incident_id}-MEDICAL",
                incident_type="medical",
                description=(
                    f"Automatic consequence of {incident.incident_id}: "
                    f"people near the fire may require emergency medical care. "
                    f"{condition}"
                ),
                location=incident.location,
                severity_inputs={
                    "cascade_source": incident.incident_id,
                    "emergency_access_blocked": True,
                },
                reporter="system_cascade",
                confidence=1.0,
            )
        )

    return cascades


def _get_severity(result) -> int:
    """
    Safely extract the severity score from a ModuleOutput,
    serialized dictionary, or multi-module result.
    """

    if isinstance(result, list):
        scores = [_get_severity(item) for item in result]
        return max(scores, default=0)

    if hasattr(result, "severity"):
        severity = result.severity
        if isinstance(severity, dict):
            return int(severity.get("score", 0))

    if isinstance(result, dict):
        severity = result.get("severity", {})
        if isinstance(severity, dict):
            return int(severity.get("score", 0))

    return 0


def _find_nearest_open_road(location: dict, world: dict):
    """
    Find the road edge whose midpoint is closest to the incident.
    Only open roads are considered.
    """

    roads = world.get("roads", {})
    nodes = {
        node["id"]: node
        for node in roads.get("nodes", [])
    }

    lat = location.get("lat")
    lng = location.get("lng")

    if lat is None or lng is None:
        return None

    candidates = []

    for edge in roads.get("edges", []):
        if edge.get("blocked"):
            continue

        start = nodes.get(edge.get("from"))
        end = nodes.get(edge.get("to"))

        if not start or not end:
            continue

        midpoint_lat = (start["lat"] + end["lat"]) / 2
        midpoint_lng = (start["lng"] + end["lng"]) / 2

        distance = (
            (midpoint_lat - lat) ** 2
            + (midpoint_lng - lng) ** 2
        )

        candidates.append((distance, edge))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]