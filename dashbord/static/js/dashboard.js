
let chart     = null;
let chartNode = "NODE_A";
let valveOpen = false;
let mqttStatus = false;
let alertCount = 0;

const nodeData = {
  NODE_A: { moisture: null, temp: null, hum: null, valve: "CLOSED", angle: 0 }
};

function toggleSidebar() {
  const sidebar  = document.getElementById("sidebar");
  const overlay  = document.getElementById("sidebarOverlay");
  const isOpen   = sidebar.classList.contains("open");
  if (isOpen) { closeSidebar(); }
  else { sidebar.classList.add("open"); overlay.classList.add("active"); }
}

function closeSidebar() {
  document.getElementById("sidebar")?.classList.remove("open");
  document.getElementById("sidebarOverlay")?.classList.remove("active");
}

document.addEventListener("DOMContentLoaded", () => {
  // Close sidebar on nav click (mobile)
  document.querySelectorAll(".nav-item").forEach(link => {
    link.addEventListener("click", () => {
      if (window.innerWidth <= 768) closeSidebar();
    });
  });

  updateClock();
  setInterval(updateClock, 1000);
  initChart();
  loadLatestSensors();
  loadValveState();
  loadAlerts();
  checkMqttStatus();
  startSSE();
  setInterval(checkMqttStatus, 10000);
});

function updateClock() {
  const now  = new Date();
  const time = now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
  const date = now.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  setText("currentTime", time);
  setText("currentDate", date);
}

function startSSE() {
  const evtSource = new EventSource("/api/stream");

  evtSource.addEventListener("connected", () => {
    console.log("[SSE] Connected");
  });

  evtSource.addEventListener("sensor_update", (e) => {
    const d = JSON.parse(e.data);
    if (!nodeData[d.node]) nodeData[d.node] = {};
    Object.assign(nodeData[d.node], {
      moisture: d.moisture, temp: d.temp, hum: d.hum,
      valve: d.valve, angle: d.angle
    });
    updateKPIs(d);
    updateNodeCard(d.node, d);
    valveOpen = d.valve === "OPEN";
    updateValveDisplay(valveOpen, d.angle);
    syncSlider(d.angle);
    if (d.node === chartNode) addChartPoint(d.moisture);
    flashUpdate();

    // Update status text
    const sm = document.getElementById("statusMsg");
    if (sm) sm.textContent = `Last update: ${d.ts || "now"}`;
  });

  evtSource.addEventListener("alert", (e) => {
    const d = JSON.parse(e.data);
    showAlertBanner(d);
    alertCount++;
    loadAlerts();
  });

  evtSource.addEventListener("ack", (e) => {
    const d = JSON.parse(e.data);
    showToast(d.message);
  });

  evtSource.addEventListener("heartbeat", () => {});

  evtSource.onerror = () => {
    console.warn("[SSE] Disconnected — retrying in 3s...");
    setTimeout(startSSE, 3000);
    evtSource.close();
  };
}

function updateKPIs(d) {
  setText("kpiMoisture", d.moisture?.toFixed(1) ?? "--");
  setText("kpiTemp",     d.temp?.toFixed(1)     ?? "--");
  setText("kpiHumidity", d.hum?.toFixed(1)      ?? "--");
  setText("kpiAngle",    d.angle ?? "0");

  const badge = document.getElementById("kpiMoistureBadge");
  if (badge) {
    if      (d.moisture < 20) { badge.textContent = "Critical Dry"; badge.className = "kpi-badge badge-red"; }
    else if (d.moisture < 40) { badge.textContent = "Needs Water";  badge.className = "kpi-badge badge-gold"; }
    else if (d.moisture > 80) { badge.textContent = "Over-watered"; badge.className = "kpi-badge badge-gold"; }
    else                      { badge.textContent = "Optimal";      badge.className = "kpi-badge badge-green"; }
  }

  const tempBadge = document.getElementById("kpiTempBadge");
  if (tempBadge) {
    if      (d.temp > 35) tempBadge.textContent = "Too Hot";
    else if (d.temp > 30) tempBadge.textContent = "Warm";
    else                  tempBadge.textContent = "Normal";
  }

  const humBadge = document.getElementById("kpiHumidityBadge");
  if (humBadge) {
    if      (d.hum > 70) humBadge.textContent = "High";
    else if (d.hum < 30) humBadge.textContent = "Low";
    else                 humBadge.textContent = "Normal";
  }

  const valveBadge = document.getElementById("kpiValveBadge");
  if (valveBadge) valveBadge.textContent = d.valve === "OPEN" ? `OPEN ${d.angle}deg` : "CLOSED";
}

