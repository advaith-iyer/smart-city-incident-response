"""
Fire & Hazard Module
Person A — Smart City Incident Response Agent

Handles:
    - fire
    - gas leaks
    - chemical hazards

Pipeline:
    Observe -> Assess -> Decide -> Act -> Monitor & Adapt -> Log

Shared interface:
    handle_incident(incident: Incident, world: dict) -> ModuleOutput
"""

import json
import math
import time
from datetime import datetime

from shared.contracts import Incident, ModuleOutput


AVERAGE_FIRE_SPEED_KMPH = 35


# ============================================================
# SEVERITY CONFIGURATION
# ============================================================

SPREAD_SCORES = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "extreme": 3,
}

CROWD_SCORES = {
    "low": 0,
    "medium": 1,
    "high": 2,
}

STRUCTURE_SCORES = {
    "low": 0,
    "medium": 1,
    "high": 2,
}

WIND_SCORES = {
    "calm": 0,
    "low": 0,
    "moderate": 1,
    "high": 2,
    "strong": 2,
}


FIRE_KEYWORDS = [
    "fire",
    "flames",
    "burning",
    "smoke",
    "blaze",
    "explosion",
]

GAS_KEYWORDS = [
    "gas leak",
    "gas leakage",
    "gas smell",
    "chemical leak",
    "chemical spill",
    "toxic fumes",
    "leaking cylinder",
]


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def now() -> str:
    """Return current UTC timestamp."""
    return datetime.utcnow().isoformat()


def haversine_km(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
) -> float:
    """Calculate straight-line distance between two coordinates."""

    radius = 6371.0

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

    return 2 * radius * math.asin(math.sqrt(a))


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


def eta_minutes(
    distance_km: float,
    speed_kmph: float = AVERAGE_FIRE_SPEED_KMPH,
) -> float:
    """Estimate fire tender ETA."""

    return round((distance_km / speed_kmph) * 60, 1)


def incident_to_dict(incident: Incident) -> dict:
    """
    Convert the team's shared Incident dataclass into the dictionary
    format used internally by the Fire module.
    """

    return {
        "incident_id": incident.incident_id,
        "incident_type": incident.incident_type,
        "description": incident.description,
        "raw_description": incident.description,
        "location": incident.location,
        "severity_inputs": incident.severity_inputs,
        "reporter": incident.reporter,
    }


def location_of(resource: dict) -> dict:
    """
    Support both:
        {"location": {"lat": ..., "lng": ...}}
    and:
        {"lat": ..., "lng": ...}
    """

    if "location" in resource:
        return resource["location"]

    return resource


# ============================================================
# STEP 1 — OBSERVE / CLASSIFY
# ============================================================

def classify_hazard(incident: dict) -> str:
    """Classify the incident as fire or gas/chemical hazard."""

    text = incident.get(
        "description",
        incident.get("raw_description", ""),
    ).lower()

    if any(keyword in text for keyword in GAS_KEYWORDS):
        return "gas_or_chemical"

    if any(keyword in text for keyword in FIRE_KEYWORDS):
        return "fire"

    # If the Orchestrator already routed the incident to Fire,
    # conservatively treat an unclear description as a fire.
    return "fire"


# ============================================================
# STEP 2 — ASSESS
# ============================================================

def assess_severity(incident: dict) -> dict:
    """
    Assess fire/hazard severity on a 1–5 scale.

    Inputs:
        spread_risk
        crowd_proximity
        structure_proximity
        wind
        corroborating_reports
        confidence
    """

    inputs = incident.get("severity_inputs", {})

    hazard_type = classify_hazard(incident)

    spread = str(
        inputs.get("spread_risk", "medium")
    ).lower()

    crowd = str(
        inputs.get("crowd_proximity", "medium")
    ).lower()

    structures = str(
        inputs.get("structure_proximity", "medium")
    ).lower()

    wind = str(
        inputs.get("wind", "moderate")
    ).lower()

    corroborating_reports = int(
        inputs.get("corroborating_reports", 1)
    )

    confidence = float(
        inputs.get("confidence", 1.0)
    )

    score = 1
    reasons = []

    score += SPREAD_SCORES.get(spread, 1)
    reasons.append(f"spread risk={spread}")

    score += CROWD_SCORES.get(crowd, 1)
    reasons.append(f"crowd proximity={crowd}")

    score += STRUCTURE_SCORES.get(structures, 1)
    reasons.append(f"structure proximity={structures}")

    score += WIND_SCORES.get(wind, 1)
    reasons.append(f"wind={wind}")

    if hazard_type == "gas_or_chemical":
        score += 1
        reasons.append(
            "chemical/gas hazard increases risk"
        )

    if corroborating_reports >= 3:
        score += 1
        reasons.append(
            f"{corroborating_reports} corroborating reports"
        )

    elif corroborating_reports == 1:
        reasons.append(
            "single report; verification recommended"
        )

    if confidence < 0.5:
        reasons.append(
            "low confidence; extra verification recommended"
        )

    score = max(1, min(5, score))

    return {
        "score": score,
        "level": (
            "critical" if score == 5
            else "high" if score == 4
            else "medium" if score == 3
            else "low"
        ),
        "reasoning": (
            f"{hazard_type.replace('_', ' ').title()} "
            f"severity assessed at {score}/5: "
            + ", ".join(reasons)
        ),
    }


