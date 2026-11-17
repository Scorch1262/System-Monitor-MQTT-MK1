/* System-Monitor-MQTT-MK1 – Oberfläche */
"use strict";
const $ = (id) => document.getElementById(id);
let META = null, CONFIG = null, STATE = null, AUTOSTART = null, UPDATE = null, START_VERSION = null, fanMax = 2500;

/* ---------------------------------------------------------- Helfer */
function num(v, d = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  return Number(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmt(v, unit, d = 1) { const n = num(v, d); return n === null ? "–" : unit ? `${n} ${unit}` : n; }
function tempClass(v) { return v == null ? "" : v >= 85 ? "crit" : v >= 70 ? "hot" : ""; }
function setV(id, text, cls = "") { const el = $(id); el.textContent = text; el.className = "v " + cls; }
function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function slug(t) {
  t = String(t).trim().toLowerCase().replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss").replace(/#/g, "");
  t = t.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return t || "x";
}
function duration(s) {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} T ${h} h ${m} min` : h ? `${h} h ${m} min` : `${m} min`;
}
function rate(kb) {
  if (kb == null) return "–";
  if (kb >= 1024) return fmt(kb / 1024, "MB/s");
  return fmt(kb, "kB/s");
}
function toast(text, ms = 3500) {
  const t = $("toast"); t.textContent = text; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok && j && j.error) throw new Error(j.error);
  return j;
}

/* ---------------------------------------------------------- Anzeigeelemente */
const ARC = 270, R = 42, CIRC = 2 * Math.PI * R;
function gauge(id, pct, unit = "%") {
  const el = $(id);
  if (!el.firstChild) {
    el.innerHTML = `<svg viewBox="0 0 100 100"><circle class="track" cx="50" cy="50" r="${R}" stroke-dasharray="${CIRC * ARC / 360} ${CIRC}"/>` +
      `<circle class="bar" cx="50" cy="50" r="${R}" stroke-dasharray="0 ${CIRC}"/></svg>` +
      `<div class="val"><span class="num">–</span><span class="unit">${unit}</span></div>`;
  }
  const bar = el.querySelector(".bar");
  const p = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  bar.setAttribute("stroke-dasharray", `${CIRC * ARC / 360 * p / 100} ${CIRC}`);
  bar.style.stroke = p >= 90 ? "var(--red)" : p >= 70 ? "var(--amber)" : "var(--teal)";
  el.querySelector(".num").textContent = pct == null ? "–" : Math.round(pct);
}

function spark(id, series) {
  const c = $(id), dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w) return;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  g.strokeStyle = "#1c232a"; g.lineWidth = 1;
  for (let i = 1; i < 4; i++) { const y = Math.round(h * i / 4) + .5; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
  for (const s of series) {
    const data = s.data || [];
    if (data.length < 2) continue;
    const max = s.max || Math.max(1, ...data.filter((v) => v != null)) * 1.15;
    const step = w / (120 - 1), off = 120 - data.length;
    g.beginPath(); let started = false;
    data.forEach((v, i) => {
      if (v == null) { started = false; return; }
      const x = (off + i) * step, y = h - 2 - (Math.min(v, max) / max) * (h - 4);
      if (!started) { g.moveTo(x, y); started = true; } else g.lineTo(x, y);
    });
    g.strokeStyle = s.color; g.lineWidth = 1.5; g.stroke();
    if (s.fill) { g.lineTo(w, h); g.lineTo(off * step, h); g.closePath(); g.fillStyle = s.fill; g.fill(); }
  }
}

function barRow(name, value, pct, cls = "") {
  const p = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  return `<div class="row"><span class="n" title="${esc(name)}">${esc(name)}</span>` +
    `<span class="b"><i class="${cls}" style="width:${p}%"></i></span><span class="x">${value}</span></div>`;
}

/* ---------------------------------------------------------- Übersicht */
function renderState(st) {
  STATE = st;
  const s = st.snapshot || {}, sens = st.sensors || {}, mq = st.mqtt || {};
  if (!START_VERSION) START_VERSION = st.version;
  if (st.version !== START_VERSION) { location.reload(); return; }
  renderUpdateHint(st);
  $("host").textContent = (sens.hostname || "–") + (sens.windows ? "" : " · " + (sens.platform || "Linux"));

  // Pillen
  const pm = $("pill-mqtt");
  pm.className = "pill " + (mq.state === "verbunden" ? "ok" : mq.state === "pausiert" || mq.state === "verbinde" ? "warn" : mq.state === "aus" ? "" : "err");
  pm.querySelector("span").textContent = "MQTT · " + (mq.state || "–").toUpperCase();
  const ph = $("pill-hw");
  if (sens.windows) {
    ph.className = "pill " + (sens.lhm_ok ? (sens.admin && sens.pawnio ? "ok" : "warn") : sens.nvml ? "warn" : "err");
    ph.querySelector("span").textContent = sens.lhm_ok ? (sens.admin && sens.pawnio ? "SENSOREN · VOLL" : "SENSOREN · TEILWEISE") : sens.nvml ? "SENSOREN · NVML" : "SENSOREN · BASIS";
    ph.title = "LibreHardwareMonitor: " + sens.lhm + (sens.driver ? " · PawnIO: " + sens.driver : "");
  } else {
    ph.className = "pill " + (st.raw_count ? "ok" : "warn");
    ph.querySelector("span").textContent = "SENSOREN · LINUX";
    ph.title = st.raw_count + " Sensoren (hwmon/thermal)";
  }
  const pa = $("pill-admin");
  pa.className = "pill " + (sens.admin ? "ok" : "warn") + (sens.windows ? "" : " hidden");
  pa.querySelector("span").textContent = sens.admin ? "ADMIN" : "KEIN ADMIN";
  renderBanner(sens);

  if (!s.cpu) return;
  // CPU
  const cpu = s.cpu;
  $("cpu-name").textContent = cpu.name || "–";
  gauge("g-cpu", cpu.usage);
  setV("cpu-temp", fmt(cpu.temperature, "°C"), tempClass(cpu.temperature));
  setV("cpu-clock", fmt(cpu.clock, "MHz", 0));
  setV("cpu-power", fmt(cpu.power, "W"));
  setV("sys-proc", fmt(s.system.processes, "", 0));
  const cores = cpu.cores || [], ce = $("cpu-cores");
  if (ce.children.length !== cores.length) ce.innerHTML = cores.map(() => "<div><i></i></div>").join("");
  cores.forEach((v, i) => { const b = ce.children[i]; b.firstChild.style.height = (v || 0) + "%"; b.title = `Kern ${i + 1}: ${fmt(v, "%")}`; });

  // GPU
  const gpu = s.gpu || {};
  $("tile-gpu").classList.toggle("hidden", !(s.gpus && s.gpus.length));
  $("gpu-name").textContent = gpu.name || "keine GPU-Daten";
  gauge("g-gpu", gpu.usage);
  setV("gpu-temp", fmt(gpu.temperature, "°C"), tempClass(gpu.temperature));
  setV("gpu-hot", fmt(gpu.hotspot, "°C"), tempClass(gpu.hotspot));
  setV("gpu-mem", gpu.memory_used != null ? (gpu.memory_total ? `${fmt(gpu.memory_used / 1024, "")}/${fmt(gpu.memory_total / 1024, "GB")}` : fmt(gpu.memory_used / 1024, "GB")) : "–");
  setV("gpu-fan", fmt(gpu.fan_rpm, "U/min", 0));
  setV("gpu-fanp", fmt(gpu.fan_percent, "%", 0));
  setV("gpu-power", fmt(gpu.power, "W"));
  setV("gpu-clock", fmt(gpu.clock, "MHz", 0));

  // RAM
  const ram = s.ram;
  $("ram-total").textContent = fmt(ram.total, "GB") + " gesamt";
  gauge("g-ram", ram.usage);
  setV("ram-used", fmt(ram.used, "GB"));
  setV("ram-avail", fmt(ram.available, "GB"));
  setV("swap", s.swap.total ? `${fmt(s.swap.usage, "%")} (${fmt(s.swap.used, "")}/${fmt(s.swap.total, "GB")})` : "–");
  setV("uptime", duration(s.system.uptime));
  setV("battery", s.system.battery != null ? fmt(s.system.battery, "%", 0) + (s.system.battery_plugged ? " ⚡" : "") : "–");

  // Lüfter
  const fans = s.fans || [];
  fans.forEach((f) => { if (f.rpm > fanMax) fanMax = f.rpm; });
  $("fans").innerHTML = fans.length ? fans.map((f) => barRow(f.name, fmt(f.rpm, "U/min", 0), f.rpm / fanMax * 100)).join("")
    : `<div class="empty">${sens.windows ? (sens.admin ? "Keine Lüftersensoren gefunden – bei Notebooks liefert der Hersteller oft keine Drehzahl" : "Lüfter-Drehzahlen: „Alle Sensoren aktivieren“ (Adminrechte)") : "Keine Lüftersensoren gefunden (z. B. Raspberry Pi ohne Lüfter)"}</div>`;

  // Temperaturen
  const temps = [...(s.temps_board || []).map((t) => ({ ...t, g: "" })), ...(s.temps_drives || []).map((t) => ({ ...t, g: "▣ " }))];
  $("temps").innerHTML = temps.length ? temps.map((t) => {
    const c = t.value >= 85 ? "crit" : t.value >= 65 ? "warm" : "";
    return barRow(t.g + t.name, fmt(t.value, "°C"), t.value, c);
  }).join("") : `<div class="empty">Keine weiteren Temperatursensoren</div>`;

  // Laufwerke
  $("disk-io").textContent = `▲ ${fmt(s.disk_io.write, "MB/s")}  ▼ ${fmt(s.disk_io.read, "MB/s")}`;
  $("disks").innerHTML = (s.disks || []).map((d) => {
    const c = d.usage >= 95 ? "crit" : d.usage >= 85 ? "warm" : "";
    return barRow(d.name, `${fmt(d.free_gb, "GB", 0)} frei`, d.usage, c);
  }).join("") || `<div class="empty">Keine Laufwerke</div>`;

  // Netzwerk
  setV("net-up", rate(s.net.up));
  setV("net-down", rate(s.net.down));
  $("net-now").textContent = `▲ ${rate(s.net.up)}  ▼ ${rate(s.net.down)}`;

  // MQTT
  $("mq-broker").textContent = mq.broker || "kein Broker eingestellt";
  setV("mq-state", (mq.state || "–") + (mq.enabled ? "" : " (Senden aus)"), mq.connected && !mq.paused ? "" : "hot");
  setV("mq-topic", mq.base_topic || "–");
  setV("mq-int", fmt(mq.interval, "s", 0));
  setV("mq-last", mq.last_publish ? new Date(mq.last_publish * 1000).toLocaleTimeString("de-DE") : "–");
  setV("mq-count", mq.last_publish ? `${mq.last_count} (gesamt ${mq.sent_total.toLocaleString("de-DE")})` : "–");
  $("mq-err").textContent = mq.last_error || "";
  $("btn-pause").textContent = mq.paused ? "Fortsetzen" : "Pausieren";

  // Verläufe
  const h = st.history || {};
  spark("sp-cpu", [{ data: h.cpu, color: "#2dd4bf", fill: "rgba(45,212,191,.08)", max: 100 }, { data: h.cpu_temp, color: "#f5a524", max: 100 }]);
  spark("sp-gpu", [{ data: h.gpu, color: "#2dd4bf", fill: "rgba(45,212,191,.08)", max: 100 }, { data: h.gpu_temp, color: "#f5a524", max: 100 }]);
  spark("sp-ram", [{ data: h.ram, color: "#2dd4bf", fill: "rgba(45,212,191,.08)", max: 100 }]);
  const nmax = Math.max(10, ...(h.net_down || []).filter((v) => v != null), ...(h.net_up || []).filter((v) => v != null)) * 1.15;
  spark("sp-net", [{ data: h.net_down, color: "#2dd4bf", fill: "rgba(45,212,191,.08)", max: nmax }, { data: h.net_up, color: "#f5a524", max: nmax }]);
}

function renderBanner(sens) {
  const b = $("banner");
  let html = "";
  if (META && !META.can_edit) { b.classList.add("hidden"); return; }
  if (sens.windows && !sens.lhm_ok) {
    html = `<span><b>SENSOREN</b> &nbsp;LibreHardwareMonitor nicht aktiv (${esc(sens.lhm)}). Temperaturen und Lüfter fehlen ggf.; GPU-Werte über NVML, falls NVIDIA.</span>`;
  } else if (sens.windows && !sens.admin) {
    html = `<span><b>HINWEIS</b> &nbsp;Für CPU-Temperatur und Mainboard-Lüfter braucht das Programm einmalig Adminrechte – der nötige Treiber wird dabei automatisch installiert.</span>` +
      `<button class="btn primary" onclick="restartAdmin()">Alle Sensoren aktivieren</button>`;
  } else if (sens.windows && sens.pawnio === false) {
    html = `<span><b>TREIBER</b> &nbsp;PawnIO: ${esc(sens.driver || "nicht installiert")}. Details im Protokoll neben der exe.</span>`;
  } else if (sens.windows && AUTOSTART && AUTOSTART.available && AUTOSTART.mode !== "admin" && !sessionStorageGet("tipp-weg")) {
    html = `<span><b>TIPP</b> &nbsp;Alle Sensoren laufen. Damit das nach dem nächsten Windows-Start ohne Nachfrage so bleibt: Autostart mit Adminrechten einrichten.</span>` +
      `<span><button class="btn primary" onclick="autostartAdmin()">Autostart einrichten</button> <button class="btn" onclick="hideTip()">Ausblenden</button></span>`;
  }
  if (b.dataset.html !== html) { b.innerHTML = html; b.dataset.html = html; }
  b.classList.toggle("hidden", !html);
}
function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
function hideTip() { try { sessionStorage.setItem("tipp-weg", "1"); } catch (e) { /* egal */ } AUTOSTART = null; renderBanner(STATE.sensors); }
async function autostartAdmin() {
  try { AUTOSTART = await api("/api/autostart", { mode: "admin" }); toast("Autostart mit Adminrechten eingerichtet"); loadAutostart(); }
  catch (e) { toast("Autostart: " + e.message, 6000); }
  renderBanner(STATE.sensors);
}
window.hideTip = hideTip; window.autostartAdmin = autostartAdmin;

async function poll() {
  try {
    renderState(await api("/api/state"));
  } catch (e) {
    const pm = $("pill-mqtt"); pm.className = "pill err"; pm.querySelector("span").textContent = "PROGRAMM NICHT ERREICHBAR";
  }
  if (!$("view-sensors").classList.contains("hidden")) loadRaw();
}

/* ---------------------------------------------------------- Sensoren */
const UNITS = { Temperature: "°C", Load: "%", Fan: "U/min", Clock: "MHz", Power: "W", Voltage: "V", Current: "A",
  Control: "%", Level: "%", Data: "GB", SmallData: "MB", Throughput: "B/s", Frequency: "Hz", Energy: "mWh",
  Flow: "L/h", TimeSpan: "s", Noise: "dBA", Humidity: "%", Factor: "" };
async function loadRaw() {
  const raw = await api("/api/sensors").catch(() => []);
  const f = $("raw-filter").value.trim().toLowerCase();
  const base = (STATE && STATE.mqtt && STATE.mqtt.base_topic) || "";
  $("raw-count").textContent = `${raw.length} Sensoren`;
  const seen = new Set();
  const rows = raw.map((s) => {
    let t = `sensors/${slug(s.hw)}/${slug(s.type)}/${slug(s.name)}`, tt = t, n = 1;
    if (s.value != null) { while (seen.has(tt)) { n++; tt = `${t}_${n}`; } seen.add(tt); }
    return { ...s, topic: s.value != null ? `${base}/${tt}` : "" };
  }).filter((s) => !f || `${s.hw} ${s.name} ${s.type} ${s.hw_type}`.toLowerCase().includes(f));
  $("raw-body").innerHTML = rows.length ? rows.map((s) => {
    const d = ["Fan", "Clock", "SmallData"].includes(s.type) ? 0 : ["Voltage", "Current"].includes(s.type) ? 3 : 1;
    return `<tr><td>${esc(s.hw)}</td><td>${esc(s.name)}</td><td>${esc(s.type)}</td>` +
      `<td class="num">${fmt(s.value, UNITS[s.type] || "", d)}</td><td class="mono">${esc(s.topic)}</td></tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">Keine Rohsensoren (nur mit LibreHardwareMonitor unter Windows)</td></tr>`;
}

/* ---------------------------------------------------------- Einstellungen */
function fillSettings() {
  const c = CONFIG;
  $("m-host").value = c.mqtt.host; $("m-port").value = c.mqtt.port;
  $("m-user").value = c.mqtt.username; $("m-pass").value = c.mqtt.password;
  $("m-cid").value = c.mqtt.client_id; $("m-tls").checked = c.mqtt.tls; $("m-insecure").checked = c.mqtt.tls_insecure;
  $("m-qos").value = String(c.mqtt.qos); $("m-retain").checked = c.mqtt.retain;
  $("p-enabled").checked = c.publish.enabled; $("p-base").value = c.publish.base_topic; $("p-int").value = c.publish.interval;
  $("p-json").checked = c.publish.json; $("p-jsub").value = c.publish.json_subtopic; $("p-single").checked = c.publish.single;
  $("p-status").value = c.publish.status_topic;
  $("w-port").value = c.web.port; $("w-lan").checked = c.web.lan; $("w-remote").checked = !!c.web.remote_admin; $("s-open").checked = c.start.open_browser;
  $("w-driver").checked = !c.windows || c.windows.driver_auto !== false;
  $("u-auto").checked = !c.update || c.update.check !== false;
  updateBaseResult();

  // Messwerte
  const groups = {};
  META.metrics.forEach((m) => (groups[m.group] = groups[m.group] || []).push(m));
  $("metric-list").innerHTML = Object.entries(groups).map(([g, ms]) =>
    `<div class="metric-group"><h4>${esc(g)}</h4><div class="metric-grid">` +
    ms.map((m) => `<label class="check" title="${esc(m.key.replace(".", "/"))}"><input type="checkbox" data-metric="${m.key}" ${c.metrics[m.key] ? "checked" : ""}>` +
      `${esc(m.label)} <small>${esc(m.unit)}</small></label>`).join("") + `</div></div>`).join("");
  $("dyn-list").innerHTML = META.dynamic.map((d) =>
    `<label class="check" title="${esc(d.topic)}"><input type="checkbox" data-dyn="${d.key}" ${c.dynamic[d.key] ? "checked" : ""}>${esc(d.label)}</label>`).join("");
  const gs = ["auto", ...META.gpus];
  $("gpu-sel").innerHTML = gs.map((g) => `<option value="${esc(g)}" ${c.gpu_select === g ? "selected" : ""}>${g === "auto" ? "automatisch (dedizierte zuerst)" : esc(g)}</option>`).join("");
  if (c.gpu_select !== "auto" && !META.gpus.includes(c.gpu_select)) $("gpu-sel").insertAdjacentHTML("beforeend", `<option selected value="${esc(c.gpu_select)}">${esc(c.gpu_select)} (nicht gefunden)</option>`);
  $("cfg-path").textContent = META.config_path;
}

function collectSettings() {
  const metrics = {}, dynamic = {};
  document.querySelectorAll("[data-metric]").forEach((el) => (metrics[el.dataset.metric] = el.checked));
  document.querySelectorAll("[data-dyn]").forEach((el) => (dynamic[el.dataset.dyn] = el.checked));
  return {
    mqtt: { host: $("m-host").value.trim(), port: +$("m-port").value || 1883, username: $("m-user").value, password: $("m-pass").value,
      client_id: $("m-cid").value.trim(), tls: $("m-tls").checked, tls_insecure: $("m-insecure").checked,
      qos: +$("m-qos").value, retain: $("m-retain").checked },
    publish: { enabled: $("p-enabled").checked, base_topic: $("p-base").value.trim(), interval: +$("p-int").value || 5,
      json: $("p-json").checked, json_subtopic: $("p-jsub").value.trim(), single: $("p-single").checked, status_topic: $("p-status").value.trim() },
    metrics, dynamic, gpu_select: $("gpu-sel").value,
    web: { port: +$("w-port").value || 8095, lan: $("w-lan").checked, remote_admin: $("w-remote").checked },
    start: { open_browser: $("s-open").checked },
    windows: { driver_auto: $("w-driver").checked },
    update: { check: $("u-auto").checked },
  };
}

function updateBaseResult() {
  const host = (STATE && STATE.sensors && STATE.sensors.hostname) || "PC";
  $("p-base-res").textContent = ($("p-base").value.trim() || "pc/{hostname}").replace("{hostname}", host.replace(/ /g, "_")).replace(/^\/+|\/+$/g, "");
}

async function save() {
  try {
    const r = await api("/api/config", collectSettings());
    CONFIG = r.config;
    if (r.web_restart) {
      toast("Gespeichert – Webseite wechselt auf Port " + r.port + " …");
      const host = location.hostname;
      setTimeout(() => (location.href = `${location.protocol}//${host}:${r.port}/#settings`), 1800);
      return;
    }
    toast("Einstellungen gespeichert");
    loadPreview();
  } catch (e) { toast("Fehler: " + e.message); }
}

async function loadPreview() {
  const p = await api("/api/preview").catch(() => null);
  if (!p) return;
  $("preview-info").textContent = `${p.total} Nachrichten je Versand`;
  $("preview").innerHTML = p.messages.length ? p.messages.map(([t, v]) =>
    `<b>${esc(t)}</b>  ${esc(v.length > 160 ? v.slice(0, 160) + " …" : v)}`).join("\n") +
    (p.total > p.messages.length ? `\n… (${p.total - p.messages.length} weitere)` : "")
    : "Keine Nachrichten – JSON und Einzelwerte deaktiviert oder noch keine Messwerte.";
}

async function loadAutostart() {
  const a = await api("/api/autostart").catch(() => ({ available: false }));
  AUTOSTART = a;
  $("a-linux").textContent = a.linux || "";
  const sel = $("a-mode");
  sel.disabled = !a.available;
  sel.value = a.mode || "off";
  sel.dataset.current = sel.value;
  $("a-hint").textContent = !a.available ? "Nur unter Windows verfügbar" :
    a.admin ? "Adminrechte vorhanden – alle Modi möglich" : "Für „Adminrechte“ zuerst als Administrator neu starten";
  const probs = a.problems || [];
  $("a-detail").innerHTML = (a.info ? `<span class="msg">${esc(a.info)}</span>` : "") +
    probs.map((p) => `<br><span class="msg err">⚠ ${esc(p)}</span>`).join("") +
    (a.mode !== "off" && !probs.length && a.available ? `<br><span class="msg ok">Eintrag in Ordnung</span>` : "");
  $("btn-areset").classList.toggle("hidden", !a.available || a.mode === "off");
}

async function setAutostart() {
  const sel = $("a-mode");
  try {
    const r = await api("/api/autostart", { mode: sel.value });
    sel.dataset.current = r.mode;
    toast("Autostart: " + sel.options[sel.selectedIndex].text + " – eingerichtet");
    loadAutostart();
  } catch (e) {
    toast("Autostart: " + e.message, 6000);
    sel.value = sel.dataset.current || "off";
  }
}

async function restartAdmin() {
  const r = await api("/api/restart_admin", {}).catch(() => ({ ok: false, message: "Fehler" }));
  toast(r.message);
  if (r.ok) setTimeout(() => location.reload(), 6000);
}
window.restartAdmin = restartAdmin;

/* ---------------------------------------------------------- Netzwerk */
function renderUrls() {
  const u = (META && META.urls) || [];
  $("w-urls").innerHTML = u.length ? u.map((x) => `<a href="${esc(x)}">${esc(x)}</a>`).join("<br>") : "nur auf diesem PC (127.0.0.1)";
}

async function loadFirewall() {
  if (!META || !META.windows) return;
  const f = await api("/api/firewall").catch(() => null);
  if (!f || !f.available) return;
  const st = $("fw-state"), b = $("btn-fw");
  if (!f.lan) { st.className = "msg"; st.textContent = "nicht nötig (Webseite nur auf diesem PC)"; b.classList.add("hidden"); return; }
  if (f.port_ok) { st.className = "msg ok"; st.textContent = "Eingangsregel aktiv (lokales Netz)"; b.classList.add("hidden"); return; }
  st.className = "msg err";
  st.textContent = f.exists ? "Regel mit falschem Port" : "keine Regel – andere Geräte werden ggf. blockiert";
  b.textContent = f.admin ? "Firewall freigeben" : "Firewall freigeben (Adminrechte)";
  b.classList.remove("hidden");
}

function renderReadonlyHint() {
  if (!META || META.can_edit) return "";
  return `<span><b>NUR ANSICHT</b> &nbsp;Einstellungen können nur am PC selbst geändert werden (dort unter Programm → Bearbeiten freigeben).</span>`;
}

/* ---------------------------------------------------------- Update */
function renderUpdateHint(st) {
  const u = st.update || {}, v = $("version"), b = $("update-banner");
  v.textContent = "v" + st.version + (u.available ? "  →  " + u.latest : "");
  v.classList.toggle("new", !!u.available);
  v.onclick = u.available ? () => showView("settings") : null;
  let html = "";
  if (META && !META.can_edit) {
    html = renderReadonlyHint() + (u.available ? `<span>Update ${esc(u.latest)} verfügbar</span>` : "");
  } else if (u.busy) html = `<span><b>UPDATE</b> &nbsp;${esc(u.progress || "läuft")} … Das Programm startet danach automatisch neu.</span>`;
  else if (u.available) html = `<span><b>UPDATE</b> &nbsp;Version ${esc(u.latest)} ist verfügbar (installiert: v${esc(st.version)}).</span>` +
    `<span><button class="btn primary" onclick="installUpdate()">Jetzt aktualisieren</button> <button class="btn" onclick="showView('settings')">Details</button></span>`;
  if (b.dataset.html !== html) { b.innerHTML = html; b.dataset.html = html; }
  b.classList.toggle("hidden", !html);
}

function renderUpdate(u) {
  UPDATE = u;
  $("u-repo").textContent = u.repo || "–";
  setV("u-current", "v" + u.current);
  setV("u-latest", u.latest || "–", u.available ? "hot" : "");
  setV("u-date", u.published ? new Date(u.published).toLocaleDateString("de-DE") : "–");
  setV("u-checked", u.checked_at ? new Date(u.checked_at * 1000).toLocaleString("de-DE") : "noch nicht");
  setV("u-mode", u.mode === "exe" ? "Programmordner austauschen" : u.mode === "source" ? "Programmdateien ersetzen" : "nicht unterstützt");
  const m = $("u-msg");
  if (u.busy) { m.className = "msg"; m.textContent = "Update läuft: " + (u.progress || "…"); }
  else if (u.error) { m.className = "msg err"; m.textContent = u.error; }
  else if (u.available) { m.className = "msg ok"; m.textContent = `Neue Version ${u.latest} verfügbar.`; }
  else if (u.latest) { m.className = "msg ok"; m.textContent = "Das Programm ist auf dem neuesten Stand."; }
  else m.textContent = "";
  const n = $("u-notes");
  n.textContent = u.available && u.notes ? u.notes : "";
  n.classList.toggle("hidden", !(u.available && u.notes));
  $("btn-uinstall").disabled = !u.available || u.busy || !u.supported;
}

async function loadUpdate() { renderUpdate(await api("/api/update").catch(() => ({ current: "?" }))); }

async function installUpdate() {
  if (!confirm("Update jetzt installieren? Das Programm startet danach automatisch neu; die Einstellungen bleiben erhalten.")) return;
  const r = await api("/api/update/install", {}).catch((e) => ({ ok: false, message: e.message }));
  toast(r.message, 6000);
  if (r.ok) {
    const t = setInterval(async () => {
      try { const u = await api("/api/update"); renderUpdate(u); if (!u.busy && u.error) clearInterval(t); } catch (e) { /* Neustart läuft */ }
    }, 1500);
  }
}
window.installUpdate = installUpdate; window.showView = showView;

/* ---------------------------------------------------------- Start */
function showView(v) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("hidden", s.id !== "view-" + v));
  history.replaceState(null, "", "#" + v);
  if (v === "sensors") loadRaw();
  if (v === "settings") { loadPreview(); loadAutostart(); loadUpdate(); loadFirewall(); }
  if (v === "overview" && STATE) renderState(STATE);
}