function updateNodeCard(nodeId, d) {
  const card = document.getElementById(`node-${nodeId}`);
  if (!card) return;
  const pct    = Math.max(0, Math.min(100, d.moisture || 0));
  const isLow  = pct < 40;
  const barFill = card.querySelector(".node-bar-fill");
  const valEl   = card.querySelector(".node-value");
  const metaEl  = card.querySelector(".node-meta");
  const statusEl= card.querySelector(".node-status-dot");
  if (barFill)  { barFill.style.width = pct + "%"; barFill.className = "node-bar-fill" + (isLow ? " low" : ""); }
  if (valEl)    { valEl.textContent = pct.toFixed(0) + "%"; valEl.className = "node-value" + (isLow ? " low" : ""); }
  if (metaEl)   { metaEl.textContent = `${d.temp?.toFixed(1)}C / ${d.hum?.toFixed(0)}% RH`; }
  if (statusEl) { statusEl.className = "node-status-dot" + (isLow ? " warning" : ""); }
}

async function loadLatestSensors() {
  try {
    const res  = await fetch("/api/sensors/latest");
    const data = await res.json();
    const grid = document.getElementById("nodesGrid");
    if (!grid) return;
    grid.innerHTML = "";
    const nodes = Object.keys(data).length > 0 ? Object.keys(data) : ["NODE_A"];
    nodes.forEach(nodeId => grid.appendChild(makeNodeCard(nodeId, data[nodeId] || {})));
    if (data["NODE_A"]) {
      const n = data["NODE_A"];

      updateKPIs({ moisture: n.soil_moisture, temp: n.temperature, hum: n.air_humidity, angle: nodeData["NODE_A"].angle || 0, valve: nodeData["NODE_A"].valve || "CLOSED" });
      nodeData["NODE_A"] = { ...nodeData["NODE_A"], moisture: n.soil_moisture, temp: n.temperature, hum: n.air_humidity };
    }
  } catch (e) { console.error("loadLatestSensors:", e); }
}

function makeNodeCard(nodeId, d) {
  const moisture = d.soil_moisture ?? 0;
  const isLow    = moisture < 40;
  const div      = document.createElement("div");
  div.className  = "node-card";
  div.id         = `node-${nodeId}`;
  div.innerHTML  = `
    <div class="node-header">
      <span class="node-id">${nodeId}</span>
      <span class="node-status-dot ${isLow ? "warning" : ""}"></span>
    </div>
    <div class="node-bar">
      <div class="node-bar-fill ${isLow ? "low" : ""}" style="width:${moisture}%"></div>
    </div>
    <div class="node-value ${isLow ? "low" : ""}">${moisture.toFixed(0)}%</div>
    <div class="node-meta">${d.temperature?.toFixed(1) ?? "--"}C / ${d.air_humidity?.toFixed(0) ?? "--"}% RH</div>
  `;
  return div;
}