# ============================================================
# STEP 3 — DECIDE
# ============================================================

def decide_action(
    incident: dict,
    severity_score: int,
) -> dict:
    """
    Fire escalation ladder:

        local volunteers
              ↓
        fire station
              ↓
        district emergency services
    """

    if severity_score >= 5:

        return {
            "escalation_level": "multi-agency",
            "plan": "district_fire_response",
            "reasoning": (
                "Critical fire/hazard: professional fire "
                "response and district emergency services required."
            ),
        }

    if severity_score >= 3:

        return {
            "escalation_level": "professional",
            "plan": "fire_station_response",
            "reasoning": (
                "Moderate/high risk: dispatch a fire unit "
                "and establish a controlled hazard area."
            ),
        }

    return {
        "escalation_level": "local",
        "plan": "local_fire_response",
        "reasoning": (
            "Lower risk: alert nearby trained volunteers "
            "and monitor the incident."
        ),
    }


# ============================================================
# RESOURCE MANAGEMENT
# ============================================================

def find_nearest_fire_unit(
    world: dict,
    incident: dict,
):
    """Find the nearest available fire tender."""

    location = incident.get("location", {})

    lat = location["lat"]
    lng = location["lng"]

    candidates = []

    for unit in world.get("fire_units", []):

        if unit.get("status", "available") != "available":
            continue

        unit_location = location_of(unit)

        if (
            "lat" not in unit_location
            or "lng" not in unit_location
        ):
            continue

        distance = road_aware_distance_km(
            world,
            lat,
            lng,
            unit_location["lat"],
            unit_location["lng"],
        )

        candidates.append(
            (distance, unit)
        )

    if not candidates:
        return None, None

    candidates.sort(
        key=lambda item: item[0]
    )

    return candidates[0][1], candidates[0][0]


def evacuation_radius_km(
    incident: dict,
    severity_score: int,
) -> float:
    """Calculate evacuation/hazard radius."""

    radius = {
        1: 0.05,
        2: 0.10,
        3: 0.20,
        4: 0.40,
        5: 0.75,
    }[severity_score]

    if classify_hazard(incident) == "gas_or_chemical":
        radius *= 1.25

    return round(radius, 2)


# ============================================================
# UNIQUE FEATURE — DANGEROUS AREA FLAGGING
# ============================================================

def flag_dangerous_area(
    world: dict,
    incident: dict,
    severity_score: int,
):
    """
    Mark an area as high-risk after:

        - severity 4/5 incident
        OR
        - repeated fire/hazard incidents
    """

    location = incident.get("location", {})

    area_name = location.get(
        "area_name",
        f"Area near {location.get('lat', 'unknown')}, "
        f"{location.get('lng', 'unknown')}",
    )

    history = world.setdefault(
        "fire_history",
        [],
    )

    previous_count = sum(
        1
        for item in history
        if item.get("area_name") == area_name
    )

    repeated_or_severe = (
        severity_score >= 4
        or previous_count >= 1
    )

    history.append(
        {
            "incident_id": incident.get(
                "incident_id"
            ),
            "area_name": area_name,
            "severity": severity_score,
            "timestamp": now(),
        }
    )

    if not repeated_or_severe:
        return None

    zone = {
        "zone_id": (
            f"DANGER-"
            f"{area_name.replace(' ', '-').upper()}"
        ),
        "area_name": area_name,
        "center": {
            "lat": location["lat"],
            "lng": location["lng"],
        },
        "radius_km": evacuation_radius_km(
            incident,
            severity_score,
        ),
        "reason": (
            "severe incident"
            if severity_score >= 4
            else "repeated fire/hazard incidents"
        ),
        "status": "high_risk",
    }

    zones = world.setdefault(
        "danger_zones",
        [],
    )

    # Avoid duplicate zones for the same area.
    zones[:] = [
        existing
        for existing in zones
        if existing.get("area_name") != area_name
    ]

    zones.append(zone)

    return zone