async function init() {
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  document.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", save));
  $("p-base").addEventListener("input", updateBaseResult);
  $("raw-filter").addEventListener("input", loadRaw);
  $("a-mode").addEventListener("change", setAutostart);
  $("btn-all").onclick = () => document.querySelectorAll("[data-metric],[data-dyn]").forEach((e) => (e.checked = e.dataset.dyn !== "raw"));
  $("btn-none").onclick = () => document.querySelectorAll("[data-metric],[data-dyn]").forEach((e) => (e.checked = false));
  $("btn-preview").onclick = loadPreview;
  $("btn-ucheck").onclick = async () => { $("u-msg").className = "msg"; $("u-msg").textContent = "prüfe …"; renderUpdate(await api("/api/update/check", {})); };
  $("btn-uinstall").onclick = installUpdate;
  $("btn-areset").onclick = async () => {
    const sel = $("a-mode");
    if (sel.value === "admin" && AUTOSTART && !AUTOSTART.admin) {
      if (confirm("Die Aufgabe mit Adminrechten kann nur mit Adminrechten neu eingerichtet werden. Jetzt als Administrator neu starten?")) restartAdmin();
      return;
    }
    await setAutostart(); loadAutostart();
  };
  $("btn-fw").onclick = async () => {
    const r = await api("/api/firewall", {}).catch((e) => ({ ok: false, message: e.message }));
    toast(r.message, 6000); setTimeout(loadFirewall, 1500);
  };
  $("u-auto").onchange = save;
  $("btn-admin").onclick = restartAdmin;
  $("btn-quit").onclick = async () => {
    const restart = META.service && !META.windows;
    if (!confirm(restart ? "Dienst neu starten?" : "Programm wirklich beenden? Es werden dann keine Werte mehr gesendet.")) return;
    await api("/api/quit", {}).catch(() => {});
    if (restart) { toast("Dienst startet neu …"); setTimeout(() => location.reload(), 6000); return; }
    document.body.innerHTML = `<main><div class="banner"><span><b>BEENDET</b> &nbsp;Das Programm wurde beendet.</span></div></main>`;
  };
  $("btn-send").onclick = async () => {
    const r = await api("/api/mqtt/send", {}).catch(() => ({ ok: false }));
    toast(r.ok ? `${r.count} Nachrichten gesendet` : "Senden nicht möglich – Broker nicht verbunden");
  };
  $("btn-pause").onclick = async () => {
    await api("/api/pause", { paused: !(STATE && STATE.mqtt.paused) }); poll();
  };
  $("btn-test").onclick = async () => {
    const m = $("test-msg"); m.className = "msg"; m.textContent = "teste …";
    const s = collectSettings().mqtt;
    const r = await api("/api/mqtt/test", s).catch((e) => ({ ok: false, message: e.message }));
    m.className = "msg " + (r.ok ? "ok" : "err"); m.textContent = r.message;
  };

  [META, CONFIG] = await Promise.all([api("/api/meta"), api("/api/config")]);
  document.body.classList.toggle("linux", !META.windows);
  document.body.classList.toggle("readonly", !META.can_edit);
  if (!META.can_edit) document.querySelectorAll("#view-settings input, #view-settings select").forEach((e) => (e.disabled = true));
  renderUrls();
  $("raw-source").textContent = (META.windows ? "Quelle: LibreHardwareMonitor" : "Quelle: Linux hwmon / thermal") + " · Topics bei aktivierten „Rohsensoren“";
  if (META.service && !META.windows) $("btn-quit").textContent = "Dienst neu starten";
  loadAutostart();
  await poll();
  fillSettings();
  const v = location.hash.replace("#", "");
  showView(["overview", "sensors", "settings"].includes(v) ? v : "overview");
  setInterval(poll, 1000);
  // GPU-Liste nachladen, sobald Sensoren bereit sind
  setTimeout(async () => {
    META = await api("/api/meta"); const cur = $("gpu-sel").value;
    const gs = ["auto", ...META.gpus];
    if (!gs.includes(cur)) gs.push(cur);
    $("gpu-sel").innerHTML = gs.map((g) => `<option value="${esc(g)}">${g === "auto" ? "automatisch (dedizierte zuerst)" : esc(g)}</option>`).join("");
    $("gpu-sel").value = cur;
  }, 4000);
}
window.addEventListener("resize", () => STATE && renderState(STATE));
init();
