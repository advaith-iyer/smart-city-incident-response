# Factory / Industrial Incident Module — Description

Person C's module for the **Smart City Incident Response Agent** (Tech Zephyr 4.0).
Mirrors the structure, conventions, and contract compliance of the Medical /
Ambulance Module (Person B) so the Orchestrator can call every module
interchangeably.

Files:
- `factory_module.py` — the module itself. Single entry point: `handle_incident(incident, world=None) -> dict`.
- `factory_world.json` — standalone mock world data for factories (dev copy; see "Merging into the shared world file" below).

---

## 1. Assumptions about factory-related world data

These are isolated at the top of `factory_module.py` and in `factory_world.json`
so they're easy to adjust once the team locks the real shared schema:

- **`factories`** is a top-level list, each with:
  - `id`, `name`, `location {lat, lng, area_name}`, `hazmat_on_site` (bool), `contact`
  - `sections`: list of `{section_id, name, occupied, worker_roster}` — `worker_roster` is a list of worker-ID strings, used for the headcount/accountability check.
  - `machinery`: list of `{id, name, section_id, status}` (`status` is `"running"` or `"stopped"`), used to check whether the equipment behind a machine-failure incident is still active.
  - `safety_team`: list of `{id, role, name, status}` — `role` is one of `safety_officer`, `technician`, `electrician`, `first_aid`; `status` is `"available"` or `"dispatched"`.
  - `equipment`: list of `{id, type, quantity, available}` — e.g. `fire_extinguisher`, `ppe_hazmat_suit`, `gas_detector`. (Loaded/looked up via `find_available_equipment()`, though the current demo incidents route mainly through personnel; this is a ready hook for teammates who want equipment-based plans.)
- A single **`district_industrial_authority`** object (`id`, `name`, `contact`) represents the final escalation tier.
- **Incident objects may carry a few optional extra fields** beyond the core shared Incident contract — this is additive, not a change to the contract:
  - `factory_id` — which factory the incident belongs to (falls back to nearest-by-location if absent).
  - `section_id` — which section within the factory (falls back to the first `occupied` section, then the first section).
  - `containment_status` — `"contained"` / `"uncontained"`, feeds severity.
  - `people_affected` — int, feeds severity.
  - `_simulate_worker_missing`, `_simulate_resource_unavailable`, `_simulate_spread`, `_simulate_external_needed`, `_simulate_severity_increase` — demo-only flags for `monitor_and_adapt()`, following the same pattern as the Medical Module's `_simulate_escalation`.
- The module **mutates the in-memory world dict** it's given (marks staff `"dispatched"`, decrements equipment, appends to `system_state.alerts`) — same approach as the Medical Module's hospital `current_load` updates. It does not write back to disk; that's the Orchestrator/shared-store's job.

### Merging into the shared world file
`factory_world.json` is intentionally shaped as a drop-in fragment: once the
team's real `city_state.json` is frozen, just copy the `factories` and
`district_industrial_authority` keys into it and point `WORLD_PATH` there.
`load_world()` only ever reads `world["factories"]` and
`world["district_industrial_authority"]`, so nothing else in the module
needs to change.

---

## 2. How it maps to the shared contract

`handle_incident(incident: dict) -> dict` always returns exactly:

```
incident_id, severity{score, reasoning}, actions_taken[{action, timestamp}],
resources_allocated[{resource_type, resource_id, eta}], escalation_level,
status, reporter_updates[str], log{...}
```

The `log` object (deliberately unspecified in detail by the contract) carries
the Factory Module's extra reporting detail: `category`, `worker_accountability`,
`timeline`, `summary`, `bottlenecks`, and — only for redirect cases —
`redirect_target`. None of this changes the required top-level shape.

---

## 3. The 6-step loop, as implemented

