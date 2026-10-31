"""
End-to-end smoke test for the dashboard backend.

Run from the project root:  python tests_smoke.py
Exercises all four modules, cascades, updates, reallocation, low-confidence
reports, Dijkstra rerouting and Reset Demo, and checks resource bookkeeping.
"""

import json
import sys

from fastapi.testclient import TestClient

import server.app as srv

client = TestClient(srv.app)
FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def items(rec):
    r = rec.get("result")
    return r if isinstance(r, list) else [r]


def held_by(rec, rtype):
    return [
        res["resource_id"]
        for it in items(rec) if isinstance(it, dict)
        for res in it.get("resources_allocated", []) or []
        if res.get("resource_type") == rtype and res.get("status") != "reallocated"
    ]


def busy(kind):
    return sorted(u["id"] for u in srv.WORLD[kind] if u.get("status") != "available")


def report(**kw):
    body = {"lat": 21.256, "lng": 81.636, "confidence": 0.9}
    body.update(kw)
    r = client.post("/api/incidents", json=body)
    assert r.status_code == 200, r.text
    return r.json()


client.post("/api/reset")
seed = json.dumps(srv.load_world(), sort_keys=True)

# ---- all four modules -------------------------------------------------------
med = report(incident_type="medical", description="Man collapsed, unconscious, not breathing")
check(med["result"]["status"] != "unhandled", "medical module handles incident")
fire = report(incident_type="fire", description="Building fire, heavy smoke, people trapped",
              severity_inputs={"spread_risk": "high", "crowd_nearby": True})
check(isinstance(fire["result"], (dict, list)), "fire module handles incident")
fac = report(incident_type="factory", description="Gas leak in press floor, workers coughing, injured",
             lat=21.253, lng=81.633)
check(fac["result"] is not None, "factory module handles incident")
trf = report(incident_type="road_blockage", description="Truck overturned blocking road",
             severity_inputs={"road_id": "R1", "start_junction": "J1",
                              "destination_junction": "J2", "traffic_volume": "high"})
t = items(trf)[0]
check(t["status"] == "rerouted", f"traffic reroutes around R1 (status={t['status']})")
check(any("J1 -> J4" in a["action"] or "J1 ->" in a["action"] for a in t["actions_taken"]),
      "Dijkstra alternate path reported")

# ---- Dijkstra correctness --------------------------------------------------
from shared.road_graph import shortest_path
roads = srv.load_world()["roads"]
base = shortest_path(roads, "J1", "J3")
for e in roads["edges"]:
    if e["id"] == "R1":
        e["blocked"] = True
alt = shortest_path(roads, "J1", "J3")
check(base["path"] == ["J1", "J2", "J3"], f"baseline J1->J3 path {base['path']}")
check(alt and "J2" not in alt["path"][:2] and alt["distance_km"] > base["distance_km"],
      f"blocked R1 reroutes J1->J3 via {alt and alt['path']}")
check(shortest_path(roads, "J1", "J1")["path"] == ["J1"], "same-node route")

# ---- no duplicate allocation on updates/rechecks ---------------------------
amb_before = busy("ambulances")
fire_before = busy("fire_units")
for _ in range(3):
    client.post(f"/api/incidents/{med['incident']['incident_id']}/update",
                json={"description": "Still unconscious, condition worsening"})
    client.post(f"/api/incidents/{fire['incident']['incident_id']}/update",
                json={"description": "Fire has spread, more smoke visible"})
check(busy("ambulances") == amb_before, f"ambulances not double-booked ({amb_before} -> {busy('ambulances')})")
check(busy("fire_units") == fire_before, f"fire units not double-booked ({fire_before} -> {busy('fire_units')})")

# every busy ambulance belongs to exactly one live incident
owners = {}
for rec in srv.INCIDENTS.values():
    for a in held_by(rec, "ambulance"):
        owners.setdefault(a, []).append(rec["incident"]["incident_id"])
check(all(len(v) == 1 for v in owners.values()), f"each ambulance has one owner {owners}")

# ---- no duplicate cascades -------------------------------------------------
n = len(srv.INCIDENTS)
client.post(f"/api/incidents/{fire['incident']['incident_id']}/update",
            json={"description": "Fire still burning"})
client.post(f"/api/incidents/{fac['incident']['incident_id']}/update",
            json={"description": "Gas leak continuing, workers injured"})
check(len(srv.INCIDENTS) == n, f"updates don't spawn duplicate cascades ({n} -> {len(srv.INCIDENTS)})")
cascade_ids = [c["incident_id"] for rec in srv.INCIDENTS.values() for c in rec.get("cascades", [])]
check(len(cascade_ids) == len(set(cascade_ids)), "cascade ids unique")

# ---- status consistency ----------------------------------------------------
for rec in srv.INCIDENTS.values():
    check(rec.get("status") in {"active", "resolved", "rerouted", "escalated", "unhandled",
                                "false_report", "reallocated", "redirected", "monitoring"},
          f"{rec['incident']['incident_id']} has top-level status {rec.get('status')}")

# ---- low confidence --------------------------------------------------------
amb = busy("ambulances")
low = report(incident_type="medical", description="maybe someone fell?", confidence=0.1)
check(low.get("status") == "false_report", f"low-confidence flagged (status={low.get('status')})")
check(busy("ambulances") == amb, "low-confidence report dispatches nothing")
check(srv.AGENT_TRACES.get(low["incident"]["incident_id"]), "low-confidence has agent trace")
up = client.post(f"/api/incidents/{low['incident']['incident_id']}/update",
                 json={"description": "Confirmed: person unconscious, not breathing", "confidence": 0.95}).json()
check(up.get("status") != "false_report", f"confirmed follow-up dispatches (status={up.get('status')})")

# ---- timeline --------------------------------------------------------------
check(len(med.get("timeline", [])) > 0 or len(srv.INCIDENTS[med["incident"]["incident_id"]]["timeline"]) > 3,
      "decision timeline recorded")

# ---- monitoring re-check doesn't leak ---------------------------------------
import asyncio
amb = busy("ambulances")
for _ in range(3):
    srv.recheck_open_incidents()
check(busy("ambulances") == amb, "auto re-check does not leak ambulances")

# ---- resolve releases ------------------------------------------------------
client.post(f"/api/incidents/{trf['incident']['incident_id']}/update",
            json={"description": "Road cleared, traffic flowing"})
r1 = next(e for e in srv.WORLD["roads"]["edges"] if e["id"] == "R1")
check(not r1["blocked"], "resolved road blockage reopens road")

# ---- reset -----------------------------------------------------------------
client.post("/api/reset")
check(not srv.INCIDENTS and not srv.AGENT_TRACES, "reset clears incidents/traces")
check(json.dumps(srv.WORLD, sort_keys=True) == seed, "reset restores seed world exactly")
check(client.get("/api/world").json() == srv.WORLD, "/api/world serves reset world")
again = report(incident_type="medical", description="Man collapsed, unconscious")
check(held_by(again, "ambulance"), "dispatch works after reset")
client.post("/api/reset")

print("\n%d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
