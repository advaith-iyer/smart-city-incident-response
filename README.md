# Smart City Incident Response Agent

An agentic multi-module emergency response system designed to coordinate responses to urban incidents across fire, medical, industrial, and traffic domains.

The system uses a central orchestrator to classify incoming reports, assess incident severity, activate specialized response modules, coordinate resources, and manage cascading emergencies.

It includes a live web dashboard for incident monitoring, city-wide visualization, response tracking, and analytics.

## Key Features

- **Multi-module response:** Specialized Fire, Medical, Factory, and Traffic modules.
- **Central orchestration:** Incident classification, prioritization, and module coordination.
- **Cascading incident handling:** Automatically generates related incidents when an emergency creates secondary risks.
- **Road-aware routing:** Uses Dijkstra's shortest-path algorithm to calculate routes while accounting for blocked roads.
- **Resource allocation:** Assigns available emergency resources based on incident requirements.
- **Continuous monitoring:** Reassesses escalated incidents and adapts response plans.
- **Live dashboard:** Visualizes incidents, response status, city infrastructure, and operational bottlenecks.
- **LLM integration:** Optional Groq-powered classification and severity assessment, with rule-based fallback.

## Project Structure

- `modules/fire` — Fire & Hazard response
- `modules/medical` — Medical / Ambulance response
- `modules/factory` — Factory / Industrial response
- `modules/traffic` — Traffic / Road response
- `orchestrator` — Central routing, prioritization, and coordination layer
- `shared` — Common contracts, city state, road graph, and optional LLM helper
- `server` — FastAPI backend and live dashboard
- `data` — Demo and seed fixtures
- `demo.py` — Original CLI demonstration

Run commands from the project root so imports such as `from shared...` resolve correctly.

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/advaith-iyer/smart-city-incident-response.git
cd smart-city-incident-response
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the dashboard

```bash
uvicorn server.app:app --reload
```

Open the following URL in your browser:

