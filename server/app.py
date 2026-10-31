"""
server/app.py

FastAPI backend for the Smart City Incident Response Agent.

This wraps the existing orchestrator/modules with:
  - A live in-memory world + incident store (so the dashboard shows a
    genuinely evolving system state, not a static replay).
  - REST endpoints to report new incidents, send follow-up "monitoring"
    updates that trigger real re-assessment/adaptation, and pull a live
    priority-ordered incident feed + rollup analytics.
  - A background loop that periodically re-checks in-progress incidents
    for resource/road changes, matching the "Dynamic incident updates /
    continuously monitored" behavior from the original design brief.
  - Serves the static dashboard (server/static/index.html).

Run from the project root:
    uvicorn server.app:app --reload
Then open http://127.0.0.1:8000
"""

import asyncio
import copy
import json
import sys
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from shared.contracts import Incident, ModuleOutput  # noqa: E402
from shared.world_state import load_world  # noqa: E402
from orchestrator.orchestrator import (  # noqa: E402
    FALSE_REPORT_THRESHOLD,
    MULTI_MODULE_RULES,
    dispatch_to_module,
    priority_score,
    is_false_report,
    route_incident,
    serialize_result,
)
from orchestrator.cascade import generate_cascades
from orchestrator.reallocation import (
    find_reallocation_candidate,
    release_ambulance,
)
from agent.decision_engine import DecisionEngine
from agent.trace import AgentTrace

# ---------------------------------------------------------------------------
# App + shared mutable state
# ---------------------------------------------------------------------------