# ============================================================
# STEP 4 — ACT
# ============================================================

def act(
    world: dict,
    incident: dict,
    plan: dict,
    severity_score: int,
):
    """Execute the selected response plan."""

    actions_taken = []
    resources_allocated = []
    reporter_updates = []

    fire_unit, distance = find_nearest_fire_unit(
        world,
        incident,
    )

    # --------------------------------------------------------
    # Professional fire response
    # --------------------------------------------------------

    if plan["plan"] in {
        "fire_station_response",
        "district_fire_response",
    }:

        if fire_unit:

            fire_unit["status"] = "dispatched"

            actions_taken.append(
                {
                    "action": (
                        f"Dispatched "
                        f"{fire_unit.get('name', fire_unit['id'])}"
                    ),
                    "timestamp": now(),
                }
            )

            resources_allocated.append(
                {
                    "resource_type": "fire_tender",
                    "resource_id": fire_unit["id"],
                    "eta": (
                        f"{eta_minutes(distance)} min"
                    ),
                }
            )

            reporter_updates.append(
                f"Fire response unit "
                f"{fire_unit.get('name', fire_unit['id'])} "
                f"has been dispatched."
            )

        else:

            actions_taken.append(
                {
                    "action": (
                        "No available fire tender found"
                    ),
                    "timestamp": now(),
                }
            )

            reporter_updates.append(
                "No fire tender is currently available; "
                "the incident has been escalated."
            )

    # --------------------------------------------------------
    # Local response
    # --------------------------------------------------------

    if plan["plan"] == "local_fire_response":

        actions_taken.append(
            {
                "action": (
                    "Alerted nearby trained "
                    "fire-response volunteers"
                ),
                "timestamp": now(),
            }
        )

        reporter_updates.append(
            "Nearby trained volunteers have been "
            "alerted while the situation is monitored."
        )

    # --------------------------------------------------------
    # Evacuation / hazard zone
    # --------------------------------------------------------

    if severity_score >= 3:

        radius = evacuation_radius_km(
            incident,
            severity_score,
        )

        actions_taken.append(
            {
                "action": (
                    f"Created evacuation/hazard zone "
                    f"with radius {radius:.2f} km"
                ),
                "timestamp": now(),
            }
        )

        reporter_updates.append(
            f"People within approximately "
            f"{radius:.2f} km should move away "
            f"from the hazard area."
        )

    # --------------------------------------------------------
    # District escalation
    # --------------------------------------------------------

    if plan["plan"] == "district_fire_response":

        actions_taken.append(
            {
                "action": (
                    "Notified district emergency services"
                ),
                "timestamp": now(),
            }
        )

        resources_allocated.append(
            {
                "resource_type": (
                    "district_emergency_services"
                ),
                "resource_id": "DISTRICT-EMERGENCY",
                "eta": "on-call",
            }
        )

        reporter_updates.append(
            "District emergency services have been "
            "notified because the incident is critical."
        )

    return (
        actions_taken,
        resources_allocated,
        reporter_updates,
    )


# ============================================================
# STEP 5 — MONITOR & ADAPT
# ============================================================

