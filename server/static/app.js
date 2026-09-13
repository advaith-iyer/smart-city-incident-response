const API = "";
let WORLD = null;
let BOUNDS = null;
let SELECTED_INCIDENT = null;

const SEV_COLORS = { 1: "#4caf50", 2: "#8bc34a", 3: "#ffc107", 4: "#ff7043", 5: "#e53935" };

// ---------------------------------------------------------------------------
// Clock
// ---------------------------------------------------------------------------
function tickClock() {
  document.getElementById("clock").textContent = new Date().toLocaleTimeString();
}
setInterval(tickClock, 1000);
tickClock();

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------
async function getJSON(url) {
  const r = await fetch(API + url);
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(API + url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

// ---------------------------------------------------------------------------
// Map: project lat/lng -> SVG coords
// ---------------------------------------------------------------------------
function computeBounds(world) {
  const pts = [];
  (world.roads?.nodes || []).forEach(n => pts.push([n.lat, n.lng]));
  ["hospitals", "dispensaries", "factories", "traffic_resources", "ambulances", "fire_units"].forEach(key => {
    (world[key] || []).forEach(f => {
      const loc = f.location || f;
      if (loc && loc.lat != null) pts.push([loc.lat, loc.lng]);
    });
  });
  const lats = pts.map(p => p[0]), lngs = pts.map(p => p[1]);
  const pad = 0.006;
  return {
    minLat: Math.min(...lats) - pad, maxLat: Math.max(...lats) + pad,
    minLng: Math.min(...lngs) - pad, maxLng: Math.max(...lngs) + pad,
  };
}
function project(lat, lng) {
  const W = 760, H = 560, MX = 20, MY = 20;
  const x = MX + ((lng - BOUNDS.minLng) / (BOUNDS.maxLng - BOUNDS.minLng)) * W;
  const y = MY + (1 - (lat - BOUNDS.minLat) / (BOUNDS.maxLat - BOUNDS.minLat)) * H;
  return [x, y];
}

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

function renderMap(world, incidents) {
  const svg = document.getElementById("cityMap");
  svg.innerHTML = "";
  const nodeById = {};
  (world.roads?.nodes || []).forEach(n => nodeById[n.id] = n);

  // roads
  (world.roads?.edges || []).forEach(e => {
    const a = nodeById[e.from], b = nodeById[e.to];
    if (!a || !b) return;
    const [x1, y1] = project(a.lat, a.lng);
    const [x2, y2] = project(b.lat, b.lng);
    svg.appendChild(svgEl("line", {
      x1, y1, x2, y2,
      stroke: e.blocked ? "#e53935" : "#2a3654",
      "stroke-width": e.blocked ? 3 : 1.5,
      "stroke-dasharray": e.blocked ? "4,3" : "none",
    }));
  });

  // junction dots
  (world.roads?.nodes || []).forEach(n => {
    const [x, y] = project(n.lat, n.lng);
    svg.appendChild(svgEl("circle", { cx: x, cy: y, r: 2, fill: "#3a4a70" }));
  });

  // facilities
  const facilityTypes = [
    ["hospitals", "#e53935", "H"],
    ["dispensaries", "#ffb74d", "D"],
    ["fire_units", "#ff5722", "F"],
    ["factories", "#9575cd", "P"],
    ["traffic_resources", "#4fc3f7", "T"],
  ];
  facilityTypes.forEach(([key, color, label]) => {
    (world[key] || []).forEach(f => {
      const loc = f.location || f;
      if (loc.lat == null) return;
      const [x, y] = project(loc.lat, loc.lng);
      const g = svgEl("g", {});
      g.appendChild(svgEl("circle", { cx: x, cy: y, r: 7, fill: color, opacity: 0.85, stroke: "#0d1626", "stroke-width": 1.5 }));
      const t = svgEl("text", { x, y: y + 3.5, fill: "#0d1626", "font-size": 8, "text-anchor": "middle", "font-weight": "700" });
      t.textContent = label;
      g.appendChild(t);
      const title = svgEl("title", {});
      title.textContent = `${f.name || key} ${f.current_load != null ? `(${f.current_load}/${f.capacity})` : ""}`;
      g.appendChild(title);
      svg.appendChild(g);
    });
  });

  // incidents
  incidents.forEach(rec => {
    const loc = rec.incident.location;
    if (!loc || loc.lat == null) return;
    const [x, y] = project(loc.lat, loc.lng);
    const sev = getSeverity(rec);
    const color = SEV_COLORS[sev] || "#ffc107";
    const ring = svgEl("circle", { cx: x, cy: y, r: 12, fill: "none", stroke: color, "stroke-width": 2, opacity: 0.5 });
    ring.innerHTML = `<animate attributeName="r" values="9;16;9" dur="1.6s" repeatCount="indefinite" />
                       <animate attributeName="opacity" values="0.7;0;0.7" dur="1.6s" repeatCount="indefinite" />`;
    svg.appendChild(ring);
    const dot = svgEl("circle", { cx: x, cy: y, r: 6, fill: color, stroke: "white", "stroke-width": 1, cursor: "pointer" });
    dot.addEventListener("click", () => openModal(rec));
    svg.appendChild(dot);
  });
}

function getSeverity(rec) {
  const result = rec.result;
  const items = Array.isArray(result) ? result : [result];
  let max = 0;
  items.forEach(it => {
    const s = it && it.severity;
    const score = typeof s === "object" && s !== null ? (s.score ?? 0) : 0;
    if (score > max) max = score;
  });
  return max;
}

// ---------------------------------------------------------------------------
// Queue rendering
// ---------------------------------------------------------------------------
function statusClass(status) {
  if (!status) return "status-escalated";
  if (status.includes("resolved")) return "status-resolved";
  if (status.includes("reroute")) return "status-rerouted";
  if (status.includes("unhandled")) return "status-unhandled";
  return "status-escalated";
}

function summarizeResult(result) {
  const items = Array.isArray(result) ? result : [result];
  const statuses = items.map(i => i?.status || "unknown");
  return statuses.join(" + ");
}

function renderQueue(incidents) {
  const list = document.getElementById("queueList");
  document.getElementById("queueCount").textContent = incidents.length;
  list.innerHTML = "";
  incidents.forEach(rec => {
    const sev = getSeverity(rec);
    const card = document.createElement("div");
    card.className = "incident-card";
    card.style.borderLeftColor = SEV_COLORS[sev] || "#ffc107";
    const status = summarizeResult(rec.result);
    card.innerHTML = `
      <div class="row1">
        <span>${rec.incident.incident_id} · ${rec.incident.incident_type}</span>
        <span class="status-pill ${statusClass(status)}">${status}</span>
      </div>
      <div class="desc">${escapeHtml(rec.incident.description)}</div>
      <div class="row2">
        <span>Severity: ${sev || "-"}/5</span>
        <span>${rec.incident.location?.area_name || ""}</span>
      </div>
    `;
    card.addEventListener("click", () => openModal(rec));
    list.appendChild(card);
  });
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------
function renderAnalytics(a) {
  const grid = document.getElementById("analytics");
  grid.innerHTML = `
    <div class="stat"><div class="val">${a.total_incidents}</div><div class="lbl">Total</div></div>
    <div class="stat"><div class="val">${a.handled}</div><div class="lbl">Handled</div></div>
    <div class="stat"><div class="val">${a.unhandled}</div><div class="lbl">Unhandled</div></div>
    <div class="stat"><div class="val">${a.resources_used_total}</div><div class="lbl">Resources Used</div></div>
  `;
  const b = document.getElementById("bottlenecks");
  if (a.bottlenecks && a.bottlenecks.length) {
    b.innerHTML = "<div style='color:var(--muted);font-size:0.72rem;margin-bottom:6px;'>BOTTLENECKS</div>" +
      a.bottlenecks.map(x => `<div class="b-item">${escapeHtml(x.incident_id)}: ${escapeHtml(x.bottleneck)}</div>`).join("");
  } else {
    b.innerHTML = "";
  }
}

// ---------------------------------------------------------------------------
// Modal (incident detail + send update)
// ---------------------------------------------------------------------------
async function openModal(rec) {
  SELECTED_INCIDENT = rec.incident.incident_id;
  const overlay = document.getElementById("modalOverlay");
  const body = document.getElementById("modalBody");
  const items = Array.isArray(rec.result) ? rec.result : [rec.result];

  let resultHtml = items.map(it => {
    if (!it) return "";
    const actions = (it.actions_taken || []).map(a => `<li>${escapeHtml(typeof a === "string" ? a : a.action)}</li>`).join("");
    const resources = (it.resources_allocated || []).map(r => `<li>${escapeHtml(typeof r === "string" ? r : `${r.resource_type} (${r.resource_id}) ETA ${r.eta}`)}</li>`).join("");
    const updates = (it.reporter_updates || []).map(u => `<li>${escapeHtml(u)}</li>`).join("");
    const severityScore =
      it.severity && typeof it.severity === "object"
        ? (it.severity.score ?? "?")
        : "?";

    const severityReason =
      it.severity && typeof it.severity === "object"
        ? (it.severity.reasoning || it.severity.level || "")
        : ""; 
    return `
      <div class="result-section">

        <div class="result-summary">

          <div class="status-block">
            <div class="detail-label">Status</div>
            <div class="status-pill ${statusClass(it.status || "")}">
              ${escapeHtml(it.status || "Unhandled")}
            </div>
          </div>

          <div class="status-block">
            <div class="detail-label">Escalation</div>
            <div class="detail-value">
              ${escapeHtml(it.escalation_level || "—")}
            </div>
          </div>

          <div class="severity-block">
            <div class="detail-label">Severity</div>
            <div class="severity-score">${escapeHtml(String(severityScore))}/5</div>
              ${
                severityReason
                  ? `<div class="severity-reason">${escapeHtml(severityReason)}</div>`
                  : ""
              }
            </div>
          </div>

          <div class="modal-section">
            <div class="section-title">Actions Taken</div>
            <ul class="clean-list">
              ${actions || "<li>None</li>"}
            </ul>
          </div>

          <div class="modal-section">
            <div class="section-title">Resources Allocated</div>
            <ul class="clean-list">
              ${resources || "<li>None</li>"}
            </ul>
          </div>

          <div class="modal-section">
            <div class="section-title">Reporter Updates</div>
            <ul class="clean-list">
              ${updates || "<li>None</li>"}
            </ul>
          </div>

        </div>
`;
  }).join("");

  const history = (rec.history || []).slice().reverse().map(h => `
    <div class="timeline-item"><span class="t-event">${escapeHtml(h.event)}</span> — ${new Date(h.timestamp).toLocaleTimeString()}${h.note ? " · " + escapeHtml(h.note) : ""}</div>
  `).join("");

    body.innerHTML = `
    <div class="modal-header">
      <div>
        <div class="modal-eyebrow">INCIDENT DETAILS</div>
        <h3>${escapeHtml(rec.incident.incident_id)}</h3>
        <span class="modal-type">${escapeHtml(rec.incident.incident_type)}</span>
      </div>
    </div>

    <div class="detail-grid">
      <div class="detail-card detail-card-wide">
        <div class="detail-label">Description</div>
        <div class="detail-value">
          ${escapeHtml(rec.incident.description)}
        </div>
      </div>

      <div class="detail-card">
        <div class="detail-label">Confidence</div>
        <div class="detail-value">
          ${Number(rec.incident.confidence ?? 0).toFixed(2)}
        </div>
      </div>

      <div class="detail-card">
        <div class="detail-label">Location</div>
        <div class="detail-value">
          ${escapeHtml(rec.incident.location?.area_name || "Unknown")}
        </div>
      </div>
    </div>

    ${resultHtml}

    <div class="modal-section">
      <div class="section-title">Agent Decision Trace</div>
      <div id="agentTracePanel" class="agent-trace">
        <div class="empty-state">Loading agent trace...</div>
      </div>
    </div>

    <div class="update-box">
      <div class="section-title">Monitor &amp; Adapt</div>
      <p class="section-subtitle">
        Report a change in conditions and trigger a new assessment.
      </p>

      <textarea
        id="updateText"
        rows="3"
        placeholder="e.g. Fire has spread, more smoke is visible."
      ></textarea>

      <button id="sendUpdateBtn" class="btn btn-primary">
        Send Update
      </button>
    </div>

    <div class="history-section">
      <div class="section-title">Incident History</div>
      ${history || '<div class="empty-state">No history recorded.</div>'}
    </div>
  `;

  try {
    const traceData = await getJSON(`/api/incidents/${SELECTED_INCIDENT}/agent-trace`);
    const panel = document.getElementById("agentTracePanel");
    const trace = traceData.trace || [];

    panel.innerHTML = trace.map(e => `
      <div class="agent-step">
        <div class="agent-step-name">${escapeHtml(e.step)}</div>
        <div class="agent-step-message">${escapeHtml(e.message)}</div>
      </div>
    `).join("") || '<div class="empty-state">No agent trace available.</div>';
  } catch (err) {
    console.error("Agent trace load failed:", err);
  }

  document.getElementById("sendUpdateBtn").addEventListener("click", async () => {
    const text = document.getElementById("updateText").value.trim();
    if (!text) return;
    await postJSON(`/api/incidents/${SELECTED_INCIDENT}/update`, { description: text });
    closeModal();
    refreshAll();
  });

  overlay.classList.remove("hidden");
}
function closeModal() {
  document.getElementById("modalOverlay").classList.add("hidden");
}
document.getElementById("modalClose").addEventListener("click", closeModal);
document.getElementById("modalOverlay").addEventListener("click", e => {
  if (e.target.id === "modalOverlay") closeModal();
});

// ---------------------------------------------------------------------------
// Report form
// ---------------------------------------------------------------------------
function populateLocationOptions(world) {
  const sel = document.getElementById("f_location");
  sel.innerHTML = "";
  (world.roads?.nodes || []).forEach(n => {
    const opt = document.createElement("option");
    opt.value = JSON.stringify({ lat: n.lat, lng: n.lng, area_name: n.name, junction: n.id });
    opt.textContent = `${n.name} (${n.id})`;
    sel.appendChild(opt);
  });
}
function populateRoadOptions(world) {
  const roadSel = document.getElementById("f_road");
  const destSel = document.getElementById("f_dest");
  roadSel.innerHTML = "";
  destSel.innerHTML = "";
  (world.roads?.edges || []).forEach(e => {
    const opt = document.createElement("option");
    opt.value = JSON.stringify({ road_id: e.id, start_junction: e.from });
    opt.textContent = `${e.id}: ${e.from} → ${e.to}${e.blocked ? " (already blocked)" : ""}`;
    roadSel.appendChild(opt);
  });
  (world.roads?.nodes || []).forEach(n => {
    const opt = document.createElement("option");
    opt.value = n.id;
    opt.textContent = `${n.name} (${n.id})`;
    destSel.appendChild(opt);
  });
}

document.getElementById("f_type").addEventListener("change", e => {
  document.getElementById("f_trafficFields").classList.toggle("hidden", e.target.value !== "road_blockage");
});
document.getElementById("f_confidence").addEventListener("input", e => {
  document.getElementById("f_confVal").textContent = e.target.value;
});

document.getElementById("reportForm").addEventListener("submit", async e => {
  e.preventDefault();
  const type = document.getElementById("f_type").value;
  const desc = document.getElementById("f_desc").value.trim();
  if (!desc) return;
  const loc = JSON.parse(document.getElementById("f_location").value);
  const confidence = parseFloat(document.getElementById("f_confidence").value);

  let severity_inputs = {};
  if (type === "road_blockage") {
    const road = JSON.parse(document.getElementById("f_road").value);
    const dest = document.getElementById("f_dest").value;
    severity_inputs = {
      road_id: road.road_id,
      start_junction: road.start_junction,
      destination_junction: dest,
      traffic_volume: "high",
      emergency_access_blocked: true,
    };
  }

  await postJSON("/api/incidents", {
    incident_type: type,
    description: desc,
    lat: loc.lat,
    lng: loc.lng,
    area_name: loc.area_name,
    confidence,
    severity_inputs,
  });

  document.getElementById("f_desc").value = "";
  refreshAll();
});

document.getElementById("resetBtn").addEventListener("click", async () => {
  await postJSON("/api/reset", {});
  refreshAll();
});

// ---------------------------------------------------------------------------
// Refresh loop
// ---------------------------------------------------------------------------
async function refreshAll() {
  const [world, incidents, analyticsData] = await Promise.all([
    getJSON("/api/world"),
    getJSON("/api/incidents"),
    getJSON("/api/analytics"),
  ]);
  WORLD = world;
  BOUNDS = computeBounds(world);
  renderMap(world, incidents);
  renderQueue(incidents);
  renderAnalytics(analyticsData);
}

async function init() {
  const world = await getJSON("/api/world");
  WORLD = world;
  populateLocationOptions(world);
  populateRoadOptions(world);
  await refreshAll();
  setInterval(refreshAll, 3000);
}

init();
