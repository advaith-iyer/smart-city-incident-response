"""
Medical / Ambulance Module
Smart City Incident Response Agent — Tech Zephyr 4.0

Handles:
    - medical
    - injury
    - medical emergencies

Implements:
    Observe -> Assess -> Decide -> Act -> Monitor & Adapt -> Log

Public entry point:
    handle_incident(incident, world=None)
"""

import json
import math
import os
import time
from datetime import datetime

from shared.contracts import Incident, ModuleOutput


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Use the single shared city state.
WORLD_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../shared/city_state.json")
)

AVERAGE_SPEED_KMPH = 30


# ---------------------------------------------------------------------------
# Severity keyword banks
# ---------------------------------------------------------------------------

CRITICAL_KEYWORDS = [
    "unconscious",
    "not responding",
    "not breathing",
    "severe chest pain",
    "difficulty breathing",
    "heavy bleeding",
    "seizure",
    "unresponsive",
    "cardiac",
    "stroke",
    "collapsed",
]

MODERATE_KEYWORDS = [
    "chest pain",
    "bleeding",
    "broken",
    "fracture",
    "dizzy",
    "nauseous",
    "high fever",
    "burn",
    "allergic reaction",
]

MINOR_KEYWORDS = [
    "sprain",
    "twisted",
    "minor",
    "small cut",
    "bruise",
    "scrape",
    "crying",
]


# ---------------------------------------------------------------------------
# Incident adapter
# ---------------------------------------------------------------------------

def incident_to_dict(incident: Incident) -> dict:
    """
    Convert the shared Incident dataclass into the dictionary format
    used internally by this module.
    """
    data = {
        "incident_id": incident.incident_id,
        "type": incident.incident_type,
        "incident_type": incident.incident_type,
        "description": incident.description,
        "raw_description": incident.description,
        "location": incident.location,
        "severity_inputs": incident.severity_inputs,
        "reporter": incident.reporter,
        "reported_by": incident.reporter,
        "timestamp": incident.timestamp,
        "confidence": incident.confidence,
        "status": incident.status,
    }

    # Preserve any optional simulation flags supplied through severity_inputs.
    if incident.severity_inputs:
        for key, value in incident.severity_inputs.items():
            data.setdefault(key, value)

    return data


def normalize_incident(incident) -> dict:
    """
    Accept either:
        - shared Incident object
        - legacy dictionary

    and convert it to one consistent internal dictionary.
    """
    if isinstance(incident, Incident):
        return incident_to_dict(incident)

    if isinstance(incident, dict):
        normalized = dict(incident)

        # Support both contract naming styles.
        if "raw_description" not in normalized:
            normalized["raw_description"] = normalized.get("description", "")

        if "description" not in normalized:
            normalized["description"] = normalized.get("raw_description", "")

        if "incident_type" not in normalized:
            normalized["incident_type"] = normalized.get("type")

        if "type" not in normalized:
            normalized["type"] = normalized.get("incident_type")

        if "reported_by" not in normalized:
            normalized["reported_by"] = normalized.get("reporter")

        if "reporter" not in normalized:
            normalized["reporter"] = normalized.get("reported_by")

        normalized.setdefault("confidence", 1.0)
        normalized.setdefault("status", "reported")

        return normalized

    raise TypeError(
        f"Medical module expected Incident or dict, got {type(incident).__name__}"
    )


# ---------------------------------------------------------------------------
# World / resource directory
# ---------------------------------------------------------------------------

def load_world(path=WORLD_PATH):
    """Load the shared city state."""
    with open(path, "r") as f:
        return json.load(f)


def get_coordinates(item):
    """
    Read coordinates from the shared world format.

    Supports:
        {"location": {"lat": ..., "lng": ...}}

    and also the older flat format:
        {"lat": ..., "lng": ...}
    """
    location = item.get("location", {})

    if isinstance(location, dict):
        lat = location.get("lat")
        lng = location.get("lng")

        if lat is not None and lng is not None:
            return float(lat), float(lng)

    if item.get("lat") is not None and item.get("lng") is not None:
        return float(item["lat"]), float(item["lng"])

    return None, None