def monitor_and_adapt(
    world: dict,
    incident: dict,
    severity_score: int,
    plan: dict,
    actions_taken: list,
    resources_allocated: list,
    reporter_updates: list,
):
    """
    Re-plan when the environment changes.

    Demo simulation flags:

        _simulate_spread
        _simulate_resource_unavailable
        _simulate_severity_increase
    """

    changed = False

    simulation = incident.get(
        "severity_inputs",
        {},
    )

    # --------------------------------------------------------
    # Resource failure
    # --------------------------------------------------------

    if simulation.get(
        "_simulate_resource_unavailable"
    ):

        for unit in world.get(
            "fire_units",
            [],
        ):

            if unit.get("status") == "dispatched":

                unit["status"] = "unavailable"

                changed = True

                actions_taken.append(
                    {
                        "action": (
                            f"Monitoring update: "
                            f"{unit.get('name', unit['id'])} "
                            f"became unavailable"
                        ),
                        "timestamp": now(),
                    }
                )

                reporter_updates.append(
                    "Update: the assigned fire unit "
                    "became unavailable. Searching "
                    "for another response resource."
                )

                break

        replacement, distance = (
            find_nearest_fire_unit(
                world,
                incident,
            )
        )

        if replacement:

            replacement["status"] = "dispatched"

            resources_allocated.append(
                {
                    "resource_type": "fire_tender",
                    "resource_id": replacement["id"],
                    "eta": (
                        f"{eta_minutes(distance)} min"
                    ),
                }
            )

            actions_taken.append(
                {
                    "action": (
                        f"Reassigned response to "
                        f"{replacement.get('name', replacement['id'])}"
                    ),
                    "timestamp": now(),
                }
            )

            reporter_updates.append(
                f"Replacement fire unit "
                f"{replacement.get('name', replacement['id'])} "
                f"has been dispatched."
            )

    # --------------------------------------------------------
    # Fire spreading
    # --------------------------------------------------------

    if simulation.get("_simulate_spread"):

        old_score = severity_score

        severity_score = min(
            5,
            severity_score + 1,
        )

        changed = True

        actions_taken.append(
            {
                "action": (
                    f"Monitoring update: fire spread "
                    f"detected; severity increased "
                    f"from {old_score} to {severity_score}"
                ),
                "timestamp": now(),
            }
        )

        reporter_updates.append(
            "Update: the hazard is spreading. "
            "The response priority and evacuation "
            "zone have been increased."
        )

        new_plan = decide_action(
            incident,
            severity_score,
        )

        if new_plan["plan"] != plan["plan"]:

            plan = new_plan

            extra_actions, extra_resources, extra_updates = act(
                world,
                incident,
                plan,
                severity_score,
            )

            actions_taken.extend(
                extra_actions
            )

            resources_allocated.extend(
                extra_resources
            )

            reporter_updates.extend(
                extra_updates
            )

    # --------------------------------------------------------
    # General severity increase
    # --------------------------------------------------------

    if simulation.get(
        "_simulate_severity_increase"
    ):

        old_score = severity_score

        severity_score = min(
            5,
            severity_score + 2,
        )

        changed = True

        actions_taken.append(
            {
                "action": (
                    f"Monitoring update: severity "
                    f"re-assessed from {old_score} "
                    f"to {severity_score}"
                ),
                "timestamp": now(),
            }
        )

        reporter_updates.append(
            "Update: the incident has worsened "
            "and has been escalated to a higher "
            "response level."
        )

        new_plan = decide_action(
            incident,
            severity_score,
        )

        if new_plan["plan"] != plan["plan"]:

            plan = new_plan

            extra_actions, extra_resources, extra_updates = act(
                world,
                incident,
                plan,
                severity_score,
            )

            actions_taken.extend(
                extra_actions
            )

            resources_allocated.extend(
                extra_resources
            )

            reporter_updates.extend(
                extra_updates
            )

    # Refresh danger-zone status after adaptation.
    if changed:

        flag_dangerous_area(
            world,
            incident,
            severity_score,
        )

    return (
        severity_score,
        plan,
        "adapted" if changed else "no_change",
    )


# ============================================================
# STEP 6 — LOG
# ============================================================