function initChart() {
  const canvas = document.getElementById("moistureChart");
  if (!canvas) return;
  chart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels: [],
      datasets: [{
        label: "Soil Moisture %",
        data: [],
        borderColor:     "#5F8F5B",
        backgroundColor: "rgba(95,143,91,0.08)",
        borderWidth: 2.5,
        pointRadius: 3,
        pointBackgroundColor: "#0F5A37",
        tension: 0.4,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      resizeDelay: 0,
      animation: { duration: 300 },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `Moisture: ${ctx.parsed.y.toFixed(1)}%` } }
      },
      scales: {
        y: { min: 0, max: 100, grid: { color: "rgba(168,201,143,0.15)" }, ticks: { color: "#4a6b4a", callback: v => v + "%" } },
        x: { grid: { display: false }, ticks: { color: "#4a6b4a", maxTicksLimit: 8 } }
      }
    }
  });
  loadChartHistory("NODE_A");
}

async function loadChartHistory(nodeId) {
  try {
    const res  = await fetch(`/api/sensors/history/${nodeId}`);
    const data = await res.json();
    if (!chart) return;
    chart.data.labels              = data.map(r => r.timestamp.slice(11, 16));
    chart.data.datasets[0].data   = data.map(r => r.soil_moisture);
    chart.update();
  } catch (e) { console.error("loadChartHistory:", e); }
}

function addChartPoint(moisture) {
  if (!chart) return;
  const now = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
  chart.data.labels.push(now);
  chart.data.datasets[0].data.push(moisture);
  if (chart.data.labels.length > 24) {
    chart.data.labels.shift();
    chart.data.datasets[0].data.shift();
  }
  chart.update("none");
}

async function loadValveState() {
  try {
    const res  = await fetch("/api/valve");
    const data = await res.json();
    valveOpen  = Boolean(data.is_open);
    updateValveDisplay(valveOpen, data.servo_angle);
    syncSlider(data.servo_angle);
    setText("kpiAngle", data.servo_angle ?? "0");
  } catch (e) { console.error("loadValveState:", e); }
}

async function toggleValve() {
  const newState = !valveOpen;
  try {
    const res  = await fetch("/api/valve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ open: newState })
    });
    const data = await res.json();
    valveOpen  = newState;
    const angle = newState ? 120 : 0;
    updateValveDisplay(newState, angle);
    syncSlider(angle);
    showToast(data.mqtt_sent ? "Command sent to ESP32 Gateway" : "MQTT not connected — saved to DB only");
  } catch (e) { console.error("toggleValve:", e); }
}

function updateValveDisplay(isOpen, angle) {
  const circle  = document.getElementById("valveCircle");
  const status  = document.getElementById("valveStatus");
  const angleEl = document.getElementById("valveAngle");
  if (!circle) return;
  if (isOpen) {
    circle.classList.add("open");
    if (status) status.textContent = "OPEN";
  } else {
    circle.classList.remove("open");
    if (status) status.textContent = "CLOSED";
  }
  if (angleEl) angleEl.textContent = (angle ?? 0) + "°";
}

function updateSliderDisplay(val) {
  const display = document.getElementById("sliderDisplay");
  if (display) display.textContent = val + "°";
}

function syncSlider(angle) {
  const slider  = document.getElementById("servoSlider");
  const display = document.getElementById("sliderDisplay");
  if (slider)  slider.value        = angle;
  if (display) display.textContent = angle + "°";
}

function setServoAngle(angle) { syncSlider(angle); }

async function sendManualAngle() {
  const slider = document.getElementById("servoSlider");
  if (!slider) return;
  const angle = parseInt(slider.value);
  try {
    const res  = await fetch("/api/valve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ angle })
    });
    const data = await res.json();
    valveOpen  = angle > 0;
    updateValveDisplay(valveOpen, angle);
    showToast(data.mqtt_sent
      ? `Servo set to ${angle}deg — sent to Gateway`
      : `Servo set to ${angle}deg — MQTT not connected`);
  } catch (e) {
    console.error("sendManualAngle:", e);
    showToast("Failed to send command");
  }
}

