from datetime import datetime
from typing import Any


class AgentTrace:
    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def add(self, step: str, message: str, data: dict[str, Any] | None = None):
        self.events.append({
            "timestamp": datetime.utcnow().isoformat(),
            "step": step,
            "message": message,
            "data": data or {},
        })

    def get_events(self) -> list[dict[str, Any]]:
        return self.events

    def clear(self):
        self.events.clear()