def get_incident_coordinates(incident):
    """Get incident latitude/longitude safely."""
    location = incident.get("location", {})

    if not isinstance(location, dict):
        raise ValueError("Incident location must be a dictionary.")

    lat = location.get("lat")
    lng = location.get("lng")

    if lat is None or lng is None:
        raise ValueError(
            "Incident location must contain 'lat' and 'lng'."
        )

    return float(lat), float(lng)


def haversine_km(lat1, lng1, lat2, lng2):
    """Straight-line distance in km between two lat/lng points."""
    R = 6371.0

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dlambda / 2) ** 2
    )

    return 2 * R * math.asin(math.sqrt(a))


def eta_minutes(distance_km, speed_kmph=AVERAGE_SPEED_KMPH):
    """Convert distance to approximate travel time."""
    return round((distance_km / speed_kmph) * 60, 1)


def road_aware_distance_km(world, lat1, lng1, lat2, lng2):
    """
    Distance used for ETA calculations.

    Uses the shared road graph (shared/road_graph.py), which respects
    blocked roads via Dijkstra shortest-path, when the world provides
    one. Falls back to straight-line distance if the world has no road
    graph or the graph lookup fails for any reason, so this never
    crashes because of a missing/malformed roads section.
    """
    roads = world.get("roads") if world else None
    if roads:
        try:
            from shared.road_graph import route_distance_km
            return route_distance_km(roads, lat1, lng1, lat2, lng2)["distance_km"]
        except Exception:
            pass
    return haversine_km(lat1, lng1, lat2, lng2)


def find_best_facility(
    world,
    facility_type,
    incident_lat,
    incident_lng,
    require_capacity=True,
):
    """
    Find the nearest facility with available capacity.

    Returns:
        (facility, distance_km)

    or:
        (None, None)
    """

    candidates = world.get(facility_type, [])

    scored = []

    for facility in candidates:

        if require_capacity and "capacity" in facility:
            current_load = facility.get(
                "current_load",
                facility.get("available_capacity", 0),
            )

            # If available_capacity exists, use it directly.
            if "available_capacity" in facility:
                if facility["available_capacity"] <= 0:
                    continue
            elif current_load >= facility["capacity"]:
                continue

        lat, lng = get_coordinates(facility)

        if lat is None or lng is None:
            continue

        distance = road_aware_distance_km(
            world,
            incident_lat,
            incident_lng,
            lat,
            lng,
        )

        scored.append((distance, facility))

    if not scored:
        return None, None

    scored.sort(key=lambda x: x[0])

    best_distance, best_facility = scored[0]

    return best_facility, best_distance


def find_available_ambulance(
    world,
    incident_lat,
    incident_lng,
):
    """Find the nearest available ambulance."""

    candidates = [
        ambulance
        for ambulance in world.get("ambulances", [])
        if ambulance.get("status") == "available"
    ]

    scored = []

    for ambulance in candidates:

        lat, lng = get_coordinates(ambulance)

        if lat is None or lng is None:
            continue

        distance = road_aware_distance_km(
            world,
            incident_lat,
            incident_lng,
            lat,
            lng,
        )

        scored.append((distance, ambulance))

    if not scored:
        return None, None

    scored.sort(key=lambda x: x[0])

    distance, ambulance = scored[0]

    return ambulance, distance


def increment_load(facility):
    """Safely increment facility load/capacity state."""

    if "current_load" in facility:
        facility["current_load"] += 1

    if "available_capacity" in facility:
        facility["available_capacity"] = max(
            0,
            facility["available_capacity"] - 1,
        )


# ---------------------------------------------------------------------------
# Step 2 — Assess
# ---------------------------------------------------------------------------

