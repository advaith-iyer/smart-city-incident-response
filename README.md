# Smart City Incident Response Agent

Clean project structure for the four incident modules and shared orchestration layer,
plus a live web dashboard on top.

## Structure
- `modules/fire` — Fire & Hazard (Person A)
- `modules/medical` — Medical / Ambulance (Person B)
- `modules/factory` — Factory / Industrial (Person C)
- `modules/traffic` — Traffic / Road (Person D)
- `orchestrator` — central routing/priority layer
- `shared` — common contracts, world state, road graph, optional LLM helper
- `server` — FastAPI backend + live dashboard (map, priority queue, report form, analytics)
- `data` — demo/seed fixtures
- `demo.py` — original CLI demo (still works, run with `python demo.py`)

Run commands from this project root so imports such as `from shared...` resolve correctly.

## Running the live dashboard

```bash
pip install -r requirements.txt
uvicorn server.app:app --reload
```

Then open **http://127.0.0.1:8000** in a browser. You'll see:

- **Live Priority Queue** (left) — every reported incident, auto-sorted by priority,
  color-coded by severity. Click any card (or any pulsing dot on the map) for full detail.
- **City Map** (center) — hospitals, dispensaries, fire units, factories, and traffic
  points plotted from `shared/city_state.json`, with roads drawn from the real road
  graph (red dashed = blocked). Incident markers pulse and are colored by severity.
- **Report an Incident** (right) — submit a new incident through the same form a
  bystander would use. Pick "Not sure / let the agent classify" to see the Orchestrator's
  LLM-based (or keyword-fallback) classification step choose the module for you.
- **Analytics** (right) — live rollup: total/handled/unhandled, resources used, and any
  bottlenecks logged by the modules.

Click into any incident and use **"Send Update"** to simulate a follow-up report
(e.g. "fire has spread, more smoke visible") — this re-runs that incident through its
module's real Monitor & Adapt path and updates its severity/plan live. This, plus a
low-confidence report (set the confidence slider under ~0.3) triggering false-report
detection, are your two best "induced failure / adaptation" beats for the demo video.

The system also runs a background monitoring loop (every 5s) that automatically
re-checks any incident still in an `escalated` state, in case resource/road availability
has changed since it was first assessed — this is what makes "continuously monitored"
an actual running behavior instead of a one-shot pipeline.

Use **Reset Demo** (top right) to restore the world to its seed state between takes.

## Enabling real LLM reasoning

By default, severity assessment (Medical, Factory) and incident classification
(Orchestrator, when "auto" is selected) run on fast, dependency-free rule-based logic —
zero setup required. To switch on real LLM-based reasoning instead (via Groq's free API):

```bash
export USE_LLM_SEVERITY=1
export GROQ_API_KEY=your_key_here
uvicorn server.app:app --reload
```

Get a free key at https://console.groq.com/keys (no card required). By default this uses
the `llama-3.1-8b-instant` model; override with `GROQ_MODEL` if you want a bigger model
(e.g. `llama-3.3-70b-versatile`).

If the LLM call fails for any reason (no key, no network, bad response), every call site
catches the error and falls back to rule-based logic automatically — the system never
crashes because of the LLM being unavailable.

**Design note:** only Medical and Factory severity are LLM-eligible, because they're
driven primarily by free-text descriptions. Fire severity is computed from structured
inputs (spread risk, wind, crowd proximity) and Traffic severity from live road/traffic
data — both are already deterministic multi-factor scores, so an LLM call there would be
reasoning-for-show rather than reasoning that changes the outcome. Be ready to explain
this distinction to judges — it's a better answer than "we used an LLM everywhere."

## Fixed since initial integration

`ModuleOutput.log` was a **dict** in Medical/Factory but a **list of strings** in
Fire/Traffic — a real contract drift from independent development. All 4 modules
now return the same log shape:
`{incident_id, response_time_sec, final_severity, escalation_level, resources_used, bottlenecks}`.
Traffic's `actions_taken`/`resources_allocated` were also standardized from plain
strings to the `{action, timestamp}` / `{resource_type, resource_id, eta}` dict shape
used everywhere else. `server/app.py`'s analytics endpoint no longer needs to guess
between formats.

**ETA calculations are now road-aware.** Previously only the Traffic module used
`shared/road_graph.py`'s Dijkstra shortest-path; Medical, Fire, and Factory all
computed ETA as straight-line distance, so a blocked road never actually affected
an ambulance/fire-tender/mutual-aid ETA. `shared/road_graph.py` gained
`nearest_node()` and `route_distance_km()` (snaps arbitrary lat/lng points to their
nearest junction, routes between junctions via the existing shortest-path function,
adds the "last mile" straight-line hops), and all 3 modules now call it through a
`road_aware_distance_km()` wrapper that falls back to straight-line distance if the
graph is unavailable or disconnected. Verified directly: blocking the roads around
a location raises the computed ambulance/fire-tender ETA to that location instead
of leaving it unchanged. `resolve_factory()`'s nearest-factory lookup intentionally
still uses straight-line distance, since that's identifying which building an
incident belongs to, not a travel-time estimate.
