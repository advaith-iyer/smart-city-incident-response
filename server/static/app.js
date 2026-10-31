const API = "";
let WORLD = null;
let BOUNDS = null;
let SELECTED_INCIDENT = null;
let PROFILE = null;  // "citizen" | "authority"

function storageGet(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : JSON.parse(v);
  } catch { return fallback; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
}
function myReports() { return storageGet("sc_my_reports", []); }
function isAuthority() { return PROFILE === "authority"; }

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
  if (status.includes("false_report")) return "status-false_report";
  if (status.includes("closed")) return "status-closed";
  return "status-escalated";
}

function summarizeResult(result) {
  const items = Array.isArray(result) ? result : [result];
  const statuses = items.map(i => i?.status || "unknown");
  return statuses.join(" + ");
}

function renderQueue(incidents) {
  const list = document.getElementById("queueList");
  if (!isAuthority()) {
    const mine = new Set(myReports());
    incidents = incidents.filter(r => mine.has(r.incident.incident_id));
  }
  document.getElementById("queueCount").textContent = incidents.length;
  list.innerHTML = "";
  if (!incidents.length) {
    list.innerHTML = `<div class="empty-state">${isAuthority()
      ? "No active incidents."
      : "You haven't reported anything yet. Use the form on the left - type or tap the mic."}</div>`;
  }
  incidents.forEach(rec => {
    const sev = getSeverity(rec);
    const card = document.createElement("div");
    card.className = "incident-card";
    card.style.borderLeftColor = SEV_COLORS[sev] || "#ffc107";
    const status = rec.status || summarizeResult(rec.result);
    card.innerHTML = `
      <div class="row1">
        <span>${rec.incident.incident_id} · ${rec.incident.incident_type}</span>
        <span class="status-pill ${statusClass(status)}">${escapeHtml(status.replace("_", " "))}</span>
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

function renderResourceBoard(world) {
  const units = (label, list) => `<div class="rb-group">${label}</div>` + (list || []).map(u => {
    const free = u.status === "available";
    return `<div class="rb-row"><span>${escapeHtml(u.id)} · ${escapeHtml(u.name || "")}</span>
      <span class="${free ? "rb-free" : "rb-busy"}">${escapeHtml(u.status || "")}</span></div>`;
  }).join("");
  const beds = (label, list) => `<div class="rb-group">${label}</div>` + (list || []).map(f => {
    const left = f.available_capacity ?? (f.capacity - f.current_load);
    return `<div class="rb-row"><span>${escapeHtml(f.id)} · ${escapeHtml(f.name || "")}</span>
      <span class="${left > 0 ? "rb-free" : "rb-full"}">${left}/${f.capacity} free</span></div>`;
  }).join("");
  document.getElementById("resourceBoard").innerHTML =
    units("Ambulances", world.ambulances) +
    units("Fire tenders", world.fire_units) +
    beds("Hospitals", world.hospitals) +
    beds("Dispensaries", world.dispensaries);
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

          <div class="status-block authority-only">
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

          <div class="modal-section authority-only">
            <div class="section-title">Actions Taken</div>
            <ul class="clean-list">
              ${actions || "<li>None</li>"}
            </ul>
          </div>

          <div class="modal-section">
            <div class="section-title">${isAuthority() ? "Resources Allocated" : "Help on the way"}</div>
            <ul class="clean-list">
              ${resources || "<li>None</li>"}
            </ul>
          </div>

          <div class="modal-section">
            <div class="section-title">${isAuthority() ? "Reporter Updates" : "Updates for you"}</div>
            <ul class="clean-list">
              ${updates || "<li>None</li>"}
            </ul>
          </div>

        </div>
`;
  }).join("");

  const STAGE_COLORS = {
    RECEIVE: "#4f8cff", CLASSIFY: "#4f8cff", VERIFY: "#9aa7c7", ROUTE: "#7e8cff",
    ASSESS: "#ffc107", DISPATCH: "#6fd68f", REALLOCATE: "#ff7043", CASCADE: "#c9b6ff",
    BOTTLENECK: "#ff8a8a", MONITOR: "#6fc4d6", RELEASE: "#6fc4d6", STATUS: "#e7ecf7",
  };
  const decisionTimeline = (rec.timeline || []).map(t => `
    <div class="dt-item" style="--dt-color:${STAGE_COLORS[t.stage] || "#4f8cff"}">
      <div class="dt-head">
        <span class="dt-stage">${escapeHtml(t.stage)}</span>
        <span class="dt-title">${escapeHtml(t.title)}</span>
        <span class="dt-time">${new Date(t.timestamp).toLocaleTimeString()}</span>
      </div>
      ${t.detail ? `<div class="dt-detail">${escapeHtml(t.detail)}</div>` : ""}
    </div>
  `).join("");

  const isFalseReport = rec.status === "false_report";

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

    ${isAuthority() ? controlActionsHtml(rec) : ""}

    <div class="modal-section authority-only">
      <div class="section-title">Decision Timeline</div>
      <div class="decision-timeline">
        ${decisionTimeline || '<div class="empty-state">No decisions recorded.</div>'}
      </div>
    </div>

    <div class="modal-section authority-only">
      <div class="section-title">Agent Decision Trace</div>
      <div id="agentTracePanel" class="agent-trace">
        <div class="empty-state">Loading agent trace...</div>
      </div>
    </div>

    <div class="update-box">
      <div class="section-title">${isAuthority() ? "Monitor &amp; Adapt" : "Send an update"}</div>
      <p class="section-subtitle">
        ${isAuthority()
          ? "Report a change in conditions and trigger a new assessment."
          : "Has anything changed? Tell us and the response will be re-assessed."}
      </p>

      <textarea
        id="updateText"
        rows="3"
        placeholder="e.g. Fire has spread, more smoke is visible."
      ></textarea>

      <button id="sendUpdateBtn" class="btn btn-primary">
        Send Update
      </button>
      ${isFalseReport && isAuthority() ? `
      <button id="verifyBtn" class="btn btn-ghost btn-verify">
        Verify report &amp; dispatch
      </button>` : ""}
    </div>

    <div class="history-section authority-only">
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

  const assignBtn = document.getElementById("assignBtn");
  if (assignBtn) {
    assignBtn.addEventListener("click", async () => {
      const sel = document.getElementById("assignSelect");
      if (!sel.value) return;
      const [resource_type, resource_id] = sel.value.split("|");
      const res = await postJSON(`/api/incidents/${SELECTED_INCIDENT}/assign`, { resource_type, resource_id });
      if (res.error) { alert(res.error); return; }
      closeModal();
      refreshAll();
    });
  }
  const closeIncBtn = document.getElementById("closeIncidentBtn");
  if (closeIncBtn) {
    closeIncBtn.addEventListener("click", async () => {
      await postJSON(`/api/incidents/${SELECTED_INCIDENT}/close`, {});
      closeModal();
      refreshAll();
    });
  }

  const verifyBtn = document.getElementById("verifyBtn");
  if (verifyBtn) {
    verifyBtn.addEventListener("click", async () => {
      const text = document.getElementById("updateText").value.trim();
      await postJSON(`/api/incidents/${SELECTED_INCIDENT}/update`, {
        description: text || rec.incident.description,
        confidence: 0.9,
      });
      closeModal();
      refreshAll();
    });
  }

  overlay.classList.remove("hidden");
}
function controlActionsHtml(rec) {
  const free = [
    ...(WORLD?.ambulances || []).filter(u => u.status === "available").map(u => ["ambulance", u]),
    ...(WORLD?.fire_units || []).filter(u => u.status === "available").map(u => ["fire_tender", u]),
  ];
  const options = free.map(([type, u]) =>
    `<option value="${type}|${escapeHtml(u.id)}">${escapeHtml(u.id)} - ${escapeHtml(u.name || type)}</option>`
  ).join("");
  const closed = rec.status === "closed";
  return `
    <div class="modal-section">
      <div class="section-title">Control Room Actions</div>
      <div class="control-actions">
        ${options
          ? `<select id="assignSelect">${options}</select>
             <button id="assignBtn" class="btn btn-primary">Dispatch unit</button>`
          : `<span class="empty-state">No ambulances or fire tenders available.</span>`}
        ${closed ? "" : `<button id="closeIncidentBtn" class="btn btn-ghost">Close incident &amp; release units</button>`}
      </div>
    </div>`;
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
    opt.value = JSON.stringify({ road_id: e.id, start_junction: e.from, end_junction: e.to });
    opt.textContent = `${e.id}: ${e.from} → ${e.to}${e.blocked ? " (already blocked)" : ""}`;
    roadSel.appendChild(opt);
  });
  (world.roads?.nodes || []).forEach(n => {
    const opt = document.createElement("option");
    opt.value = n.id;
    opt.textContent = `${n.name} (${n.id})`;
    destSel.appendChild(opt);
  });
  syncDestToRoad();
}