app = FastAPI(title="Smart City Incident Response Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WORLD = load_world()          # single shared, mutable "world" every module reads/writes
INCIDENTS: dict[str, dict] = {}  # incident_id -> record
AGENT_TRACES: dict[str, list[dict]] = {}  # incident_id -> agent trace

# Sync endpoints run in a threadpool while the monitoring loop runs in its own
# thread, so every read/write of WORLD/INCIDENTS goes through this lock.
STATE_LOCK = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def _serialize(result):
    if isinstance(result, list):
        return [_serialize(r) for r in result]
    if isinstance(result, ModuleOutput):
        return serialize_result(result)
    return result  # already a plain dict (unhandled / false report)


def _incident_public(incident: Incident) -> dict:
    d = asdict(incident)
    return d


def _extract_logs(serialized):
    """Pull out every log dict from a (possibly multi-module) serialized result."""
    logs = []
    items = serialized if isinstance(serialized, list) else [serialized]
    for item in items:
        if isinstance(item, dict) and "log" in item and isinstance(item["log"], dict):
            logs.append(item["log"])
    return logs


# ---------------------------------------------------------------------------
# Resource ledger
# ---------------------------------------------------------------------------
# Modules mark units "dispatched" / bump facility load every time they run.
# Before an existing incident is re-run (follow-up update, monitoring
# re-check) we hand back whatever it currently holds, so the module
# re-plans against true availability instead of double-booking.

UNIT_POOLS = {"ambulance": "ambulances", "fire_tender": "fire_units"}
FACILITY_POOLS = {"hospital": "hospitals", "dispensary": "dispensaries"}
# Listed in resources_allocated but never actually reserved in the world.
NON_COMMITTED_TYPES = {
    "safety_officer_standby",
    "district_authority",
    "traffic_control_point",
}


def _result_items(serialized) -> list[dict]:
    items = serialized if isinstance(serialized, list) else [serialized]
    return [item for item in items if isinstance(item, dict)]


def _held_resources(record: dict) -> list[dict]:
    held = []
    for item in _result_items(record.get("result")):
        for res in item.get("resources_allocated", []) or []:
            if not isinstance(res, dict) or res.get("status") == "reallocated":
                continue
            if res.get("resource_type") in NON_COMMITTED_TYPES:
                continue
            if res.get("resource_id"):
                held.append(res)
    return held


def _find_factory_person(person_id: str):
    for factory in WORLD.get("factories", []):
        for person in factory.get("safety_team", []):
            if person.get("id") == person_id:
                return person
    return None


def _release_resources(record: dict) -> list[str]:
    released = []
    for res in _held_resources(record):
        rtype, rid = res["resource_type"], res["resource_id"]

        if rtype in UNIT_POOLS:
            unit = next((u for u in WORLD.get(UNIT_POOLS[rtype], []) if u.get("id") == rid), None)
            if unit and unit.get("status") == "dispatched":
                unit["status"] = "available"
                released.append(rid)

        elif rtype in FACILITY_POOLS:
            fac = next((f for f in WORLD.get(FACILITY_POOLS[rtype], []) if f.get("id") == rid), None)
            if fac:
                if "current_load" in fac:
                    fac["current_load"] = max(0, fac["current_load"] - 1)
                if "available_capacity" in fac:
                    cap = fac.get("capacity", fac["available_capacity"] + 1)
                    fac["available_capacity"] = min(cap, fac["available_capacity"] + 1)
                released.append(f"{rid} bed")

        else:
            person = _find_factory_person(rid)
            if person and person.get("status") == "dispatched":
                person["status"] = "available"
                released.append(rid)
    return released


# ---------------------------------------------------------------------------
# Status + timeline helpers
# ---------------------------------------------------------------------------

def _overall_status(serialized) -> str:
    """One canonical status per incident, derived from all module results."""
    statuses = [item.get("status") or "unhandled" for item in _result_items(serialized)]
    if not statuses:
        return "unhandled"
    if all(s == "false_report" for s in statuses):
        return "false_report"
    if all(s == "unhandled" for s in statuses):
        return "unhandled"
    for s in ("escalated", "rerouted", "redirected"):
        if s in statuses:
            return s
    if all(s == "resolved" for s in statuses if s != "unhandled"):
        return "resolved"
    return statuses[0]


def _modules_for(incident: Incident) -> list[str]:
    modules = []
    primary = route_incident(incident)
    if primary:
        modules.append(primary)
    for flag, module_name in MULTI_MODULE_RULES.items():
        if incident.severity_inputs.get(flag) and module_name not in modules:
            modules.append(module_name)
    return modules


def _resource_label(res: dict) -> str:
    eta = res.get("eta")
    return f"{res.get('resource_type')} {res.get('resource_id')}" + (
        f" (ETA {eta})" if eta and eta not in {"n/a", "on-site"} else ""
    )


def _false_report_trace(incident: Incident) -> list[dict]:
    trace = AgentTrace()
    trace.add("OBSERVE", "Incident received", {"incident_type": incident.incident_type})
    trace.add(
        "GUARDRAIL",
        "Dispatch blocked: reporter confidence below threshold",
        {"confidence": incident.confidence, "threshold": FALSE_REPORT_THRESHOLD},
    )
    trace.add(
        "ADAPT",
        "Holding report for verification instead of dispatching",
        {"next_step": "await follow-up or verification"},
    )
    trace.add("EVALUATE", "No resources committed", {"status": "false_report"})
    return list(trace.get_events())


def _run_agent(incident: Incident, severity: float) -> list[dict]:
    agent = DecisionEngine(type("WorldAdapter", (), {
        "get_state": lambda self: {
            "resources": {
                "fire_units": sum(
                    1 for u in WORLD.get("fire_units", [])
                    if u.get("status") == "available"
                ),
                "ambulances": sum(
                    1 for u in WORLD.get("ambulances", [])
                    if u.get("status") == "available"
                ),
            }
        }
    })())

    agent_result = agent.decide({
        "type": incident.incident_type,
        "severity": severity,
        "location": incident.location,
    })
    return list(agent_result["trace"])


def _run_and_record(
    incident: Incident,
    event: str,
    note: str = "",
    allow_cascade: bool = True,
):
    with STATE_LOCK:
        return _run_and_record_locked(incident, event, note, allow_cascade)


def _run_and_record_locked(
    incident: Incident,
    event: str,
    note: str,
    allow_cascade: bool,
):
    existing = INCIDENTS.get(incident.incident_id)
    previous_status = existing.get("status") if existing else None
    previous_held = (
        sorted(r["resource_id"] for r in _held_resources(existing)) if existing else []
    )

    timeline: list[dict] = []

    def step(stage: str, title: str, detail: str = ""):
        timeline.append({
            "timestamp": now(),
            "stage": stage,
            "title": title,
            "detail": detail,
        })

    if existing:
        released = _release_resources(existing)
        step(
            "MONITOR",
            "Re-assessment triggered" if event != "auto_recheck" else "Monitoring re-check",
            note,
        )
        if released:
            step("RELEASE", "Returned resources to pool for re-planning", ", ".join(released))
    else:
        source = incident.severity_inputs.get("cascade_source")
        step(
            "RECEIVE",
            f"{incident.incident_type.replace('_', ' ').title()} report received",
            f"Cascade from {source}" if source else (incident.reporter or ""),
        )
        if note and event == "reported":
            step("CLASSIFY", "Incident type classified", note)

    false_report = is_false_report(incident)
    step(
        "VERIFY",
        "Low confidence: dispatch withheld" if false_report else "Report confidence accepted",
        f"confidence {incident.confidence:.2f} (threshold {FALSE_REPORT_THRESHOLD})",
    )

    if not false_report:
        modules = _modules_for(incident)
        step(
            "ROUTE",
            f"Routed to {' + '.join(m.title() for m in modules)} module"
            if modules else "No module registered for this type",
            f"priority score {priority_score(incident)}",
        )

    result = dispatch_to_module(incident, WORLD)

    if result is None:
        result = {
            "incident_id": incident.incident_id,
            "status": "unhandled",
            "message": (
                f"No response module is registered for "
                f"'{incident.incident_type}'."
            ),
        }

    serialized = _serialize(result)

    # Feed the module's computed severity into the agent trace.
    agent_severity = 0
    for item in _result_items(serialized):
        severity_data = item.get("severity", {})
        if isinstance(severity_data, dict):
            score = severity_data.get("score", 0)
            if isinstance(score, (int, float)):
                agent_severity = max(agent_severity, score)

    if not false_report:
        for item in _result_items(serialized):
            sev = item.get("severity")
            if isinstance(sev, dict) and sev.get("score") is not None:
                step(
                    "ASSESS",
                    f"Severity assessed {sev.get('score')}/5",
                    str(sev.get("reasoning") or sev.get("level") or ""),
                )
            committed = [
                r for r in item.get("resources_allocated", []) or []
                if isinstance(r, dict)
            ]
            if committed:
                step("DISPATCH", "Resources allocated", "; ".join(_resource_label(r) for r in committed))
            elif item.get("status") != "unhandled":
                step("BOTTLENECK", "No resources available", item.get("escalation_level") or "")

    AGENT_TRACES[incident.incident_id] = (
        _false_report_trace(incident) if false_report
        else _run_agent(incident, agent_severity)
    )

    record = existing or {
        "created_at": now(),
        "history": [],
        "timeline": [],
        "cascades": [],
    }
    record.setdefault("timeline", [])
    record.setdefault("cascades", [])

    record["incident"] = _incident_public(incident)
    record["result"] = serialized
    record["priority_score"] = 0 if false_report else priority_score(incident)
    record["updated_at"] = now()

    INCIDENTS[incident.incident_id] = record

    # ------------------------------------------------------------
    # Resource reallocation
    # ------------------------------------------------------------
    # If a critical medical incident could not get an ambulance,
    # search for an ambulance assigned to a lower-severity incident.
    if incident.incident_type == "medical" and not false_report:
        medical_item = next(
            (
                item
                for item in _result_items(serialized)
                if isinstance(item.get("severity"), dict)
            ),
            None,
        )

        if medical_item:
            severity_score = medical_item["severity"].get("score", 0)

            has_ambulance = any(
                isinstance(resource, dict)
                and resource.get("resource_type") == "ambulance"
                for resource in medical_item.get(
                    "resources_allocated",
                    [],
                )
            )

            if severity_score >= 4 and not has_ambulance:
                candidate = find_reallocation_candidate(
                    _incident_public(incident),
                    INCIDENTS,
                    int(severity_score),
                    record["priority_score"],
                )

                if candidate:
                    release_result = release_ambulance(
                        candidate,
                        WORLD,
                    )

                    if release_result.get("success"):
                        ambulance_id = release_result["ambulance_id"]

                        ambulance = next(
                            (
                                a
                                for a in WORLD.get("ambulances", [])
                                if a.get("id") == ambulance_id
                            ),
                            None,
                        )

                        if ambulance:
                            ambulance["status"] = "dispatched"

                        medical_item.setdefault(
                            "resources_allocated",
                            [],
                        ).append(
                            {
                                "resource_type": "ambulance",
                                "resource_id": ambulance_id,
                                "eta": "reallocated",
                            }
                        )

                        medical_item.setdefault(
                            "actions_taken",
                            [],
                        ).append(
                            {
                                "action": (
                                    f"Reallocated ambulance "
                                    f"{ambulance_id} from lower-priority "
                                    f"incident {candidate['incident_id']}."
                                )
                            }
                        )

                        medical_item.setdefault(
                            "reporter_updates",
                            [],
                        ).append(
                            (
                                f"Ambulance {ambulance_id} was reallocated "
                                "to this higher-priority emergency."
                            )
                        )

                        medical_item["status"] = "resolved"

                        log = medical_item.get("log")
                        if isinstance(log, dict):
                            log["resource_reallocated"] = ambulance_id
                            log["reallocated_from"] = (
                                candidate["incident_id"]
                            )

                        step(
                            "REALLOCATE",
                            f"Ambulance {ambulance_id} reallocated",
                            f"Taken from lower-priority {candidate['incident_id']} "
                            f"(severity {candidate['severity']})",
                        )

                        # The donor incident lost its ambulance and is open again.
                        donor = candidate["record"]
                        donor["status"] = _overall_status(donor.get("result"))
                        donor["incident"]["status"] = donor["status"]
                        donor["updated_at"] = now()
                        donor.setdefault("timeline", []).append({
                            "timestamp": now(),
                            "stage": "REALLOCATE",
                            "title": f"Ambulance {ambulance_id} reassigned away",
                            "detail": (
                                f"Moved to higher-priority {incident.incident_id}; "
                                f"monitoring will re-dispatch when a unit frees up"
                            ),
                        })
                        donor["history"].append({
                            "timestamp": now(),
                            "event": "resource_reallocated",
                            "note": f"Ambulance {ambulance_id} moved to {incident.incident_id}.",
                            "result": donor.get("result"),
                        })

    status = _overall_status(serialized)
    record["status"] = status
    record["incident"]["status"] = status
    step("STATUS", f"Status: {status.replace('_', ' ')}", "")

    # Generate downstream incidents. Each cascade id is created only once,
    # so follow-up updates and monitoring re-checks never duplicate them.
    new_cascades = []
    if allow_cascade and not false_report:
        for cascade in generate_cascades(incident, result, WORLD):
            if cascade.incident_id in INCIDENTS:
                continue
            _run_and_record_locked(
                cascade,
                event="cascade_generated",
                note=f"Generated from {incident.incident_id}",
                allow_cascade=False,
            )
            new_cascades.append(cascade)
            record["cascades"].append({
                "incident_id": cascade.incident_id,
                "incident_type": cascade.incident_type,
            })
        if new_cascades:
            step(
                "CASCADE",
                f"Spawned {len(new_cascades)} downstream incident(s)",
                ", ".join(c.incident_id for c in new_cascades),
            )

    # Periodic re-checks that change nothing are not worth a timeline entry.
    current_held = sorted(r["resource_id"] for r in _held_resources(record))
    unchanged = (
        event == "auto_recheck"
        and status == previous_status
        and current_held == previous_held
        and not new_cascades
    )
    if not unchanged:
        record["timeline"].extend(timeline)
        record["history"].append({
            "timestamp": now(),
            "event": event,
            "note": note,
            "result": serialized,
        })

    return record


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class IncidentIn(BaseModel):
    incident_type: str  # fire | medical | factory | road_blockage | auto
    description: str
    lat: float
    lng: float
    area_name: Optional[str] = None
    reporter: Optional[str] = "anonymous"
    confidence: float = 0.9
    severity_inputs: dict = Field(default_factory=dict)


class UpdateIn(BaseModel):
    description: str
    confidence: Optional[float] = None
    severity_inputs: Optional[dict] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/incidents")
def create_incident(payload: IncidentIn):
    incident_type = payload.incident_type
    classification_note = ""

    if incident_type == "auto":
        try:
            from shared.llm import llm_classify_type
            incident_type = llm_classify_type(payload.description)
            classification_note = f"LLM classified this report as '{incident_type}'."
        except Exception:
            text = payload.description.lower()
            if any(k in text for k in ["fire", "smoke", "burning", "flame"]):
                incident_type = "fire"
            elif any(k in text for k in ["hurt", "injur", "bleeding", "unconscious", "pain"]):
                incident_type = "medical"
            elif any(k in text for k in ["factory", "machine", "plant", "chemical"]):
                incident_type = "factory"
            elif any(k in text for k in ["road", "traffic", "accident", "blocked", "flood"]):
                incident_type = "road_blockage"
            else:
                incident_type = "fire"
            classification_note = f"Keyword fallback classified this report as '{incident_type}'."

    incident_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
    incident = Incident(
        incident_id=incident_id,
        incident_type=incident_type,
        description=payload.description,
        location={"lat": payload.lat, "lng": payload.lng, "area_name": payload.area_name},
        severity_inputs=payload.severity_inputs,
        reporter=payload.reporter,
        timestamp=now(),
        confidence=payload.confidence,
    )
    record = _run_and_record(incident, event="reported", note=classification_note)
    with STATE_LOCK:
        return copy.deepcopy(record)


@app.post("/api/incidents/{incident_id}/update")
def send_update(incident_id: str, payload: UpdateIn):
    """
    Simulate a bystander/monitoring follow-up (e.g. "condition worsening",
    "fire has spread") landing on an already-reported incident. This is
    the module's real Monitor & Adapt path being exercised live, not a
    scripted flag -- whatever severity_inputs/description change, the
    module re-assesses and may issue a different plan.
    """
    with STATE_LOCK:
        if incident_id not in INCIDENTS:
            return {"error": f"Unknown incident_id '{incident_id}'"}
        prior = copy.deepcopy(INCIDENTS[incident_id]["incident"])
    incident = Incident(
        incident_id=incident_id,
        incident_type=prior["incident_type"],
        description=payload.description,
        location=prior["location"],
        severity_inputs={**prior.get("severity_inputs", {}), **(payload.severity_inputs or {})},
        reporter=prior.get("reporter"),
        timestamp=now(),
        confidence=payload.confidence if payload.confidence is not None else prior.get("confidence", 0.9),
    )
    record = _run_and_record(
        incident,
        event="update_received",
        note=payload.description,
        allow_cascade=incident.reporter != "system_cascade",
    )
    with STATE_LOCK:
        return copy.deepcopy(record)


@app.get("/api/incidents")
def list_incidents():
    with STATE_LOCK:
        records = copy.deepcopy(list(INCIDENTS.values()))
    records.sort(key=lambda r: r.get("priority_score", 0), reverse=True)
    return records


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str):
    with STATE_LOCK:
        return copy.deepcopy(INCIDENTS.get(incident_id, {"error": "not found"}))