def build_log(
    incident: dict,
    severity_score: int,
    plan: dict,
    resources_allocated: list,
    adapt_status: str,
    start_time: float,
    danger_zone,
) -> dict:
    """
    Create the standard ModuleOutput log object.

    Matches the shared contract used by Medical/Factory:
        {incident_id, response_time_sec, final_severity,
         escalation_level, resources_used, bottlenecks}
    (Previously returned a list of strings, which was a real drift from
    the other 3 modules -- fixed so all 4 modules' logs are one shape.)
    """

    response_time_sec = round(
        time.time() - start_time,
        3,
    )

    bottlenecks = []
    if not resources_allocated:
        bottlenecks.append("No available fire tender.")
    if adapt_status and adapt_status != "no_change":
        bottlenecks.append(f"Adaptation occurred: {adapt_status}.")

    return {
        "incident_id": incident.get("incident_id"),
        "response_time_sec": response_time_sec,
        "final_severity": severity_score,
        "escalation_level": plan["escalation_level"],
        "resources_used": len(resources_allocated),
        "bottlenecks": bottlenecks if bottlenecks else ["None"],
        "danger_zone_flagged": bool(danger_zone),
    }


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def handle_incident(
    incident: Incident,
    world: dict,
) -> ModuleOutput:
    """
    Main entry point used by the Orchestrator.

    The outside interface follows the shared team contract:

        Incident -> ModuleOutput

    Internally, the existing Fire logic continues using dictionaries.
    """

    start_time = time.time()

    # Convert shared Incident → internal dictionary.
    incident_data = incident_to_dict(
        incident
    )

    # --------------------------------------------------------
    # 1. OBSERVE
    # --------------------------------------------------------

    hazard_type = classify_hazard(
        incident_data
    )

    # --------------------------------------------------------
    # 2. ASSESS
    # --------------------------------------------------------

    severity = assess_severity(
        incident_data
    )

    severity_score = severity["score"]

    # --------------------------------------------------------
    # 3. DECIDE
    # --------------------------------------------------------

    plan = decide_action(
        incident_data,
        severity_score,
    )

    # --------------------------------------------------------
    # 4. ACT
    # --------------------------------------------------------

    (
        actions_taken,
        resources_allocated,
        reporter_updates,
    ) = act(
        world,
        incident_data,
        plan,
        severity_score,
    )

    # --------------------------------------------------------
    # UNIQUE FIRE FEATURE
    # --------------------------------------------------------

    danger_zone = flag_dangerous_area(
        world,
        incident_data,
        severity_score,
    )

    if danger_zone:

        actions_taken.append(
            {
                "action": (
                    f"Flagged "
                    f"{danger_zone['area_name']} "
                    f"as a high-risk danger zone "
                    f"({danger_zone['reason']})"
                ),
                "timestamp": now(),
            }
        )

        reporter_updates.append(
            f"{danger_zone['area_name']} is now "
            "marked as a high-risk area for "
            "future reports."
        )

    # --------------------------------------------------------
    # 5. MONITOR & ADAPT
    # --------------------------------------------------------

    (
        severity_score,
        plan,
        adapt_status,
    ) = monitor_and_adapt(
        world,
        incident_data,
        severity_score,
        plan,
        actions_taken,
        resources_allocated,
        reporter_updates,
    )

    severity["score"] = severity_score

    # --------------------------------------------------------
    # 6. LOG
    # --------------------------------------------------------

    log = build_log(
        incident_data,
        severity_score,
        plan,
        resources_allocated,
        adapt_status,
        start_time,
        danger_zone,
    )

    status = (
        "resolved"
        if resources_allocated
        else "escalated"
    )

    # --------------------------------------------------------
    # Convert internal rich structures to the team's
    # canonical ModuleOutput format.
    # --------------------------------------------------------

    clean_actions = [
        action["action"]
        if isinstance(action, dict)
        else action
        for action in actions_taken
    ]

    clean_resources = [
        (
            f"{resource['resource_type']} "
            f"({resource['resource_id']}, "
            f"ETA {resource['eta']})"
        )
        if isinstance(resource, dict)
        else resource
        for resource in resources_allocated
    ]

    return ModuleOutput(
        incident_id=incident.incident_id,
        severity=severity,
        actions_taken=clean_actions,
        resources_allocated=clean_resources,
        escalation_level=plan[
            "escalation_level"
        ],
        status=status,
        reporter_updates=reporter_updates,
        log=log,
    )


# ============================================================
# STANDALONE TESTING
# ============================================================

def handle_and_save_incident(
    incident: Incident,
) -> ModuleOutput:
    """
    Convenience function for standalone Fire testing.

    Uses the shared city state.
    """

    from shared.world_state import (
        load_world,
        save_world,
    )

    world = load_world()

    result = handle_incident(
        incident,
        world,
    )

    save_world(world)

    return result


# ============================================================
# FIRE MODULE DEMO
# ============================================================

if __name__ == "__main__":

    from shared.world_state import load_world

    demo_incident = Incident(
        incident_id="FIRE-TEST-001",
        incident_type="fire",
        description=(
            "Large fire near a crowded market "
            "with smoke spreading."
        ),
        location={
            "lat": 21.255,
            "lng": 81.635,
            "area_name": "Central City",
        },
        severity_inputs={
            "spread_risk": "high",
            "crowd_proximity": "high",
            "structure_proximity": "high",
            "wind": "strong",
            "corroborating_reports": 2,
            "confidence": 0.9,
        },
        reporter="demo_user",
    )

    world = load_world()

    result = handle_incident(
        demo_incident,
        world,
    )

    print(
        json.dumps(
            result.__dict__,
            indent=2,
        )
    )