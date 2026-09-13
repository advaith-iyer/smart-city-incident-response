"""
factory_module.py
Person C — Factory / Industrial Module
Smart City Incident Response Agent — Tech Zephyr 4.0

This module owns everything related to "factory accident / industrial
equipment failure / workplace hazard" type incidents. It implements the
standard 6-step agent loop (Observe -> Assess -> Decide -> Act ->
Monitor & Adapt -> Log) and exposes ONE function to the rest of the system:

    handle_incident(incident: dict) -> dict

The Orchestrator (built by Person D) should be able to import this file and
call handle_incident() directly, passing an Incident object that matches the
shared contract in Section 3 of the pipeline doc, and receiving back a
ModuleOutput object that matches the same contract. No other function in
this file needs to be touched by anyone outside this module.

Scope note: if the incident is primarily a FIRE or MEDICAL emergency (no
factory-specific hazard keyword present), this module does NOT try to run
the full response itself. It returns a ModuleOutput that flags the redirect
target (see log["redirect_target"]) so the Orchestrator can route it to the
Fire or Medical module instead, per the project's multi-module handling.

Swap points for teammates / later improvement:
  - assess_severity_llm() is a stub. Wire in a real LLM call there if you
    want smarter severity/decision reasoning than the keyword-based default.
  - Factory world data currently loads from factory_world.json (a standalone
    dev file). Once merged into the shared city_state.json, just copy the
    "factories" and "district_industrial_authority" keys into that file and
    point WORLD_PATH there -- load_world() only ever reads world["factories"],
    so nothing else changes.
"""

import json
import math
import os
import time
import uuid
from datetime import datetime

from shared.contracts import Incident, ModuleOutput

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WORLD_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../shared/city_state.json")
)
AVERAGE_SPEED_KMPH = 30  # used for mutual-aid ETA estimates between factories

# Keyword banks used for rule-based incident classification.
# (Replace/augment with an LLM call via assess_severity_llm() if desired.)
FACTORY_KEYWORDS = {
    # Order matters: checked top-to-bottom, first match wins.
    "gas_leak": ["gas leak", "gas smell", "toxic fumes", "leaking cylinder"],
    "chemical_spill": ["chemical spill", "acid spill", "chemical leak", "toxic spill", "runoff"],
    "electrical_failure": ["electrical fire", "sparking", "short circuit", "power surge",
                            "blackout", "electrical failure", "exposed wiring"],
    "machine_failure": ["machine stuck", "machine jammed", "equipment malfunction",
                         "machine failure", "conveyor stuck", "press malfunction",
                         "is stuck", "jammed", "malfunction", "breakdown", "equipment failure"],
    "workplace_hazard": ["workplace hazard", "unsafe condition", "structural damage",
                          "falling debris", "blocked exit"],
}
FIRE_KEYWORDS = ["fire", "flames", "burning", "smoke everywhere", "explosion"]
MEDICAL_KEYWORDS = ["injured", "injury", "unconscious", "not breathing", "bleeding",
                     "collapsed", "worker down", "burned", "heart attack"]

# Base severity per classified category before situational adjustments.
CATEGORY_BASE_SEVERITY = {
    "gas_leak": 4,
    "chemical_spill": 4,
    "electrical_failure": 3,
    "machine_failure": 2,
    "workplace_hazard": 2,
    "fire_and_medical": 5,
    "fire_primary": 4,
    "medical_primary": 3,
}

# Which specialist role handles which category on-site.
CATEGORY_SPECIALIST = {
    "gas_leak": "safety_officer",
    "chemical_spill": "safety_officer",
    "electrical_failure": "electrician",
    "machine_failure": "technician",
    "workplace_hazard": "safety_officer",
}

# Categories that should always spin up a hazard/danger zone.
HAZARD_ZONE_CATEGORIES = {"gas_leak", "chemical_spill"}
HAZARD_RADIUS_BASE_KM = {
    "gas_leak": 0.5,
    "chemical_spill": 0.4,
    "electrical_failure": 0.1,
    "machine_failure": 0.05,
    "workplace_hazard": 0.1,
}


# ---------------------------------------------------------------------------
# World / resource directory
# ---------------------------------------------------------------------------

def load_world(path=WORLD_PATH):
    """Load the shared factory directory (factories, safety teams, equipment)."""
    with open(path, "r") as f:
        return json.load(f)


