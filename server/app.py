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
    dispatch_to_module,
    priority_score,
    is_false_report,
    serialize_result,
)
from orchestrator.cascade import generate_cascades
from orchestrator.reallocation import (
    find_reallocation_candidate,
    release_ambulance,
)
from agent.decision_engine import DecisionEngine

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


def _run_and_record(
    incident: Incident,
    event: str,
    note: str = "",
    allow_cascade: bool = True,
):
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
    items = serialized if isinstance(serialized, list) else [serialized]
    for item in items:
        if isinstance(item, dict):
            severity_data = item.get("severity", {})
            if isinstance(severity_data, dict):
                score = severity_data.get("score", 0)
                if isinstance(score, (int, float)):
                    agent_severity = max(agent_severity, score)

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
        "severity": agent_severity,
        "location": incident.location,
    })

    AGENT_TRACES[incident.incident_id] = agent_result["trace"]

    record = INCIDENTS.setdefault(
        incident.incident_id,
        {
            "created_at": now(),
            "history": [],
        },
    )

    record["incident"] = _incident_public(incident)
    record["result"] = serialized
    record["priority_score"] = priority_score(incident)
    record["updated_at"] = now()
    record["history"].append({
        "timestamp": now(),
        "event": event,
        "note": note,
        "result": serialized,
    })

    INCIDENTS[incident.incident_id] = record

        # ------------------------------------------------------------
    # Resource reallocation
    # ------------------------------------------------------------
    # If a critical medical incident could not get an ambulance,
    # search for an ambulance assigned to a lower-severity incident.
    if incident.incident_type == "medical":
        items = serialized if isinstance(serialized, list) else [serialized]

        medical_item = next(
            (
                item
                for item in items
                if isinstance(item, dict)
                and isinstance(item.get("severity"), dict)
            ),
            None,
        )

        if medical_item:
            severity_score = medical_item["severity"].get("score", 0)

            has_ambulance = any(
                resource.get("resource_type") == "ambulance"
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
                    priority_score(incident),
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

                        record["result"] = serialized
                        record["updated_at"] = now()

                        record["history"].append(
                            {
                                "timestamp": now(),
                                "event": "resource_reallocated",
                                "note": (
                                    f"Ambulance {ambulance_id} moved from "
                                    f"{candidate['incident_id']} to "
                                    f"{incident.incident_id}."
                                ),
                                "result": serialized,
                            }
                        )

                        INCIDENTS[incident.incident_id] = record

    # Generate and record downstream incidents only for the
    # original incident. This prevents cascade -> cascade loops.
    if allow_cascade:
        cascades = generate_cascades(
            incident,
            result,
            WORLD,
        )

        cascade_records = []

        for cascade in cascades:
            cascade_record = _run_and_record(
                cascade,
                event="cascade_generated",
                note=f"Generated from {incident.incident_id}",
                allow_cascade=False,
            )
            cascade_records.append(cascade_record)

        record["cascades"] = [
            {
                "incident_id": c["incident"]["incident_id"],
                "incident_type": c["incident"]["incident_type"],
            }
            for c in cascade_records
        ]

        INCIDENTS[incident.incident_id] = record

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
    return record


@app.post("/api/incidents/{incident_id}/update")
def send_update(incident_id: str, payload: UpdateIn):
    """
    Simulate a bystander/monitoring follow-up (e.g. "condition worsening",
    "fire has spread") landing on an already-reported incident. This is
    the module's real Monitor & Adapt path being exercised live, not a
    scripted flag -- whatever severity_inputs/description change, the
    module re-assesses and may issue a different plan.
    """
    if incident_id not in INCIDENTS:
        return {"error": f"Unknown incident_id '{incident_id}'"}

    prior = INCIDENTS[incident_id]["incident"]
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
    record = _run_and_record(incident, event="update_received", note=payload.description)
    return record


@app.get("/api/incidents")
def list_incidents():
    records = list(INCIDENTS.values())
    records.sort(key=lambda r: r.get("priority_score", 0), reverse=True)
    return records


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str):
    return INCIDENTS.get(incident_id, {"error": "not found"})


@app.get("/api/incidents/{incident_id}/agent-trace")
def get_agent_trace(incident_id: str):
    return {
        "incident_id": incident_id,
        "trace": AGENT_TRACES.get(incident_id, []),
    }


@app.get("/api/world")
def get_world():
    return WORLD


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

    for rec in INCIDENTS.values():
        result = rec.get("result")
        items = result if isinstance(result, list) else [result]
        rec_handled = False
        for item in items:
            if isinstance(item, dict) and item.get("status") == "unhandled":
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
        "avg_response_time_sec": avg_response_time,
        "resources_used_total": resources_used_total,
        "bottlenecks": bottlenecks,
    }


@app.post("/api/reset")
def reset_world():
    """Reset the in-memory world + incident store back to the seed city_state.json. Useful between demo takes."""
    global WORLD
    WORLD = load_world()
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

async def monitoring_loop():
    while True:
        await asyncio.sleep(5)
        for incident_id, rec in list(INCIDENTS.items()):
            result = rec.get("result")
            items = result if isinstance(result, list) else [result]
            still_open = any(
                isinstance(item, dict) and item.get("status") == "escalated"
                for item in items
            )
            if not still_open:
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
            _run_and_record(incident, event="auto_recheck",
                             note="Periodic monitoring re-check (resource/road availability may have changed).")


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
