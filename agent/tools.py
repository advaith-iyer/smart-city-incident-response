from typing import Any


class AgentTools:
    def __init__(self, world_state):
        self.world_state = world_state

    def get_world_state(self) -> dict[str, Any]:
        return self.world_state.get_state()

    def validate_action(self, action: str, incident_type: str) -> dict[str, Any]:
        state = self.get_world_state()
        resources = state.get("resources", {})

        if action == "dispatch_nearest_available_unit":
            resource_key = {
                "fire": "fire_units",
                "medical": "ambulances",
            }.get(incident_type)

            if resource_key and resources.get(resource_key, 0) <= 0:
                return {
                    "allowed": False,
                    "reason": f"no available {resource_key}",
                    "replacement": "request_mutual_aid",
                }

        return {
            "allowed": True,
            "reason": "policy checks passed",
            "replacement": None,
        }

    def log_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "logged",
            "decision": decision,
        }