def haversine_km(lat1, lng1, lat2, lng2):
    """Straight-line distance in km between two lat/lng points."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def eta_minutes(distance_km, speed_kmph=AVERAGE_SPEED_KMPH):
    return round((distance_km / speed_kmph) * 60, 1)


def road_aware_distance_km(world, lat1, lng1, lat2, lng2):
    """
    Distance used for mutual-aid ETA calculations (request_mutual_aid).

    Uses the shared road graph (shared/road_graph.py), which respects
    blocked roads via Dijkstra shortest-path, when the world provides
    one. Falls back to straight-line distance if the world has no road
    graph or the graph lookup fails for any reason, so this never
    crashes because of a missing/malformed roads section.

    Note: resolve_factory() above still uses plain haversine_km -- that
    call is identifying WHICH factory an incident belongs to (nearest
    building to a point), not a travel-time estimate, so straight-line
    distance is the right tool there and is left unchanged.
    """
    roads = world.get("roads") if world else None
    if roads:
        try:
            from shared.road_graph import route_distance_km
            return route_distance_km(roads, lat1, lng1, lat2, lng2)["distance_km"]
        except Exception:
            pass
    return haversine_km(lat1, lng1, lat2, lng2)


def resolve_factory(world, incident):
    """
    Find the factory this incident belongs to: by explicit factory_id if the
    Incident carries one, otherwise the nearest factory to the reported
    location. Returns None if the world has no factory records at all.
    """
    factories = world.get("factories", [])
    if not factories:
        return None

    factory_id = incident.get("factory_id")
    if factory_id:
        for fac in factories:
            if fac["id"] == factory_id:
                return fac

    lat, lng = incident["location"]["lat"], incident["location"]["lng"]
    scored = [
        (
            haversine_km(
                lat,
                lng,
                f["location"]["lat"],
                f["location"]["lng"]
            ),
            f
        )
        for f in factories
    ]
    scored.sort(key=lambda x: x[0])
    return scored[0][1]


def pick_section(factory, incident):
    """Pick the relevant section within a factory for this incident."""
    if not factory:
        return None

    sections = factory.get("sections", [])
    if not sections:
        return None

    section_id = incident.get("section_id")
    if section_id:
        for sec in sections:
            if sec["section_id"] == section_id:
                return sec

    for sec in sections:
        if sec.get("occupied"):
            return sec

    return sections[0]


def find_available_personnel(factory, role):
    """Return the first available on-site staff member with the given role."""
    if not factory:
        return None

    for person in factory.get("safety_team", []):
        if person.get("role") == role and person.get("status") == "available":
            return person

    return None


def find_available_equipment(factory, equipment_type, quantity_needed=1):
    """Return the first equipment entry of the given type with enough units free."""
    if not factory:
        return None

    for item in factory.get("equipment", []):
        if item.get("type") == equipment_type and item.get("available", 0) >= quantity_needed:
            return item

    return None


def request_mutual_aid(world, requesting_factory, role, incident_lat, incident_lng):
    """
    Escalation tier 3: the requesting factory doesn't have the needed
    specialist on-site, so scan other factories in the world data for one
    that's available, nearest first. Simulates the request by marking that
    person "dispatched" at their home factory.
    Returns (other_factory, person, distance_km) or (None, None, None).
    """
    requesting_id = requesting_factory["id"] if requesting_factory else None
    candidates = []

    for fac in world.get("factories", []):
        if fac["id"] == requesting_id:
            continue

        person = find_available_personnel(fac, role)
        if person:
            dist = road_aware_distance_km(
                world,
                incident_lat,
                incident_lng,
                fac["location"]["lat"],
                fac["location"]["lng"]
            )
            candidates.append((dist, fac, person))

    if not candidates:
        return None, None, None

    candidates.sort(key=lambda x: x[0])
    dist, fac, person = candidates[0]
    return fac, person, dist


# ---------------------------------------------------------------------------
# Incident adapter
# ---------------------------------------------------------------------------

def normalize_incident(incident):
    """Convert shared Incident to the dictionary format used internally."""
    if isinstance(incident, Incident):
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

        # Preserve factory-specific fields passed through severity_inputs
        data.update(incident.severity_inputs or {})
        return data

    if isinstance(incident, dict):
        incident = dict(incident)

        incident.setdefault(
            "raw_description",
            incident.get("description", "")
        )
        incident.setdefault(
            "description",
            incident.get("raw_description", "")
        )
        incident.setdefault(
            "incident_type",
            incident.get("type")
        )
        incident.setdefault(
            "type",
            incident.get("incident_type")
        )
        incident.setdefault(
            "reported_by",
            incident.get("reporter")
        )
        incident.setdefault(
            "reporter",
            incident.get("reported_by")
        )
        incident.setdefault("confidence", 1.0)
        incident.setdefault("status", "reported")

        return incident

    raise TypeError(
        f"Factory module expected Incident or dict, got {type(incident).__name__}"
    )


# ---------------------------------------------------------------------------
# Step 1/2 support: Classification
# ---------------------------------------------------------------------------

def classify_incident(incident):
    """
    Rule-based classification of the incident into a factory-specific
    category, or a flag that this is primarily a fire/medical emergency
    that belongs to a different module.
    Returns a dict: {category, redirect, requires_fire_support, requires_medical_support}
    """
    text = incident.get("raw_description", "").lower()

    factory_category = None
    for category, keywords in FACTORY_KEYWORDS.items():
        if any(k in text for k in keywords):
            factory_category = category
            break

    fire_match = any(k in text for k in FIRE_KEYWORDS)
    medical_match = any(k in text for k in MEDICAL_KEYWORDS)

    if factory_category:
        return {
            "category": factory_category,
            "redirect": None,
            "requires_fire_support": fire_match,
            "requires_medical_support": medical_match,
        }

    if fire_match and medical_match:
        return {
            "category": "fire_and_medical",
            "redirect": "fire",
            "requires_fire_support": True,
            "requires_medical_support": True
        }

    if fire_match:
        return {
            "category": "fire_primary",
            "redirect": "fire",
            "requires_fire_support": True,
            "requires_medical_support": False
        }

    if medical_match:
        return {
            "category": "medical_primary",
            "redirect": "medical",
            "requires_fire_support": False,
            "requires_medical_support": True
        }

    # No strong signal either way -- default to a generic factory hazard so
    # the module still produces a safe, conservative response.
    return {
        "category": "workplace_hazard",
        "redirect": None,
        "requires_fire_support": False,
        "requires_medical_support": False
    }


# ---------------------------------------------------------------------------
# Worker accountability (used by both Assess and Act)
# ---------------------------------------------------------------------------

def check_worker_accountability(factory, incident, missing_override=None):
    """
    Simple simulated headcount check for the affected section.
    missing_override lets monitor_and_adapt() recompute this mid-response
    using a fresh _simulate_worker_missing value.
    """
    section = pick_section(factory, incident)
    roster = section.get("worker_roster", []) if section else []
    present = len(roster)

    missing_flag = (
        missing_override
        if missing_override is not None
        else incident.get("_simulate_worker_missing", 0)
    )

    if isinstance(missing_flag, list):
        missing_ids = [w for w in missing_flag if w in roster]
    else:
        count = int(missing_flag) if missing_flag else 0
        missing_ids = roster[-count:] if count > 0 else []

    accounted = present - len(missing_ids)

    return {
        "section_id": section.get("section_id") if section else None,
        "section_name": section.get("name") if section else None,
        "workers_present": present,
        "workers_accounted": accounted,
        "missing_workers": missing_ids,
    }


# ---------------------------------------------------------------------------
# Step 2: Assess
# ---------------------------------------------------------------------------

def assess_severity_rule_based(incident, factory, classification, accountability):
    """
    Default severity assessment using the classified category plus
    situational factors observable from the incident and factory data.
    Returns (score 1-5, reasoning string).
    """
    category = classification["category"]
    score = CATEGORY_BASE_SEVERITY.get(category, 2)
    reasons = [f"Classified as '{category}'."]

    if factory and factory.get("hazmat_on_site") and category in ("gas_leak", "chemical_spill"):
        score += 1
        reasons.append("Hazardous material is present on-site, amplifying risk.")

    if incident.get("containment_status") == "uncontained":
        score += 1
        reasons.append("Problem is reported as uncontained/spreading.")

    if category == "machine_failure":
        section = pick_section(factory, incident)
        section_id = section.get("section_id") if section else None

        running = any(
            m.get("section_id") == section_id and m.get("status") == "running"
            for m in (factory.get("machinery", []) if factory else [])
        )

        if running:
            score += 1
            reasons.append("Affected machinery is still running.")

    people_affected = incident.get("people_affected", 0)

    if people_affected and people_affected >= 3:
        score += 1
        reasons.append(f"Multiple people affected ({people_affected}).")

    if accountability["missing_workers"]:
        score += 1
        reasons.append(
            f"{len(accountability['missing_workers'])} worker(s) unaccounted for."
        )

    score = max(1, min(5, score))

    if incident.get("confidence", 0.5) < 0.5:
        reasons.append(
            "Confidence is low; recommend verification alongside response."
        )

    return score, " ".join(reasons)


def assess_severity_llm(incident):
    """
    OPTIONAL: plug in a real LLM call here for smarter assessment, e.g.:

        from anthropic import Anthropic
        client = Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": f"Assess factory/industrial incident severity 1-5 "
                            f"and explain why: {incident['raw_description']}"
            }]
        )
        # parse response.content[0].text into (score, reasoning)

    Not wired in by default so this module runs with zero API dependencies
    during development/testing. Swap the call in assess_severity() below
    once an API key / client is available.
    """
    raise NotImplementedError("Wire in an LLM call here if desired.")


def assess_severity(incident, factory, classification, accountability):
    """Entry point used by handle_incident(). Uses rule-based scoring by default."""
    try:
        if os.environ.get("USE_LLM_SEVERITY") == "1":
            return assess_severity_llm(incident)
    except NotImplementedError:
        pass

    return assess_severity_rule_based(
        incident,
        factory,
        classification,
        accountability
    )


# ---------------------------------------------------------------------------
# Step 3: Decide
# ---------------------------------------------------------------------------

def decide_action(incident, severity_score, classification):
    """
    Escalation ladder: on-site safety team -> nearby specialist (mutual aid)
    -> district industrial safety authority. Returns a plan dict describing
    what tier of response to invoke.
    """
    if severity_score >= 5:
        return {
            "escalation_level": "multi-agency",
            "plan": "district_authority_and_evacuate",
            "reasoning": (
                "Critical severity: evacuate affected section and bring in the district "
                "industrial safety authority alongside on-site response."
            )
        }

    elif severity_score == 4:
        return {
            "escalation_level": "professional",
            "plan": "specialist_and_mutual_aid",
            "reasoning": (
                "High severity: dispatch on-site specialist and pre-arrange nearby-factory "
                "mutual aid in case on-site resources are insufficient."
            )
        }

    elif severity_score == 3:
        return {
            "escalation_level": "professional",
            "plan": "onsite_specialist",
            "reasoning": (
                "Moderate severity: on-site specialist handles containment; "
                "monitor for escalation."
            )
        }

    else:
        return {
            "escalation_level": "local",
            "plan": "safety_team_containment",
            "reasoning": (
                "Low severity: on-site safety team containment is sufficient; "
                "no external dispatch needed."
            )
        }


# ---------------------------------------------------------------------------
# Step 4: Act
# ---------------------------------------------------------------------------

IMMEDIATE_CONTAINMENT_ACTIONS = {
    "gas_leak": "Requested shutdown/isolation of gas supply and restricted access to the affected area",
    "chemical_spill": "Initiated spill containment procedure and restricted access to the affected area",
    "electrical_failure": "Isolated the affected electrical circuit/equipment",
    "machine_failure": "Requested shutdown/isolation of the affected machinery",
    "workplace_hazard": "Restricted access to the hazard area",
}


def create_hazard_zone(incident, classification, severity_score):
    """Simple radius-based danger zone for hazard-worthy categories."""
    category = classification["category"]

    if category not in HAZARD_ZONE_CATEGORIES and severity_score < 4:
        return None

    base_radius = HAZARD_RADIUS_BASE_KM.get(category, 0.1)
    radius_km = base_radius * (1.5 if severity_score >= 4 else 1.0)

    return {
        "zone_id": f"HZ-{uuid.uuid4().hex[:8]}",
        "center": {
            "lat": incident["location"]["lat"],
            "lng": incident["location"]["lng"]
        },
        "radius_km": round(radius_km, 2),
        "category": category,
    }


def act(world, incident, plan, factory, classification, accountability, timeline):
    """
    Executes the plan: immediate containment, specialist notification,
    mutual aid, evacuation/worker accountability, and hazard zoning.
    "Notification" is simulated as a log line, same as the Medical Module.
    """
    actions_taken = []
    resources_allocated = []
    reporter_updates = []

    category = classification["category"]
    lat = incident["location"]["lat"]
    lng = incident["location"]["lng"]

    def now():
        return datetime.utcnow().isoformat()

    def log_event(text):
        actions_taken.append({
            "action": text,
            "timestamp": now()
        })
        timeline.append({
            "time": now(),
            "event": text
        })

    if not factory:
        log_event(
            "No factory record found for this location; treating as a generic industrial site."
        )

    # 1. Immediate containment
    containment_msg = IMMEDIATE_CONTAINMENT_ACTIONS.get(category)

    if containment_msg:
        log_event(containment_msg)
        reporter_updates.append(containment_msg + ".")

    # 2. On-site specialist notification
    # Skip for local-only plan; safety team handles it silently.
    specialist_role = CATEGORY_SPECIALIST.get(category)

    if (
        plan["plan"] != "safety_team_containment"
        and specialist_role
        and factory
    ):
        person = find_available_personnel(factory, specialist_role)

        if person:
            person["status"] = "dispatched"

            log_event(
                f"Notified on-site {specialist_role.replace('_', ' ')} ({person['name']})"
            )

            resources_allocated.append({
                "resource_type": specialist_role,
                "resource_id": person["id"],
                "eta": "on-site"
            })

            reporter_updates.append(
                f"{person['name']} ({specialist_role.replace('_', ' ')}) "
                f"has been notified and is responding on-site."
            )

        else:
            # Tier 3: nearby factory mutual aid
            other_factory, other_person, dist = request_mutual_aid(
                world,
                factory,
                specialist_role,
                lat,
                lng
            )

            if other_person:
                other_person["status"] = "dispatched"
                eta = eta_minutes(dist)

                log_event(
                    f"No {specialist_role.replace('_', ' ')} on-site - "
                    f"requested mutual aid from {other_factory['name']}"
                )

                resources_allocated.append({
                    "resource_type": f"mutual_aid_{specialist_role}",
                    "resource_id": other_person["id"],
                    "eta": f"{eta} min"
                })

                reporter_updates.append(
                    f"{other_factory['name']} is sending a "
                    f"{specialist_role.replace('_', ' ')} (ETA {eta} min)."
                )

            else:
                # Tier 4: district authority
                authority = world.get("district_industrial_authority")

                log_event(
                    f"No {specialist_role.replace('_', ' ')} available on-site "
                    f"or at nearby factories - escalating to district "
                    f"industrial safety authority"
                )

                if authority:
                    resources_allocated.append({
                        "resource_type": "district_authority",
                        "resource_id": authority["id"],
                        "eta": "N/A"
                    })

                reporter_updates.append(
                    "District Industrial Safety Authority has been notified "
                    "due to lack of available specialist resources."
                )

    # 3. Safety team on-site containment for low-severity local cases
    if plan["plan"] == "safety_team_containment" and factory:
        officer = find_available_personnel(factory, "safety_officer")

        if officer:
            officer["status"] = "dispatched"

            log_event(
                f"On-site safety team ({officer['name']}) handling containment"
            )

            resources_allocated.append({
                "resource_type": "safety_officer",
                "resource_id": officer["id"],
                "eta": "on-site"
            })

            reporter_updates.append(
                f"{officer['name']} from the on-site safety team is handling containment."
            )

    # 4. Evacuation + worker accountability for higher-severity / spreading incidents
    if plan["plan"] in (
        "specialist_and_mutual_aid",
        "district_authority_and_evacuate"
    ):
        section_name = accountability.get("section_name") or "the affected section"

        log_event(f"Initiated evacuation of {section_name}")

        reporter_updates.append(
            f"Evacuation of {section_name} has been initiated."
        )

        log_event(
            f"Headcount check: "
            f"{accountability['workers_accounted']}/"
            f"{accountability['workers_present']} workers accounted for"
        )

        if accountability["missing_workers"]:
            log_event(
                f"{len(accountability['missing_workers'])} worker(s) "
                f"unaccounted for: "
                f"{', '.join(accountability['missing_workers'])}"
            )

            reporter_updates.append(
                f"{len(accountability['missing_workers'])} worker(s) are "
                f"currently unaccounted for; search is underway."
            )

    # 5. District authority notification for critical incidents
    if plan["plan"] == "district_authority_and_evacuate":
        authority = world.get("district_industrial_authority")

        if authority and not any(
            r["resource_type"] == "district_authority"
            for r in resources_allocated
        ):
            log_event(
                "Notified district industrial safety authority due to critical severity"
            )

            resources_allocated.append({
                "resource_type": "district_authority",
                "resource_id": authority["id"],
                "eta": "N/A"
            })

            reporter_updates.append(
                "District Industrial Safety Authority has been notified "
                "and is coordinating the response."
            )

    # 6. Hazard zone
    hazard_zone = create_hazard_zone(
        incident,
        classification,
        plan.get("_severity_for_zone", 0)
    )

    if hazard_zone:
        log_event(
            f"Established hazard zone "
            f"(radius {hazard_zone['radius_km']} km) around incident location"
        )

        reporter_updates.append(
            f"A {hazard_zone['radius_km']} km hazard zone has been "
            f"established around the site. Please stay clear."
        )

        world.setdefault("system_state", {}).setdefault(
            "alerts", []
        ).append(
            f"Hazard zone {hazard_zone['zone_id']} active near "
            f"{incident['location'].get('area_name', 'incident location')}"
        )

    return (
        actions_taken,
        resources_allocated,
        reporter_updates,
        hazard_zone
    )


# ---------------------------------------------------------------------------
# Step 5: Monitor & Adapt
# ---------------------------------------------------------------------------

def monitor_and_adapt(
    world,
    incident,
    severity_score,
    plan,
    factory,
    classification,
    accountability,
    actions_taken,
    resources_allocated,
    reporter_updates,
    timeline,
    hazard_zone
):
    """
    Simulates a monitoring check after initial response, using the same
    _simulate_* flag pattern as the Medical Module's _simulate_escalation.
    Reacts to: severity increasing, a resource becoming unavailable, a
    worker turning up missing, the incident spreading, or external
    assistance becoming necessary -- re-planning instead of sticking with
    the original response where needed.
    """

    def now():
        return datetime.utcnow().isoformat()

    def log_event(text):
        actions_taken.append({
            "action": text,
            "timestamp": now()
        })
        timeline.append({
            "time": now(),
            "event": text
        })

    triggered = False

    # Worker turns up missing mid-response
    if incident.get("_simulate_worker_missing"):
        new_accountability = check_worker_accountability(
            factory,
            incident
        )

        if len(new_accountability["missing_workers"]) > len(
            accountability["missing_workers"]
        ):
            accountability.update(new_accountability)
            severity_score = min(5, severity_score + 1)

            log_event(
                f"Monitoring update: "
                f"{len(accountability['missing_workers'])} worker(s) now "
                f"confirmed unaccounted for; severity raised to {severity_score}"
            )

            reporter_updates.append(
                "Update: additional workers are unaccounted for. "
                "Search efforts have been escalated."
            )

            triggered = True

    # A previously allocated resource becomes unavailable
    if incident.get("_simulate_resource_unavailable") and resources_allocated:
        lost = resources_allocated.pop(0)

        log_event(
            f"Monitoring update: allocated resource "
            f"({lost['resource_type']}) became unavailable - seeking alternative"
        )

        specialist_role = CATEGORY_SPECIALIST.get(
            classification["category"]
        )

        if specialist_role and factory:
            lat = incident["location"]["lat"]
            lng = incident["location"]["lng"]

            other_factory, other_person, dist = request_mutual_aid(
                world,
                factory,
                specialist_role,
                lat,
                lng
            )

            if other_person:
                other_person["status"] = "dispatched"
                eta = eta_minutes(dist)

                resources_allocated.append({
                    "resource_type": f"mutual_aid_{specialist_role}",
                    "resource_id": other_person["id"],
                    "eta": f"{eta} min"
                })

                reporter_updates.append(
                    f"Backup {specialist_role.replace('_', ' ')} arranged "
                    f"from {other_factory['name']} (ETA {eta} min)."
                )

            else:
                reporter_updates.append(
                    "No backup specialist resource could be found; "
                    "district authority remains the fallback."
                )

        triggered = True

    # Incident spreads -- widen hazard zone
    if incident.get("_simulate_spread") and hazard_zone:
        hazard_zone["radius_km"] = round(
            hazard_zone["radius_km"] * 1.5,
            2
        )

        log_event(
            f"Monitoring update: incident has spread - hazard zone radius "
            f"increased to {hazard_zone['radius_km']} km"
        )

        reporter_updates.append(
            f"The incident has spread. The hazard zone has been widened "
            f"to {hazard_zone['radius_km']} km."
        )

        triggered = True

    # External assistance becomes necessary regardless of current plan
    if (
        incident.get("_simulate_external_needed")
        and plan["escalation_level"] != "multi-agency"
    ):
        authority = world.get("district_industrial_authority")

        log_event(
            "Monitoring update: external assistance now required - "
            "escalating to district industrial safety authority"
        )

        if authority and not any(
            r["resource_type"] == "district_authority"
            for r in resources_allocated
        ):
            resources_allocated.append({
                "resource_type": "district_authority",
                "resource_id": authority["id"],
                "eta": "N/A"
            })

        reporter_updates.append(
            "Situation now requires external assistance; District Industrial "
            "Safety Authority has been engaged."
        )

        plan = {
            **plan,
            "escalation_level": "multi-agency"
        }

        triggered = True

    # General severity increase -- re-decide and re-act
    if incident.get("_simulate_severity_increase"):
        old_score = severity_score
        severity_score = min(5, severity_score + 2)

        log_event(
            f"Monitoring update: condition worsened, severity re-assessed "
            f"from {old_score} to {severity_score}"
        )

        reporter_updates.append(
            "Update: the situation has escalated. "
            "A higher-priority response is now being dispatched."
        )

        new_plan = decide_action(
            incident,
            severity_score,
            classification
        )

        extra_actions, extra_resources, extra_updates, _ = act(
            world,
            incident,
            new_plan,
            factory,
            classification,
            accountability,
            timeline
        )

        actions_taken.extend(extra_actions)
        resources_allocated.extend(extra_resources)
        reporter_updates.extend(extra_updates)

        plan = new_plan
        triggered = True

    adapt_status = "escalated" if triggered else "no_change"

    return severity_score, plan, adapt_status


# ---------------------------------------------------------------------------
# Step 6: Log
# ---------------------------------------------------------------------------

def build_log(
    incident,
    severity_score,
    plan,
    resources_allocated,
    accountability,
    adapt_status,
    timeline,
    start_time,
    classification
):
    response_time_sec = round(time.time() - start_time, 3)
    bottlenecks = []

    if not resources_allocated:
        bottlenecks.append("No resource could be allocated.")

    if adapt_status == "escalated":
        bottlenecks.append(
            "Initial assessment underestimated the incident; "
            "required re-escalation."
        )

    if accountability["missing_workers"]:
        bottlenecks.append(
            f"{len(accountability['missing_workers'])} worker(s) "
            f"remained unaccounted for during response."
        )

    summary = (
        f"{classification['category'].replace('_', ' ').title()} incident "
        f"resolved at severity {severity_score}/5 "
        f"({plan['escalation_level']} escalation) using "
        f"{len(resources_allocated)} resource(s) in {response_time_sec}s. "
        f"Worker accountability: "
        f"{accountability['workers_accounted']}/"
        f"{accountability['workers_present']} accounted for."
    )

    return {
        "incident_id": incident["incident_id"],
        "response_time_sec": response_time_sec,
        "final_severity": severity_score,
        "category": classification["category"],
        "escalation_level": plan["escalation_level"],
        "resources_used": len(resources_allocated),
        "worker_accountability": accountability,
        "timeline": timeline,
        "bottlenecks": bottlenecks if bottlenecks else ["None"],
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Main entry point — this is what the Orchestrator calls
# ---------------------------------------------------------------------------

def handle_incident(incident, world=None):
    """
    Runs the full 6-step loop for one factory/industrial incident and
    returns a ModuleOutput object matching the shared contract:

        ModuleOutput(
            incident_id,
            severity,
            actions_taken,
            resources_allocated,
            escalation_level,
            status,
            reporter_updates,
            log
        )
    """
    start_time = time.time()

    # Accept the shared Incident contract while preserving
    # the module's existing internal dictionary-based logic.
    incident = normalize_incident(incident)

    if world is None:
        world = load_world()

    timeline = []

    def now():
        return datetime.utcnow().isoformat()

    timeline.append({
        "time": now(),
        "event": "Incident reported"
    })

    # 1. Observe -- incident is already the observation, nothing to do here
    #    beyond having received it as an argument.

    # 1b. Classify (factory-specific vs. fire/medical redirect)
    classification = classify_incident(incident)
    factory = resolve_factory(world, incident)
    accountability = check_worker_accountability(
        factory,
        incident
    )

    # 2. Assess
    severity_score, severity_reasoning = assess_severity(
        incident,
        factory,
        classification,
        accountability
    )

    timeline.append({
        "time": now(),
        "event": f"Severity assessed as {severity_score}/5"
    })

    # Redirect path: primarily a fire or medical emergency, not ours to run.
    if classification["redirect"]:
        target = classification["redirect"]

        reason_label = (
            "fire and medical emergency"
            if classification["category"] == "fire_and_medical"
            else f"{target} emergency"
        )

        redirect_msg = (
            f"Classified as primarily a {reason_label} - redirecting to "
            f"the {target.capitalize()} Module for full handling"
        )

        timeline.append({
            "time": now(),
            "event": redirect_msg
        })

        actions_taken = [{
            "action": redirect_msg,
            "timestamp": now()
        }]

        reporter_updates = [
            f"This appears to be primarily a {target} emergency. "
            f"The {target.capitalize()} Module has been notified and will "
            f"lead the response; the on-site factory safety team remains "
            f"on standby to assist."
        ]

        resources_allocated = []

        if factory:
            officer = find_available_personnel(
                factory,
                "safety_officer"
            )

            if officer:
                resources_allocated.append({
                    "resource_type": "safety_officer_standby",
                    "resource_id": officer["id"],
                    "eta": "on-site"
                })

        log = build_log(
            incident,
            severity_score,
            {"escalation_level": "multi-agency"},
            resources_allocated,
            accountability,
            "redirected",
            timeline,
            start_time,
            classification
        )

        log["redirect_target"] = target

        return ModuleOutput(
            incident_id=incident["incident_id"],
            severity={
                "score": severity_score,
                "reasoning": severity_reasoning
            },
            actions_taken=actions_taken,
            resources_allocated=resources_allocated,
            escalation_level="multi-agency",
            status="escalated",
            reporter_updates=reporter_updates,
            log=log,
        )

    # 3. Decide
    plan = decide_action(
        incident,
        severity_score,
        classification
    )

    plan["_severity_for_zone"] = severity_score

    timeline.append({
        "time": now(),
        "event": f"Decided plan: {plan['plan']}"
    })

    # 4. Act
    actions_taken, resources_allocated, reporter_updates, hazard_zone = act(
        world,
        incident,
        plan,
        factory,
        classification,
        accountability,
        timeline
    )

    # 5. Monitor & Adapt
    severity_score, plan, adapt_status = monitor_and_adapt(
        world,
        incident,
        severity_score,
        plan,
        factory,
        classification,
        accountability,
        actions_taken,
        resources_allocated,
        reporter_updates,
        timeline,
        hazard_zone
    )

    # 6. Log
    log = build_log(
        incident,
        severity_score,
        plan,
        resources_allocated,
        accountability,
        adapt_status,
        timeline,
        start_time,
        classification
    )

    if hazard_zone:
        log["hazard_zone"] = hazard_zone

    status = "resolved" if resources_allocated else "escalated"

    return ModuleOutput(
        incident_id=incident["incident_id"],
        severity={
            "score": severity_score,
            "reasoning": severity_reasoning
        },
        actions_taken=actions_taken,
        resources_allocated=resources_allocated,
        escalation_level=plan["escalation_level"],
        status=status,
        reporter_updates=reporter_updates,
        log=log,
    )


if __name__ == "__main__":
    # Quick smoke tests using demo incidents, run directly:
    #   python factory_module.py
    world = load_world()

    demo_incident_1 = {
        "incident_id": "INC-F001",
        "type": "factory",
        "location": {
            "lat": 21.269,
            "lng": 81.652,
            "area_name": "Riverside"
        },
        "reported_by": "reporter_demo",
        "raw_description": "Chemical spill in the mixing hall, area not yet contained.",
        "timestamp": datetime.utcnow().isoformat(),
        "confidence": 0.9,
        "status": "reported",
        "factory_id": "F2",
        "containment_status": "uncontained",
        "_simulate_worker_missing": 1,
    }

    print("=== Demo 1: Chemical spill (factory-specific) ===")
    print(json.dumps(
        handle_incident(demo_incident_1, world),
        indent=2,
        default=str
    ))

    demo_incident_2 = {
        "incident_id": "INC-F002",
        "type": "factory",
        "location": {
            "lat": 21.242,
            "lng": 81.615,
            "area_name": "Old Town"
        },
        "reported_by": "reporter_demo",
        "raw_description": "Weaving loom is stuck and making a loud grinding noise.",
        "timestamp": datetime.utcnow().isoformat(),
        "confidence": 0.8,
        "status": "reported",
        "factory_id": "F3",
        "containment_status": "uncontained",
    }

    print("\n=== Demo 2: Machine failure, no on-site technician (mutual aid path) ===")
    print(json.dumps(
        handle_incident(demo_incident_2, world),
        indent=2,
        default=str
    ))

    demo_incident_3 = {
        "incident_id": "INC-F003",
        "type": "factory",
        "location": {
            "lat": 21.253,
            "lng": 81.633,
            "area_name": "Central City"
        },
        "reported_by": "reporter_demo",
        "raw_description": "Worker collapsed and is unconscious near the loading bay.",
        "timestamp": datetime.utcnow().isoformat(),
        "confidence": 0.85,
        "status": "reported",
        "factory_id": "F1",
    }

    print("\n=== Demo 3: Medical emergency (should redirect) ===")
    print(json.dumps(
        handle_incident(demo_incident_3, world),
        indent=2,
        default=str
    ))