| Step | Function(s) | What happens |
|---|---|---|
| 1. Observe | — | Incident received as-is; a "Incident reported" timeline entry is stamped. |
| 1b. Classify | `classify_incident()` | Rule-based keyword match into a factory category (`gas_leak`, `chemical_spill`, `electrical_failure`, `machine_failure`, `workplace_hazard`) **or** a redirect flag (`fire_primary`, `medical_primary`, `fire_and_medical`) when no factory-specific keyword is present. |
| 2. Assess | `assess_severity()` → `assess_severity_rule_based()` (default) or `assess_severity_llm()` (stub) | Score 1–5 from category base severity + hazmat-on-site + containment status + running machinery + people affected + missing workers + confidence caveat. |
| 3. Decide | `decide_action()` | Escalation ladder by severity: `safety_team_containment` (local) → `onsite_specialist` (professional) → `specialist_and_mutual_aid` (professional) → `district_authority_and_evacuate` (multi-agency). |
| 4. Act | `act()` | Immediate containment action, on-site specialist notification (with mutual-aid and district-authority fallbacks), evacuation + worker headcount for higher-severity plans, hazard zone creation. |
| 5. Monitor & Adapt | `monitor_and_adapt()` | Reacts to `_simulate_*` flags: newly-missing worker, a resource going unavailable (triggers a fresh mutual-aid search), incident spreading (widens the hazard zone), external assistance becoming necessary, or a general severity spike (re-decides and re-acts, same pattern as Medical's escalation demo). |
| 6. Log | `build_log()` | Response time, final severity, category, resources used, worker accountability, full timeline, bottlenecks, and a one-line dashboard summary. |

### Redirect path (fire / medical primary incidents)
If `classify_incident()` finds no factory-specific keyword but does find a
fire or medical one, `handle_incident()` returns early with a minimal but
fully contract-shaped `ModuleOutput`: `status = "escalated"`,
`escalation_level = "multi-agency"`, one action noting the redirect, one
reporter update, an optional on-site safety-officer "standby" allocation,
and `log["redirect_target"]` set to `"fire"` or `"medical"` so the
Orchestrator knows where to route it next.

### Escalation ladder (tiers actually implemented in `act()`)
1. **On-site safety team** — `find_available_personnel(factory, role)`.
2. **Nearby specialist / mutual aid** — `request_mutual_aid()` scans other
   factories in the world data for an available person with the needed role,
   nearest first, and "dispatches" them (marks `status`, computes ETA via
   `haversine_km` + `eta_minutes`, same helpers as the Medical Module).
3. **District industrial safety authority** — used when no on-site or
   mutual-aid resource exists, or automatically for severity-5 plans.

### Hazard zone
`create_hazard_zone()` produces a simple `{zone_id, center, radius_km,
category}` for `gas_leak` / `chemical_spill` incidents, or any incident at
severity ≥ 4. `monitor_and_adapt()` can widen it via `_simulate_spread`. It's
also pushed into `world["system_state"]["alerts"]` so it's visible to the
shared live-incident view, and included in the final `log["hazard_zone"]`.

### Worker accountability
`check_worker_accountability()` reads the relevant section's `worker_roster`
and treats everyone as accounted for unless `_simulate_worker_missing` is
set (an int → simulates that many of the roster going missing, or a list →
specific worker IDs). A non-empty `missing_workers` list bumps severity by
1 point and is called out in `actions_taken`, `reporter_updates`, and the
final `log["bottlenecks"]`.

---

## 4. Swap points for teammates / later work

- **`assess_severity_llm(incident)`** — stub that raises `NotImplementedError`,
  gated behind `os.environ["USE_LLM_SEVERITY"] == "1"` in `assess_severity()`,
  exactly like the Medical Module. Wire in a real Claude/Anthropic call here
  for smarter severity + reasoning; parse its output into `(score, reasoning)`.
- **`WORLD_PATH`** — currently `factory_world.json`; repoint to the shared
  `city_state.json` once `factories` is merged in (see Section 1).
- **`find_available_equipment()`** — implemented but not yet wired into any
  plan; a natural hook for PPE/equipment-driven plans (e.g. requiring a
  `gas_detector` before entering a `gas_leak` zone).
- **Notification mechanism** — currently simulated as log lines / dict
  entries in `actions_taken`, same as Medical. Swap for the team's real
  notification service (console feed, webhook, Slack) in `act()` and
  `monitor_and_adapt()`.

---

## 5. Running it standalone

```
python factory_module.py
```

Runs three demo incidents end-to-end and prints each `ModuleOutput` as JSON:

1. **Chemical spill** at Riverside Chemical Plant (`F2`) — hazmat on-site,
   uncontained, one worker missing → severity 5, multi-agency escalation,
   hazard zone, district authority notified.
2. **Machine failure** at Old Town Textile Mill (`F3`) — uncontained →
   severity 3, no on-site technician, demonstrates the **mutual aid** tier
   (technician pulled from Central Steel Works, `F1`, with a computed ETA).
3. **Medical emergency** at Central Steel Works (`F1`) — no factory-specific
   keyword present → demonstrates the **redirect path** to the Medical
   Module.

Each can also be adapted to exercise `monitor_and_adapt()` by adding any of
the `_simulate_*` flags described in Section 1.
