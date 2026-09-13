import json
from pathlib import Path


WORLD_PATH = Path(__file__).parent / "city_state.json"


def load_world() -> dict:
    """Load the shared city state from city_state.json."""
    with WORLD_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_world(world: dict) -> None:
    """Save the updated city state back to city_state.json."""
    with WORLD_PATH.open("w", encoding="utf-8") as file:
        json.dump(world, file, indent=2)
        file.write("\n")