function syncDestToRoad() {
  const roadSel = document.getElementById("f_road");
  if (!roadSel.value) return;
  document.getElementById("f_dest").value = JSON.parse(roadSel.value).end_junction;
}
document.getElementById("f_road").addEventListener("change", syncDestToRoad);

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

  const created = await postJSON("/api/incidents", {
    incident_type: type,
    description: desc,
    lat: loc.lat,
    lng: loc.lng,
    area_name: loc.area_name,
    reporter: isAuthority() ? "control_room" : "citizen",
    confidence,
    severity_inputs,
  });

  const newId = created?.incident?.incident_id;
  if (newId) {
    storageSet("sc_my_reports", [...myReports(), newId]);
    const msg = document.getElementById("submitMsg");
    msg.textContent = `Report ${newId} received - status: ${(created.status || "processing").replace("_", " ")}.`;
    setTimeout(() => { if (msg.textContent.includes(newId)) msg.textContent = ""; }, 6000);
  }

  document.getElementById("f_desc").value = "";
  const roadSel = document.getElementById("f_road");
  const selectedRoad = roadSel.value;
  populateRoadOptions(await getJSON("/api/world"));
  if (selectedRoad) { roadSel.value = selectedRoad; syncDestToRoad(); }
  refreshAll();
});

document.getElementById("resetBtn").addEventListener("click", async () => {
  await postJSON("/api/reset", {});
  storageSet("sc_my_reports", []);
  closeModal();
  SELECTED_INCIDENT = null;
  const world = await getJSON("/api/world");
  populateLocationOptions(world);
  populateRoadOptions(world);
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
  renderResourceBoard(world);
}

function setProfile(profile) {
  PROFILE = profile;
  storageSet("sc_profile", profile);
  document.body.dataset.profile = profile;
  document.getElementById("profileChip").textContent =
    profile === "authority" ? "Disaster Management Authority" : "Citizen";
  document.getElementById("queueTitle").textContent =
    profile === "authority" ? "Live Priority Queue" : "My Reports";
  document.getElementById("profileGate").classList.add("hidden");
  closeModal();
  if (WORLD) refreshAll();
}
function showProfileGate() {
  document.getElementById("profileGate").classList.remove("hidden");
}
document.querySelectorAll(".gate-card").forEach(card =>
  card.addEventListener("click", () => setProfile(card.dataset.profile))
);
document.getElementById("switchProfileBtn").addEventListener("click", showProfileGate);

// Voice reporting via the browser's Web Speech API (Chrome / Edge).
(function setupVoice() {
  const micBtn = document.getElementById("micBtn");
  const status = document.getElementById("voiceStatus");
  const desc = document.getElementById("f_desc");
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    micBtn.disabled = true;
    micBtn.title = "Voice input needs Chrome or Edge";
    micBtn.style.opacity = 0.4;
    return;
  }
  const rec = new SR();
  rec.lang = "en-IN";
  rec.interimResults = true;
  rec.continuous = false;
  let listening = false;
  let base = "";

  rec.onstart = () => {
    listening = true;
    micBtn.classList.add("listening");
    status.textContent = "Listening... describe the emergency.";
  };
  rec.onresult = e => {
    let text = "";
    for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
    desc.value = (base ? base + " " : "") + text.trim();
  };
  rec.onerror = e => {
    status.textContent = e.error === "not-allowed"
      ? "Microphone permission denied."
      : `Voice input error: ${e.error}`;
  };
  rec.onend = () => {
    listening = false;
    micBtn.classList.remove("listening");
    if (!status.textContent.startsWith("Voice input error") && !status.textContent.startsWith("Microphone")) {
      status.textContent = desc.value.trim() ? "Got it - check the text, then submit." : "";
    }
  };
  micBtn.addEventListener("click", () => {
    if (listening) { rec.stop(); return; }
    base = desc.value.trim();
    status.textContent = "";
    try { rec.start(); } catch { /* already started */ }
  });
})();

async function init() {
  const saved = storageGet("sc_profile", null);
  if (saved === "citizen" || saved === "authority") setProfile(saved);
  else showProfileGate();

  const world = await getJSON("/api/world");
  WORLD = world;
  populateLocationOptions(world);
  populateRoadOptions(world);
  await refreshAll();
  setInterval(refreshAll, 3000);
}

init();
