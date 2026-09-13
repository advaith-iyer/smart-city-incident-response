import json

from shared.contracts import Incident
from orchestrator.orchestrator import run_response, serialize_result


def main():
    with open("shared/city_state.json") as f:
        world = json.load(f)

    incidents = [
        # 1. Fire
        Incident(
            incident_id="DEMO-FIRE",
            incident_type="fire",
            description="Building fire with visible smoke",
            location={
                "lat": 21.25,
                "lng": 81.63,
            },
            confidence=0.95,
            reporter="demo_user",
        ),

        # 2. Medical
        Incident(
            incident_id="DEMO-MED",
            incident_type="medical",
            description="Person injured and unconscious",
            location={
                "lat": 21.25,
                "lng": 81.63,
            },
            confidence=0.95,
            reporter="demo_user",
        ),

        # 3. Traffic rerouting
        Incident(
            incident_id="DEMO-TRAFFIC",
            incident_type="road_blockage",
            description="Accident blocking R7",
            location={
                "lat": 21.255,
                "lng": 81.635,
            },
            severity_inputs={
                "road_id": "R7",
                "start_junction": "J2",
                "destination_junction": "J7",
                "traffic_volume": "high",
                "emergency_access_blocked": True,
            },
            confidence=0.95,
            reporter="demo_user",
        ),

        # 4. Factory + Fire multi-module incident
        Incident(
            incident_id="DEMO-MULTI",
            incident_type="factory",
            description="Chemical fire at Riverside Chemical Plant",
            location={
                "lat": 21.269,
                "lng": 81.652,
            },
            severity_inputs={
                "factory_id": "F2",
                "containment_status": "uncontained",
                "requires_fire": True,
            },
            confidence=0.95,
            reporter="demo_user",
        ),

        # 5. Low-confidence report
        Incident(
            incident_id="DEMO-FALSE",
            incident_type="fire",
            description="Possible fire reported",
            location={
                "lat": 21.25,
                "lng": 81.63,
            },
            confidence=0.10,
            reporter="demo_user",
        ),
    ]

    result = run_response(incidents, world)

    print("\n========== SMART CITY INCIDENT RESPONSE ==========")
    print(f"Total incidents : {result['total_incidents']}")
    print(f"Handled         : {result['handled']}")
    print(f"Unhandled       : {result['unhandled']}")

    print("\n========== INCIDENT RESULTS ==========")

    for response in result["results"]:
        if isinstance(response, list):
            print("\n[MULTI-MODULE INCIDENT]")

            for module_result in response:
                print(
                    f"{module_result.incident_id} → "
                    f"{module_result.status} "
                    f"({module_result.escalation_level})"
                )

                for update in module_result.reporter_updates:
                    print(f"  • {update}")

        elif hasattr(response, "incident_id"):
            print(
                f"\n{response.incident_id} → "
                f"{response.status} "
                f"({response.escalation_level})"
            )

            for update in response.reporter_updates:
                print(f"  • {update}")

        else:
            print(
                f"\n{response['incident_id']} → "
                f"{response['status']}"
            )

    print("\n========================================")


if __name__ == "__main__":
    main()