def assess_severity_rule_based(incident):
    """
    Rule-based medical severity assessment.

    Returns:
        (score, reasoning)
    """

    text = incident.get("raw_description", "").lower()
    confidence = float(incident.get("confidence", 0.5))

    if any(keyword in text for keyword in CRITICAL_KEYWORDS):

        score = 5

        reason = (
            "Description matches critical medical symptoms "
            "(e.g. unconsciousness or breathing difficulty)."
        )

    elif any(keyword in text for keyword in MODERATE_KEYWORDS):

        score = 3

        reason = (
            "Description matches moderate medical symptoms "
            "(e.g. bleeding, fracture or dizziness)."
        )

    elif any(keyword in text for keyword in MINOR_KEYWORDS):

        score = 1

        reason = (
            "Description matches minor injury keywords "
            "(e.g. sprain or small cut)."
        )

    else:

        score = 2

        reason = (
            "No strong medical keyword match; "
            "defaulting to low-moderate severity pending more information."
        )

    if confidence < 0.5:
        reason += (
            " Confidence is low; verification is recommended "
            "alongside the response."
        )

    return score, reason


def assess_severity_llm(incident):
    """LLM-based severity assessment via shared.llm.llm_assess."""
    from shared.llm import llm_assess
    return llm_assess(incident.get("raw_description", ""), domain_hint="medical")


def assess_severity(incident):
    """
    Main severity assessment entry point.

    Tries the LLM path first when USE_LLM_SEVERITY=1; falls back to
    rule-based scoring on ANY failure (disabled, missing key, network
    error, bad response, etc.) so the module never crashes because of
    the LLM being unavailable.
    """

    if os.environ.get("USE_LLM_SEVERITY") == "1":
        try:
            return assess_severity_llm(incident)
        except Exception:
            pass

    return assess_severity_rule_based(incident)


# ---------------------------------------------------------------------------
# Step 3 — Decide
# ---------------------------------------------------------------------------

def decide_action(incident, severity_score):
    """
    Medical escalation ladder:

        local first aid
            ↓
        dispensary
            ↓
        hospital + ambulance
    """

    if severity_score >= 4:

        return {
            "escalation_level": "professional",
            "plan": "hospital_via_ambulance",
            "reasoning": (
                "High severity: route directly to hospital "
                "with ambulance dispatch."
            ),
        }

    elif severity_score == 3:

        return {
            "escalation_level": "professional",
            "plan": "dispensary_then_monitor",
            "reasoning": (
                "Moderate severity: route to nearest dispensary "
                "and escalate if condition worsens."
            ),
        }

    else:

        return {
            "escalation_level": "local",
            "plan": "local_first_aid",
            "reasoning": (
                "Low severity: local first-aid response "
                "is sufficient."
            ),
        }


# ---------------------------------------------------------------------------
# Step 4 — Act
# ---------------------------------------------------------------------------