@app.get("/api/incidents/{incident_id}/agent-trace")
def get_agent_trace(incident_id: str):
    return {
        "incident_id": incident_id,
        "trace": copy.deepcopy(AGENT_TRACES.get(incident_id, [])),
    }


@app.get("/api/world")
def get_world():
    with STATE_LOCK:
        return copy.deepcopy(WORLD)


@app.get("/api/analytics")
def analytics():
    """
    Aggregates across all 4 modules. All 4 modules' `log` field is now a
    consistent dict shape (previously Fire/Traffic returned a list of
    strings while Medical/Factory returned a dict -- fixed in
    modules/fire/fire_module.py and modules/traffic/traffic_module.py so
    the contract in shared/contracts.py is actually accurate).
    """
    total = len(INCIDENTS)
    handled = 0
    unhandled = 0
    response_times = []
    resources_used_total = 0
    bottlenecks = []

    false_reports = 0

    with STATE_LOCK:
        records = copy.deepcopy(list(INCIDENTS.values()))

    for rec in records:
        if rec.get("status") == "false_report":
            false_reports += 1
        result = rec.get("result")
        items = result if isinstance(result, list) else [result]
        rec_handled = False
        for item in items:
            if isinstance(item, dict) and item.get("status") in {"unhandled", "false_report"}:
                continue
            rec_handled = True

            resources_used_total += len(item.get("resources_allocated", []) or []) if isinstance(item, dict) else 0

            log = item.get("log") if isinstance(item, dict) else None
            if isinstance(log, dict):
                rt = log.get("response_time_sec")
                if isinstance(rt, (int, float)):
                    response_times.append(rt)
                for b in log.get("bottlenecks", []) or []:
                    if b and b != "None":
                        bottlenecks.append({"incident_id": rec["incident"]["incident_id"], "bottleneck": b})
        if rec_handled:
            handled += 1
        else:
            unhandled += 1

    avg_response_time = round(sum(response_times) / len(response_times), 3) if response_times else 0

    return {
        "total_incidents": total,
        "handled": handled,
        "unhandled": unhandled,
        "false_reports": false_reports,
        "avg_response_time_sec": avg_response_time,
        "resources_used_total": resources_used_total,
        "bottlenecks": bottlenecks,
    }