async function loadAlerts() {
  try {
    const res  = await fetch("/api/alerts");
    const data = await res.json();
    const list  = document.getElementById("eventsList");
    const badge = document.getElementById("eventsBadge");
    if (!list) return;
    if (data.length === 0) {
      list.innerHTML = `<div class="events-loading">No alerts — system healthy</div>`;
      if (badge) badge.textContent = "0 Alerts";
      return;
    }
    const unacked = data.filter(a => !a.acknowledged).length;
    if (badge) badge.textContent = unacked > 0 ? `${unacked} Active` : "All Clear";
    list.innerHTML = data.slice(0, 8).map(a => `
      <div class="event-item" style="${a.acknowledged ? "opacity:0.5" : ""}">
        <div class="event-tag">${alertTag(a.alert_type)}</div>
        <div>
          <div class="event-node">${a.node_id} · ${a.alert_type}</div>
          <div class="event-detail">${a.detail || a.raw_message?.slice(0,40)} · ${a.timestamp?.slice(11,16)}</div>
        </div>
        ${!a.acknowledged ? `<button onclick="ackAlert(${a.id})" class="btn-ack">ACK</button>` : ""}
      </div>
    `).join("");
  } catch (e) { console.error("loadAlerts:", e); }
}

async function ackAlert(id) {
  await fetch(`/api/alerts/${id}/ack`, { method: "POST" });
  loadAlerts();
}

function alertTag(type) {
  if (type?.includes("DRY"))   return "DRY";
  if (type?.includes("FLOOD")) return "FLD";
  if (type?.includes("TEMP"))  return "TMP";
  return "ERR";
}

function showAlertBanner(d) {
  const banner = document.getElementById("alertBanner");
  if (!banner) return;
  banner.textContent   = `ALERT: ${d.node} — ${d.type}: ${d.detail}`;
  banner.style.display = "block";
  setTimeout(() => { banner.style.display = "none"; }, 6000);
}

async function checkMqttStatus() {
  try {
    const res  = await fetch("/api/mqtt/status");
    const data = await res.json();
    mqttStatus = data.connected;
    const dot  = document.getElementById("mqttDot");
    const text = document.getElementById("mqttText");
    if (dot) {
      dot.className = "mqtt-dot " + (mqttStatus ? "online" : "offline");
    }
    if (text) text.textContent = mqttStatus
      ? `Connected · ${data.broker}`
      : `Disconnected · ${data.broker}`;
    loadStats();
  } catch (e) { console.error("checkMqttStatus:", e); }
}

async function loadStats() {
  try {
    const res  = await fetch("/api/stats");
    const data = await res.json();
    setText("statReadings", data.total_readings ?? "--");
    setText("statAlerts",   data.unacked_alerts ?? "0");
  } catch (e) {}
}

async function sendSleep(seconds) {
  try {
    const res  = await fetch("/api/sleep", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: "NODE_A", seconds })
    });
    const data = await res.json();
    showToast(data.mqtt_sent ? `Node A sleeping for ${seconds}s` : "MQTT not connected");
  } catch (e) { console.error("sendSleep:", e); }
}

function refreshAll() {
  loadLatestSensors();
  loadValveState();
  loadAlerts();
  checkMqttStatus();
  loadChartHistory(chartNode);
  setTimeout(() => { if (chart) chart.resize(); }, 100);
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function flashUpdate() {
  const dot = document.querySelector(".status-dot");
  if (!dot) return;
  dot.style.background = "#D9A24C";
  setTimeout(() => { dot.style.background = "#4ade80"; }, 400);
}

let toastTimer = null;
function showToast(msg) {
  let toast = document.getElementById("toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast";
    toast.style.cssText = `
      position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
      background:#0F5A37; color:#fff; padding:10px 20px; border-radius:10px;
      font-size:13px; font-weight:600; z-index:9999;
      box-shadow:0 4px 16px rgba(15,90,55,0.3);
      font-family:'DM Sans',sans-serif; transition:opacity 0.3s;
      white-space:nowrap; max-width:90vw; overflow:hidden; text-overflow:ellipsis;
    `;
    document.body.appendChild(toast);
  }
  toast.textContent   = msg;
  toast.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.style.opacity = "0"; }, 3500);
}