def act(world, incident, plan):
    """
    Execute the selected medical response plan.
    """

    actions_taken = []
    resources_allocated = []
    reporter_updates = []

    lat, lng = get_incident_coordinates(incident)

    def now():
        return datetime.utcnow().isoformat()

    # ---------------------------------------------------------------
    # High severity
    # ---------------------------------------------------------------

    if plan["plan"] == "hospital_via_ambulance":

        hospital, hospital_distance = find_best_facility(
            world,
            "hospitals",
            lat,
            lng,
        )

        ambulance, ambulance_distance = find_available_ambulance(
            world,
            lat,
            lng,
        )

        if ambulance:

            ambulance["status"] = "dispatched"

            actions_taken.append(
                {
                    "action": f"Dispatched {ambulance['name']}",
                    "timestamp": now(),
                }
            )

            resources_allocated.append(
                {
                    "resource_type": "ambulance",
                    "resource_id": ambulance["id"],
                    "eta": f"{eta_minutes(ambulance_distance)} min",
                }
            )

            reporter_updates.append(
                f"An ambulance ({ambulance['name']}) "
                "has been dispatched to you."
            )

        else:

            actions_taken.append(
                {
                    "action": (
                        "No ambulance available - "
                        "alerting nearest dispensary for interim first aid"
                    ),
                    "timestamp": now(),
                }
            )

            reporter_updates.append(
                "No ambulance is currently free. "
                "A dispensary team has been alerted "
                "to assist in the meantime."
            )

        if hospital:

            increment_load(hospital)

            actions_taken.append(
                {
                    "action": f"Reserved bed at {hospital['name']}",
                    "timestamp": now(),
                }
            )

            resources_allocated.append(
                {
                    "resource_type": "hospital",
                    "resource_id": hospital["id"],
                    "eta": f"{eta_minutes(hospital_distance)} min",
                }
            )

            reporter_updates.append(
                f"{hospital['name']} has been notified "
                "and is expecting the patient."
            )

        else:

            actions_taken.append(
                {
                    "action": (
                        "No hospital with free capacity found nearby - "
                        "escalating to district authority"
                    ),
                    "timestamp": now(),
                }
            )

            reporter_updates.append(
                "All nearby hospitals are at capacity; "
                "escalating to district emergency services "
                "for placement."
            )

    # ---------------------------------------------------------------
    # Moderate severity
    # ---------------------------------------------------------------

    elif plan["plan"] == "dispensary_then_monitor":

        dispensary, dispensary_distance = find_best_facility(
            world,
            "dispensaries",
            lat,
            lng,
        )

        if dispensary:

            increment_load(dispensary)

            actions_taken.append(
                {
                    "action": f"Alerted {dispensary['name']}",
                    "timestamp": now(),
                }
            )

            resources_allocated.append(
                {
                    "resource_type": "dispensary",
                    "resource_id": dispensary["id"],
                    "eta": f"{eta_minutes(dispensary_distance)} min",
                }
            )

            reporter_updates.append(
                f"{dispensary['name']} has been alerted "
                "and will assist shortly."
            )

        else:

            actions_taken.append(
                {
                    "action": (
                        "No dispensary with available capacity - "
                        "escalating to hospital"
                    ),
                    "timestamp": now(),
                }
            )

            hospital, hospital_distance = find_best_facility(
                world,
                "hospitals",
                lat,
                lng,
            )

            if hospital:

                increment_load(hospital)

                resources_allocated.append(
                    {
                        "resource_type": "hospital",
                        "resource_id": hospital["id"],
                        "eta": f"{eta_minutes(hospital_distance)} min",
                    }
                )

                reporter_updates.append(
                    f"Nearest dispensary was unavailable, "
                    f"so {hospital['name']} has been notified instead."
                )

            else:

                reporter_updates.append(
                    "No nearby medical facility has available capacity. "
                    "Escalating to district emergency services."
                )

    # ---------------------------------------------------------------
    # Low severity
    # ---------------------------------------------------------------

    else:

        actions_taken.append(
            {
                "action": (
                    "Notified nearby registered "
                    "first-aid volunteers"
                ),
                "timestamp": now(),
            }
        )

        reporter_updates.append(
            "A nearby volunteer has been notified "
            "and is on the way to assist."
        )

    return (
        actions_taken,
        resources_allocated,
        reporter_updates,
    )


# ---------------------------------------------------------------------------
# Step 5 — Monitor & Adapt
# ---------------------------------------------------------------------------