@app.post("/api/reset")
def reset_world():
    """Reset the in-memory world + incident store back to the seed city_state.json. Useful between demo takes."""
    with STATE_LOCK:
        # Reset in place so every holder of the WORLD reference sees it.
        WORLD.clear()
        WORLD.update(load_world())
        INCIDENTS.clear()
        AGENT_TRACES.clear()
    return {"status": "reset"}


# ---------------------------------------------------------------------------
# Background monitoring loop
# ---------------------------------------------------------------------------
# Periodically re-checks incidents that are not yet "resolved" to reflect
# any world-state changes (e.g. a facility that was full when first assessed
# may have freed up, or a road blocked by another incident may now affect
# ETA). This is what makes "continuously monitored" a running behavior of
# the system rather than a one-shot pipeline.

def recheck_open_incidents():
    with STATE_LOCK:
        for incident_id, rec in list(INCIDENTS.items()):
            if rec.get("status") != "escalated":
                continue
            prior = rec["incident"]
            incident = Incident(
                incident_id=incident_id,
                incident_type=prior["incident_type"],
                description=prior["description"],
                location=prior["location"],
                severity_inputs=prior.get("severity_inputs", {}),
                reporter=prior.get("reporter"),
                timestamp=now(),
                confidence=prior.get("confidence", 0.9),
            )
            _run_and_record(
                incident,
                event="auto_recheck",
                note="Periodic monitoring re-check (resource/road availability may have changed).",
                allow_cascade=prior.get("reporter") != "system_cascade",
            )


async def monitoring_loop():
    while True:
        await asyncio.sleep(5)
        try:
            await asyncio.to_thread(recheck_open_incidents)
        except Exception as exc:  # keep monitoring alive for the demo
            print(f"monitoring re-check failed: {exc!r}")


@app.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(monitoring_loop())


# ---------------------------------------------------------------------------
# Static dashboard
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
