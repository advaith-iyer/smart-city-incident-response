# Fire & Hazard Module — Person A

Handles `fire` and `chemical/gas leak` incidents.

## Main entry point

```python
handle_incident(incident: Incident, world: dict) -> ModuleOutput
```

The module follows Observe -> Assess -> Decide -> Act -> Monitor & Adapt -> Log.

## Severity inputs

Pass these in `incident.severity_inputs`:

- `spread_risk`: low / medium / high / extreme
- `crowd_proximity`: low / medium / high
- `structure_proximity`: low / medium / high
- `wind`: calm / low / moderate / high / strong
- `corroborating_reports`: integer
- `confidence`: float

Demo-only adaptation flags:

- `_simulate_spread`
- `_simulate_resource_unavailable`
- `_simulate_severity_increase`

## Unique feature

`flag_dangerous_area()` stores repeated/severe incident locations in
`world["danger_zones"]` and `world["fire_history"]`. Future incidents can
use this state for prioritization.

## Shared-world integration

The current `shared/city_state.json` has an empty `fire_units` list. During
integration, add the `fire_units` records from `fire_world.json` into the
shared city state (or use the team's finalized fire-resource records).

No changes to `shared/contracts.py` or the Orchestrator are made by this
module.