def monitor_and_adapt(
    world,
    incident,
    severity_score,
    plan,
    actions_taken,
    resources_allocated,
    reporter_updates,
):
    """
    Simulate monitoring.

    If _simulate_escalation is present, severity increases
    and the response plan is recalculated.
    """

    if not incident.get("_simulate_escalation"):
        return severity_score, plan, "no_change"

    old_score = severity_score

    new_score = min(
        5,
        severity_score + 3,
    )

    actions_taken.append(
        {
            "action": (
                "Monitoring update: condition worsened, "
                f"severity re-assessed from {old_score} "
                f"to {new_score}"
            ),
            "timestamp": datetime.utcnow().isoformat(),
        }
    )

    reporter_updates.append(
        "Update: the situation has escalated. "
        "A higher-priority response is now being dispatched."
    )

    new_plan = decide_action(
        incident,
        new_score,
    )

    (
        extra_actions,
        extra_resources,
        extra_updates,
    ) = act(
        world,
        incident,
        new_plan,
    )

    actions_taken.extend(extra_actions)
    resources_allocated.extend(extra_resources)
    reporter_updates.extend(extra_updates)

    return (
        new_score,
        new_plan,
        "escalated",
    )


# ---------------------------------------------------------------------------
# Step 6 — Log
# ---------------------------------------------------------------------------

def build_log(
    incident,
    severity_score,
    plan,
    resources_allocated,
    adapt_status,
    start_time,
):
    response_time_sec = round(
        time.time() - start_time,
        3,
    )

    bottlenecks = []

    if not resources_allocated:

        bottlenecks.append(
            "No facility/resource could be allocated."
        )

    if adapt_status == "escalated":

        bottlenecks.append(
            "Initial severity assessment underestimated "
            "the incident; required re-escalation."
        )

    return {
        "incident_id": incident["incident_id"],
        "response_time_sec": response_time_sec,
        "final_severity": severity_score,
        "escalation_level": plan["escalation_level"],
        "resources_used": len(resources_allocated),
        "bottlenecks": (
            bottlenecks
            if bottlenecks
            else ["None"]
        ),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def handle_incident(incident, world=None):
    """
    Public interface used by the Orchestrator.

    Accepts:
        Incident
        OR legacy dict

    Returns:
        ModuleOutput
    """

    start_time = time.time()

    incident = normalize_incident(incident)

    if world is None:
        world = load_world()

    # ---------------------------------------------------------------
    # 1. Observe
    # ---------------------------------------------------------------

    # Incident has already been received.

    # ---------------------------------------------------------------
    # 2. Assess
    # ---------------------------------------------------------------

    severity_score, severity_reasoning = assess_severity(
        incident
    )

    # ---------------------------------------------------------------
    # 3. Decide
    # ---------------------------------------------------------------

    plan = decide_action(
        incident,
        severity_score,
    )

    # ---------------------------------------------------------------
    # 4. Act
    # ---------------------------------------------------------------

    (
        actions_taken,
        resources_allocated,
        reporter_updates,
    ) = act(
        world,
        incident,
        plan,
    )

    # ---------------------------------------------------------------
    # 5. Monitor & Adapt
    # ---------------------------------------------------------------

    (
        severity_score,
        plan,
        adapt_status,
    ) = monitor_and_adapt(
        world,
        incident,
        severity_score,
        plan,
        actions_taken,
        resources_allocated,
        reporter_updates,
    )

    # ---------------------------------------------------------------
    # 6. Log
    # ---------------------------------------------------------------

    log = build_log(
        incident,
        severity_score,
        plan,
        resources_allocated,
        adapt_status,
        start_time,
    )

    status = (
        "resolved"
        if resources_allocated
        else "escalated"
    )

    return ModuleOutput(
        incident_id=incident["incident_id"],
        severity={
            "score": severity_score,
            "reasoning": severity_reasoning,
        },
        actions_taken=actions_taken,
        resources_allocated=resources_allocated,
        escalation_level=plan["escalation_level"],
        status=status,
        reporter_updates=reporter_updates,
        log=log,
    )


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    demo_incident = Incident(
        incident_id="INC-TEST",
        incident_type="medical",
        description="Person unconscious and not responding.",
        location={
            "lat": 21.2560,
            "lng": 81.6360,
            "area_name": "Market Square",
        },
        reporter="reporter_demo",
        confidence=0.9,
    )

    result = handle_incident(demo_incident)

    print(
        json.dumps(
            result.__dict__,
            indent=2,
            default=str,
        )
    )