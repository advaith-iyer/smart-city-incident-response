from typing import Optional


def _result_items(record: dict) -> list[dict]:
    """Return all result objects from an incident record."""
    result = record.get("result")

    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]

    if isinstance(result, dict):
        return [result]

    return []


def _severity_score(record: dict) -> int:
    """Return the highest severity recorded for an incident."""
    scores = []

    for item in _result_items(record):
        severity = item.get("severity")

        if isinstance(severity, dict):
            score = severity.get("score")

            if isinstance(score, (int, float)):
                scores.append(int(score))

    return max(scores, default=0)


def _allocated_ambulance_ids(record: dict) -> list[str]:
    """Return ambulance IDs currently assigned to an incident."""
    ambulance_ids = []

    for item in _result_items(record):
        for resource in item.get("resources_allocated", []) or []:
            if resource.get("resource_type") == "ambulance":
                resource_id = resource.get("resource_id")

                if resource_id:
                    ambulance_ids.append(resource_id)

    return ambulance_ids


def find_reallocation_candidate(
    incident: dict,
    incident_store: dict,
    new_severity: int,
    new_priority_score: int,
) -> Optional[dict]:
    """
    Find an ambulance assigned to a lower-priority incident.

    Reallocation rules:
      1. Higher severity always wins.
      2. If severity is equal, higher priority wins.
      3. If severity and priority are both equal, no reallocation occurs.
    """

    candidates = []

    for incident_id, record in incident_store.items():

        # Never reallocate from the incident currently being processed.
        if incident_id == incident.get("incident_id"):
            continue

        ambulance_ids = _allocated_ambulance_ids(record)

        if not ambulance_ids:
            continue

        old_severity = _severity_score(record)
        old_priority = int(record.get("priority_score", 0))

        # Existing incident is more severe.
        if old_severity > new_severity:
            continue

        # Same severity, but existing incident has equal/higher priority.
        if (
            old_severity == new_severity
            and old_priority >= new_priority_score
        ):
            continue

        candidates.append(
            {
                "incident_id": incident_id,
                "ambulance_id": ambulance_ids[0],
                "severity": old_severity,
                "priority_score": old_priority,
                "record": record,
            }
        )

    if not candidates:
        return None

    # Prefer taking a resource from the least urgent incident.
    candidates.sort(
        key=lambda item: (
            item["severity"],
            item["priority_score"],
        )
    )

    return candidates[0]


def release_ambulance(
    candidate: dict,
    world: dict,
) -> dict:
    """
    Release an ambulance from a lower-priority incident.

    The ambulance becomes available for reassignment.
    The previous incident is marked as reallocated.
    """

    ambulance_id = candidate["ambulance_id"]

    ambulance = next(
        (
            item
            for item in world.get("ambulances", [])
            if item.get("id") == ambulance_id
        ),
        None,
    )

    if not ambulance:
        return {
            "success": False,
            "message": f"Ambulance {ambulance_id} not found.",
        }

    # Make the ambulance available again.
    ambulance["status"] = "available"

    record = candidate["record"]

    for item in _result_items(record):

        resources = item.get("resources_allocated", []) or []

        for resource in resources:
            if (
                resource.get("resource_type") == "ambulance"
                and resource.get("resource_id") == ambulance_id
            ):
                resource["status"] = "reallocated"

        actions = item.setdefault("actions_taken", [])

        actions.append(
            {
                "action": (
                    f"Ambulance {ambulance_id} was reassigned "
                    f"to a higher-priority emergency."
                )
            }
        )

        updates = item.setdefault("reporter_updates", [])

        updates.append(
            (
                f"Ambulance {ambulance_id} was reassigned "
                "to a higher-priority emergency."
            )
        )

        log = item.get("log")

        if isinstance(log, dict):
            log["resource_reallocated"] = ambulance_id
            log["reallocated_to_priority"] = True

        # The original incident remains handled, but its resource
        # was taken away.
        item["status"] = "reallocated"

    return {
        "success": True,
        "ambulance_id": ambulance_id,
        "from_incident": candidate["incident_id"],
    }