[http://127.0.0.1:8000](http://127.0.0.1:8000)

## Live Dashboard

The dashboard provides the following features:

### Live Priority Queue

- Displays reported incidents, automatically sorted by priority.
- Color-codes incidents by severity.
- Click any incident card or pulsing map marker to view its details.

### City Map

- Displays hospitals, dispensaries, fire units, factories, and traffic points from `shared/city_state.json`.
- Draws roads using the road graph.
- Displays blocked roads with red dashed lines.
- Shows incident markers colored according to severity.

### Report an Incident

- Submit incidents through the dashboard.
- Select an incident type or choose **"Not sure / let the agent classify"** to let the Orchestrator classify the report.
- The classification step uses the LLM when configured, otherwise it falls back to keyword-based logic.

### Analytics

Displays live operational information, including:

- Total incidents
- Handled and unhandled incidents
- Resources used
- Bottlenecks reported by modules

### Monitor and Adapt

Select an incident and use **"Send Update"** to simulate a follow-up report.

For example:

> The fire has spread and more smoke is visible.

The incident is reassessed through its module's Monitor & Adapt path, allowing its severity and response plan to be updated.

A low-confidence report can also be used to demonstrate false-report detection. Set the confidence slider below approximately `0.3` to test this behavior.

### Continuous Monitoring

The system runs a background monitoring loop every 5 seconds. It automatically rechecks incidents that remain in an `escalated` state in case resource or road availability has changed since the initial assessment.

### Reset Demo

Use the **Reset Demo** button in the dashboard to restore the city to its seed state between demonstrations.

## Running the CLI Demo

The original command-line demonstration is still available:

```bash
python demo.py
```

## Enabling LLM-Based Reasoning

By default, incident classification and severity assessment use fast, dependency-free, rule-based logic.

To enable optional LLM-based reasoning, the project uses Groq's API.

### 1. Get a Groq API key

Create a free API key at:

[https://console.groq.com/keys](https://console.groq.com/keys)

### 2. Configure environment variables

**Windows PowerShell**

```powershell
$env:USE_LLM_SEVERITY="1"
$env:GROQ_API_KEY="your_key_here"
uvicorn server.app:app --reload
```

**Linux / macOS**

```bash
export USE_LLM_SEVERITY=1
export GROQ_API_KEY=your_key_here
uvicorn server.app:app --reload
```

Replace `your_key_here` with your actual Groq API key.

**Important:** Never commit your API key or other secrets to GitHub. Keep them in environment variables or a local `.env` file excluded through `.gitignore`.

### 3. Model Configuration

By default, the project uses:

`llama-3.1-8b-instant`

You can override the model using the `GROQ_MODEL` environment variable. For example:

```powershell
$env:GROQ_MODEL="llama-3.3-70b-versatile"
```

### LLM Fallback Behavior

If an LLM call fails because of a missing API key, network issue, or invalid response, the system catches the error and falls back to rule-based logic.

The application is designed to continue operating even when the LLM is unavailable.

### Design Decision: Selective LLM Usage

Only Medical and Factory severity assessment are LLM-eligible because they are primarily driven by free-text incident descriptions.

- **Medical and Factory:** Can use LLM-based severity assessment.
- **Fire:** Uses structured inputs such as spread risk, wind, and crowd proximity.
- **Traffic:** Uses road and traffic data for deterministic severity assessment.
- **Orchestrator:** Can use LLM-based incident classification when automatic classification is selected.

Fire and Traffic use deterministic multi-factor assessments because their severity depends on structured operational data. Using an LLM for these assessments would not necessarily improve the decision-making process.

## Technical Improvements

### Standardized Module Output Contract

During integration, the `ModuleOutput.log` format differed between independently developed modules:

- Medical and Factory returned dictionaries.
- Fire and Traffic returned lists of strings.

This contract inconsistency was fixed. All four modules now use a standardized log structure:

```python
{
    "incident_id": "...",
    "response_time_sec": 0,
    "final_severity": 0,
    "escalation_level": "...",
    "resources_used": [],
    "bottlenecks": []
}
```

Traffic's `actions_taken` and `resources_allocated` fields were also standardized to match the structures used by the other modules:

```python
{
    "action": "...",
    "timestamp": "..."
}
```

```python
{
    "resource_type": "...",
    "resource_id": "...",
    "eta": "..."
}
```

This allows the analytics endpoint in `server/app.py` to process module outputs consistently.

### Road-Aware ETA Calculations

Previously, only the Traffic module used Dijkstra's shortest-path algorithm through `shared/road_graph.py`.

Medical, Fire, and Factory calculated travel ETAs using straight-line distance, meaning blocked roads did not affect their travel-time estimates.

This was improved by adding:

- `nearest_node()` — finds the nearest road-graph junction to a given latitude and longitude.
- `route_distance_km()` — calculates road-network distance between locations using the existing shortest-path function and includes the final straight-line distance from the snapped junctions to the actual locations.

Medical, Fire, and Factory now use a `road_aware_distance_km()` wrapper to calculate travel distances.

If the road graph is unavailable or disconnected, the wrapper falls back to straight-line distance.

The routing behavior was verified directly: blocking roads around a location increases the calculated ambulance and fire-tender ETA instead of leaving it unchanged.

**Design decision:** `resolve_factory()` intentionally continues to use straight-line distance when identifying the nearest factory. This is a location-matching operation, not a travel-time estimate.

## Technology Stack

- **Backend:** Python, FastAPI
- **Frontend:** HTML, CSS, JavaScript
- **Routing:** Dijkstra's shortest-path algorithm
- **LLM integration:** Groq API (optional)
- **Data and state:** JSON-based shared city state

## Project Goal

The goal of the Smart City Incident Response Agent is to demonstrate how specialized emergency response modules can work together through a central orchestrator to manage incidents, coordinate resources, adapt to changing conditions, and handle cascading emergencies in a simulated smart-city environment.
