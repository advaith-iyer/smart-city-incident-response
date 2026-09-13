from typing import Any

from agent.tools import AgentTools
from agent.trace import AgentTrace


class DecisionEngine:
    def __init__(self, world_state):
        self.tools = AgentTools(world_state)
        self.trace = AgentTrace()

    def decide(self, incident: dict[str, Any]) -> dict[str, Any]:
        self.trace.clear()

        self.trace.add("OBSERVE", "Incident received", {"incident": incident})

        severity = incident.get("severity", 0)
        incident_type = incident.get("type", "unknown")

        self.trace.add(
            "ANALYZE",
            "Assessing incident priority",
            {"incident_type": incident_type, "severity": severity},
        )

        priority = (
            "CRITICAL" if severity >= 8
            else "HIGH" if severity >= 5
            else "MEDIUM" if severity >= 3
            else "LOW"
        )

        plan = {
            "priority": priority,
            "incident_type": incident_type,
            "actions": self._build_plan(incident_type, priority),
        }

        self.trace.add("PLAN", "Response plan generated", {
            "priority": priority,
            "incident_type": incident_type,
            "actions": list(plan["actions"]),
        })

        self._validate_and_adapt(plan)

        self.trace.add(
            "ACT",
            "Executing validated response plan",
            {"actions": list(plan["actions"])},
        )

        evaluation = self._evaluate(plan)

        self.trace.add(
            "EVALUATE",
            "Response plan evaluated",
            evaluation,
        )

        return {
            "decision": plan,
            "evaluation": evaluation,
            "trace": self.trace.get_events(),
        }

    def _build_plan(self, incident_type: str, priority: str) -> list[str]:
        actions = []

        if priority in {"CRITICAL", "HIGH"}:
            actions.append("dispatch_nearest_available_unit")
            actions.append("monitor_incident")

        if incident_type == "fire":
            actions.append("check_fire_spread")
        elif incident_type == "medical":
            actions.append("check_medical_capacity")
        elif incident_type == "traffic":
            actions.append("check_road_conditions")
        elif incident_type == "factory":
            actions.append("check_hazard_and_evacuation")

        actions.append("evaluate_response")
        return actions

    def _validate_and_adapt(self, plan: dict[str, Any]) -> None:
        action = "dispatch_nearest_available_unit"

        if action not in plan["actions"]:
            return

        validation = self.tools.validate_action(
            action,
            plan["incident_type"],
        )

        if validation["allowed"]:
            self.trace.add(
                "GUARDRAIL",
                "Dispatch action approved",
                {"reason": validation["reason"]},
            )
            return

        plan["actions"].remove(action)

        replacement = validation.get("replacement")

        if replacement and replacement not in plan["actions"]:
            plan["actions"].insert(0, replacement)

        self.trace.add(
            "GUARDRAIL",
            "Dispatch action rejected",
            {"reason": validation["reason"]},
        )

        self.trace.add(
            "ADAPT",
            "Plan updated after policy rejection",
            {
                "replacement": replacement,
                "new_actions": list(plan["actions"]),
            },
        )

    def _evaluate(self, plan: dict[str, Any]) -> dict[str, Any]:
        actions = plan["actions"]

        if not actions:
            return {
                "status": "FAILED",
                "success": False,
                "reason": "no executable actions",
            }

        if "request_mutual_aid" in actions:
            return {
                "status": "ADAPTED",
                "success": True,
                "reason": "primary resource unavailable; fallback plan selected",
            }

        return {
            "status": "READY",
            "success": True,
            "reason": "validated response plan available",
        }
