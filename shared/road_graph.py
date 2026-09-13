import heapq
import math


def _haversine_km(lat1, lng1, lat2, lng2):
    """Straight-line distance in km between two lat/lng points."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def nearest_node(roads, lat, lng):
    """
    Find the road-graph junction nearest to an arbitrary lat/lng point
    (e.g. an incident location, or a hospital/dispensary/factory that
    isn't itself sitting exactly on a junction).

    Returns (node_id, last_mile_distance_km), or (None, None) if the
    graph has no nodes.
    """
    best_id, best_dist = None, float("inf")
    for node in roads.get("nodes", []):
        d = _haversine_km(lat, lng, node["lat"], node["lng"])
        if d < best_dist:
            best_dist, best_id = d, node["id"]
    return best_id, (best_dist if best_id is not None else None)


def route_distance_km(roads, from_lat, from_lng, to_lat, to_lng):
    """
    Road-aware distance between two arbitrary points, respecting blocked
    edges.

    Snaps each point to its nearest junction, finds the shortest path
    between those junctions via Dijkstra (shortest_path(), which already
    ignores blocked edges), and adds the "last mile" straight-line hop
    from each point to its junction on top of the graph distance.

    Returns a dict: {"distance_km": float, "path": list[str] | None,
    "used_road_graph": bool}. Falls back to straight-line distance
    (used_road_graph=False, path=None) if the graph has no nodes, or if
    no route exists between the two junctions (e.g. blocked roads have
    cut the area off entirely) -- callers should treat that as a
    bottleneck worth logging rather than a crash.
    """
    from_node, from_last_mile = nearest_node(roads, from_lat, from_lng)
    to_node, to_last_mile = nearest_node(roads, to_lat, to_lng)

    if from_node is None or to_node is None:
        return {
            "distance_km": _haversine_km(from_lat, from_lng, to_lat, to_lng),
            "path": None,
            "used_road_graph": False,
        }

    if from_node == to_node:
        return {
            "distance_km": from_last_mile + to_last_mile,
            "path": [from_node],
            "used_road_graph": True,
        }

    route = shortest_path(roads, from_node, to_node)
    if route is None:
        # Blocked roads have disconnected the two junctions -- fall back
        # to straight-line distance so callers still get a usable number,
        # but flag it so it can be surfaced as a bottleneck.
        return {
            "distance_km": _haversine_km(from_lat, from_lng, to_lat, to_lng),
            "path": None,
            "used_road_graph": False,
        }

    return {
        "distance_km": from_last_mile + route["distance_km"] + to_last_mile,
        "path": route["path"],
        "used_road_graph": True,
    }


def shortest_path(roads, start, end):
    """
    Find the shortest available route between two junctions.

    Blocked roads are ignored.
    """

    # Build the graph using only unblocked roads
    graph = {}

    for node in roads["nodes"]:
        graph[node["id"]] = []

    for edge in roads["edges"]:
        if not edge["blocked"]:
            graph[edge["from"]].append(
                (edge["to"], edge["distance_km"])
            )

    # Dijkstra's algorithm
    distances = {node: float("inf") for node in graph}
    previous = {node: None for node in graph}

    distances[start] = 0
    priority_queue = [(0, start)]

    while priority_queue:
        current_distance, current_node = heapq.heappop(priority_queue)

        if current_distance > distances[current_node]:
            continue

        if current_node == end:
            break

        for neighbor, distance in graph[current_node]:
            new_distance = current_distance + distance

            if new_distance < distances[neighbor]:
                distances[neighbor] = new_distance
                previous[neighbor] = current_node

                heapq.heappush(
                    priority_queue,
                    (new_distance, neighbor)
                )

    # No route exists
    if distances[end] == float("inf"):
        return None

    # Reconstruct route
    path = []
    current = end

    while current is not None:
        path.append(current)
        current = previous[current]

    path.reverse()

    return {
        "distance_km": distances[end],
        "path": path
    }