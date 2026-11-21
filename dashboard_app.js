/* Adalya infrastructure dashboard — no libraries, works from file:// and from the live server. */
let D = window.AUDIT_DATA;

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = "—") => (v == null || v === "" ? d : v);
const pct = (v, d = "—") => (v == null ? d : v + "%");
const ago = ts => {
  if (!ts) return "unknown";
  const s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (s < 90) return Math.round(s) + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 172800) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
};
const sev = s => ({ critical: "bad", high: "warn", medium: "warn", info: "info" }[s] || "info");
const statusBadge = s => {
  const k = s === "online" ? "b-ok" : s === "offline" ? "b-bad" : "b-info";
  return `<span class="badge ${k}">${esc(s || "unknown")}</span>`;
};

/* ---------------------------------------------------------------- charts */
function lineChart(series, opts = {}) {
  const w = opts.w || 560, h = opts.h || 150, pad = 26;
  const all = series.flatMap(s => s.points.map(p => p.v)).filter(v => v != null);
  if (!all.length) return `<div class="muted" style="padding:18px">No samples yet.</div>`;
  const min = Math.min(...all), max = Math.max(...all), span = Math.max(1, max - min);
  const n = Math.max(2, ...series.map(s => s.points.length));
  const x = i => pad + (i * (w - pad * 2)) / (n - 1);
  const y = v => h - pad - ((v - min) / span) * (h - pad * 2);
  const colors = ["#1d5f8a", "#b35c00", "#2f7d4f", "#8a1f16", "#6b4ea8"];
  const paths = series.map((s, si) => {
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    return `<path d="${d}" fill="none" stroke="${colors[si % colors.length]}" stroke-width="2"/>`;
  }).join("");
  const legend = series.map((s, si) => `<span style="color:${colors[si % colors.length]}">● ${esc(s.name)}</span>`).join(" ");
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="trend">
    <line x1="${pad}" y1="${h - pad}" x2="${w - pad}" y2="${h - pad}" stroke="#ddd8cc"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${h - pad}" stroke="#ddd8cc"/>
    <text x="${pad}" y="${pad - 6}" font-size="10" fill="#777">${max}${opts.unit || ""}</text>
    <text x="${pad}" y="${h - 8}" font-size="10" fill="#777">${min}${opts.unit || ""}</text>
    ${paths}</svg><div class="legend">${legend}</div>`;
}

function bars(items, opts = {}) {
  if (!items.length) return `<div class="muted">No data.</div>`;
  return items.map(it => {
    const v = Math.max(0, Math.min(100, it.value == null ? 0 : it.value));
    const cls = it.value == null ? "warn" : v >= 99 ? "ok" : v >= 95 ? "warn" : "bad";
    return `<div style="margin:7px 0"><div style="display:flex;justify-content:space-between;font-size:12.5px">
      <span>${esc(it.label)}</span><span class="muted">${it.value == null ? "no data" : v + "%"}</span></div>
      <div class="bar"><i class="${cls}" style="width:${v}%"></i></div></div>`;
  }).join("");
}

/* ---------------------------------------------------------------- topology */
function topologySvg() {
  const nodes = (D.topology.nodes || []).slice().sort((a, b) =>
    (a.type === "repeater" ? 0 : 1) - (b.type === "repeater" ? 0 : 1));
  const edges = D.topology.edges || [];
  const tiers = [
    ["router"], ["switch"], ["nvr"],
    ["repeater", "ap", "camera", "dahua-device", "camera?", "unknown"],
  ];
  const W = 980, perRow = 5, gapY = 118, pos = {};
  let y = 50, H = 0;
  tiers.forEach((types, ti) => {
    const row = nodes.filter(n => types.includes(n.type));
    const rows = Math.max(1, Math.ceil(row.length / perRow));
    row.forEach((n, i) => {
      const r = Math.floor(i / perRow), c = i % perRow;
      const count = Math.min(perRow, row.length - r * perRow);
      const x = (W / (count + 1)) * (c + 1);
      pos[n.id] = { x, y: y + r * gapY };
    });
    y += rows * gapY + 8;
    H = y;
  });
  const color = s => s === "online" ? "#2f7d4f" : s === "offline" ? "#a0291e" : "#8a8f98";
  const lines = edges.map(e => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return "";
    const dash = e.confidence === "confirmed" ? "" : ' stroke-dasharray="6 5"';
    return `<line x1="${a.x}" y1="${a.y + 16}" x2="${b.x}" y2="${b.y - 16}" stroke="#9aa3ad" stroke-width="1.4"${dash}/>`;
  }).join("");
  const cards = nodes.map(n => {
    const p = pos[n.id]; if (!p) return "";
    const label = n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label;
    return `<g><rect x="${p.x - 72}" y="${p.y - 16}" width="144" height="34" rx="8" fill="#fff" stroke="${color(n.status)}"/>
      <circle cx="${p.x - 60}" cy="${p.y + 1}" r="4" fill="${color(n.status)}"/>
      <text x="${p.x - 50}" y="${p.y + 1}" font-size="11" fill="#1c1f24">${esc(label)}</text>
      <text x="${p.x - 50}" y="${p.y + 15}" font-size="9.5" fill="#777">${esc(n.id)}</text></g>`;
  }).join("");
  return `<div class="topology"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="network topology">${lines}${cards}</svg>
    <div class="legend"><span><i></i>confirmed (NVR channel)</span><span><i class="dash"></i>inferred wiring</span></div></div>`;
}

/* ---------------------------------------------------------------- tabs */
function kpis() {
  const c = D.health.counts, n = D.nvr || {}, nc = n.channel_counts || {};
  return `<div class="grid kpis">
    <div class="card kpi"><b>${c.devices}</b><span>devices discovered</span><small>${c.online} online · ${c.offline} offline · ${c.unknown} unknown</small></div>
    <div class="card kpi"><b>${c.cameras}</b><span>cameras</span><small>${(D.inventory.devices.filter(d => d.role && d.role.includes("camera") && d.status === "online")).length} online</small></div>
    <div class="card kpi"><b>${num(nc.online)} / ${num(nc.total)}</b><span>NVR channels</span><small>${num(nc.offline)} offline</small></div>
    <div class="card kpi"><b>${D.health.score == null ? "—" : D.health.score}<span style="font-size:14px">${D.health.score == null ? "" : "/100"}</span></b><span>health · ${esc(D.health.grade)}</span><small>${D.health.alerts.length} findings</small></div>
    <div class="card kpi"><b>${num(n.storage ? n.storage.used_pct : null)}%</b><span>NVR disk used</span><small>${num(n.storage ? n.storage.total_gb : null)} GB total</small></div>
  </div>`;
}

function alertsHtml(limit = 8) {
  const a = D.health.alerts.slice(0, limit);
  if (!a.length) return `<div class="muted">No findings. Re-run the audit after changes.</div>`;
  return `<div class="alerts">${a.map(x => `<div class="alert ${esc(x.severity)}">
    <b>${esc(x.title)}</b> <span class="badge ${sev(x.severity)}">${esc(x.severity)}</span>
    <span class="badge b-info">${esc(x.confidence)}</span>
    <p>${esc(x.detail)}</p><div class="act"><b>Action:</b> ${esc(x.action)}</div></div>`).join("")}</div>`;
}

function overview() {
  const inv = D.inventory.devices;
  const uptimeItems = inv.filter(d => d.uptime_pct != null).map(d => ({ label: d.name || d.ip, value: d.uptime_pct }));
  const perfTargets = (D.performance.targets || []).slice(0, 6);
  const series = [];
  const seriesMap = D.performance.series || {};
  perfTargets.forEach(t => {
    const pts = (seriesMap[t.ip] || []).filter(p => p.ms != null).map(p => ({ v: p.ms }));
    if (pts.length > 2) series.push({ name: t.name || t.ip, points: pts });
  });
  return `${kpis()}
  <div class="grid two" style="margin-top:12px">
    <div class="card"><h2>Findings</h2><div class="explain">Severity and confidence come from the measurements; "suspected" means no direct measurement yet.</div>${alertsHtml(6)}</div>
    <div>
      <div class="chart"><h3>Camera uptime</h3><div class="explain">Share of successful probes in the monitoring window.</div>${bars(uptimeItems, {})}</div>
      <div class="chart" style="margin-top:12px"><h3>Latency trend (ms)</h3><div class="explain">Mean ICMP round-trip per sample, from monitor JSONL.</div>${series.length ? lineChart(series, { unit: " ms" }) : '<div class="muted">No monitoring samples in this scan.</div>'}</div>
    </div>
  </div>
  <div class="card" style="margin-top:12px"><h2>Data freshness</h2>
    <p>Generated <b>${esc(ago(D.generated_at))}</b> (${esc(D.generated_at)}). Monitoring: <b>${D.health.monitoring_active ? "samples present" : "no samples in this scan"}</b>.
    ${location.protocol === "file:" ? "Opened as a file: this is a static snapshot." : "Served live: the page reloads when a newer build appears."}</p>
    <p class="muted">Health score ${D.health.score}/100 is a weighted summary of findings, not a measurement.</p></div>`;
}

let camFilter = "all", camQuery = "";
function cameras() {
  const cams = D.inventory.devices.filter(d => (d.role || "").includes("camera") || d.nvr_online !== undefined);
  const counts = { all: cams.length, online: 0, offline: 0, unstable: 0, unknown: 0 };
  cams.forEach(d => {
    if (d.status === "online") counts.online++; else if (d.status === "offline") counts.offline++; else counts.unknown++;
    if (d.loss_pct >= 5 || (d.uptime_pct != null && d.uptime_pct < 99)) counts.unstable++;
  });
  const list = cams.filter(d => {
    if (camFilter === "online" && d.status !== "online") return false;
    if (camFilter === "offline" && d.status !== "offline") return false;
    if (camFilter === "unknown" && d.status !== "unknown") return false;
    if (camFilter === "unstable" && !(d.loss_pct >= 5 || (d.uptime_pct != null && d.uptime_pct < 99))) return false;
    if (camQuery && !((d.name || "") + " " + d.ip + " " + (d.mac || "")).toLowerCase().includes(camQuery)) return false;
    return true;
  });
  const rows = list.map(d => {
    const st = d.stream || {};
    return `<tr><td><b>${esc(d.name || d.ip)}</b><div class="muted" style="font-size:11.5px">ch ${num(d.channel)} · ${esc(d.mac_vendor || "")}</div></td>
      <td>${esc(d.ip)}</td><td style="font-family:Consolas,monospace;font-size:12px">${esc(d.mac || "—")}</td>
      <td>${statusBadge(d.status)}</td><td>${num(d.response_ms)}</td><td>${d.loss_pct == null ? "—" : d.loss_pct + "%"}</td>
      <td>${d.uptime_pct == null ? "—" : d.uptime_pct + "%"}</td>
      <td>${st.width ? st.width + "×" + st.height : "—"}</td><td>${num(st.fps)}</td><td>${num(st.bitrate_kbps)}</td>
      <td class="muted">${d.channel ? "NVR" : "—"}</td></tr>`;
  }).join("");
  return `<div class="toolbar"><div class="seg" id="camSeg">
      ${[["all", "All"], ["online", "Online"], ["offline", "Offline"], ["unstable", "Unstable"], ["unknown", "Unknown"]]
        .map(([k, l]) => `<button data-f="${k}" class="${camFilter === k ? "on" : ""}">${l} (${counts[k] || 0})</button>`).join("")}</div>
      <input type="search" id="camSearch" placeholder="Search camera, IP, MAC…" value="${esc(camQuery)}"></div>
    <table><thead><tr><th>Camera</th><th>IP</th><th>MAC</th><th>Status</th><th>ms</th><th>Loss</th><th>Uptime</th><th>Res</th><th>FPS</th><th>kbps</th><th>Source</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="11" class="muted">No cameras match this filter.</td></tr>'}</tbody></table>
    <p class="explain">Status combines the last discovery sweep, the latest performance run and the monitoring samples. Resolution/FPS/bitrate are the NVR's configured stream values, not measured traffic.</p>`;
}

function nvrTab() {
  const n = D.nvr;
  if (!n) return `<div class="card"><h2>NVR unavailable</h2><p>No NVR inspection in this scan. Run <code>python adalya_audit.py nvr --user admin</code> and rebuild.</p></div>`;
  const c = n.channel_counts || {}, s = n.storage || {}, d = n.device || {};
  const rows = (n.channels || []).map(ch => `<tr><td>${ch.channel}</td><td>${esc(ch.name || "—")}</td>
    <td>${ch.online === "1" ? '<span class="badge b-ok">online</span>' : ch.online === "0" ? '<span class="badge b-bad">offline</span>' : '<span class="badge b-info">?</span>'}</td>
    <td>${esc((ch.main || {}).codec || "—")}</td><td>${(ch.main || {}).width ? ch.main.width + "×" + ch.main.height : "—"}</td>
    <td>${num((ch.main || {}).fps)}</td><td>${num((ch.main || {}).bitrate_kbps)}</td>
    <td>${esc((ch.remote || {}).ip || "—")}</td></tr>`).join("");
  const logs = Object.entries(n.logs && n.logs.types || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">No log data.</td></tr>';
  return `<div class="grid kpis">
      <div class="card kpi"><b>${esc(d.type || "—")}</b><span>model</span><small>fw ${esc(d.firmware || "?")}</small></div>
      <div class="card kpi"><b>${num(c.online)} / ${num(c.total)}</b><span>channels online</span><small>${num(c.named)} named</small></div>
      <div class="card kpi"><b>${num(s.used_pct)}%</b><span>disk used</span><small>${num(s.total_gb)} GB · ${num(s.used_gb)} GB used</small></div>
      <div class="card kpi"><b>${num(((D.health.capacity || {}).recording || {}).estimated_retention_days)}</b><span>est. recording days</span><small>model, not measured</small></div></div>
    <div class="card" style="margin-top:12px"><h2>Storage</h2><div class="bar"><i class="${(s.used_pct || 0) >= 85 ? "bad" : "ok"}" style="width:${Math.min(100, s.used_pct || 0)}%"></i></div>
      <p class="explain">Capacity from storageDevice.cgi. SMART and CPU/RAM are not exposed by this firmware; they are reported as unavailable, not estimated.</p></div>
    <div class="card" style="margin-top:12px"><h2>Channels</h2><table><thead><tr><th>Ch</th><th>Name</th><th>Online</th><th>Codec</th><th>Res</th><th>FPS</th><th>kbps</th><th>Camera IP</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="grid two" style="margin-top:12px"><div class="card"><h3>Log types (24 h)</h3><table>${logs}</table></div>
      <div class="card"><h3>Disk states</h3><p>${esc((s.states || []).join(", ") || "unknown")}</p><p class="muted">Serial ${esc(d.serial || "—")} · ${esc(d.time || "")}</p></div></div>`;
}

function networkTab() {
  const inv = D.inventory.devices;
  const invRows = inv.map(d => `<tr><td>${esc(d.ip)}</td><td>${esc(d.name || "")}</td><td>${esc(d.role || "")}</td>
    <td>${esc(d.mac || "—")}</td><td>${esc(d.mac_vendor || "—")}</td><td>${statusBadge(d.status)}</td>
    <td>${(d.ports || []).join(" ") || "—"}</td><td>${(d.ports || []).includes("80") && (d.ports || []).includes("554") && (d.ports || []).includes("37777") ? "Dahua-like" : ""}</td></tr>`).join("");
  const perf = (D.performance.targets || []).map(t => `<tr><td>${esc(t.name || t.ip)}</td><td>${t.ip}</td>
    <td>${num(t.loss_pct)}%</td><td>${num(t.avg_ms)}</td><td>${num(t.max_ms)}</td><td>${num(t.jitter_ms)}</td>
    <td>${num((t.tcp_ms || {})["554"])}</td></tr>`).join("");
  const s = (D.discover || {}).summary || {};
  return `${topologySvg()}
    <div class="grid kpis" style="margin-top:12px">
      <div class="card kpi"><b>${num(s.alive)}</b><span>hosts answering</span></div>
      <div class="card kpi"><b>${num(s.missing)}</b><span>inventory devices missing</span></div>
      <div class="card kpi"><b>${num((D.discover || {}).wrong_mac ? D.discover.wrong_mac.length : 0)}</b><span>wrong MAC</span></div>
      <div class="card kpi"><b>${num((D.discover || {}).in_dhcp_pool ? D.discover.in_dhcp_pool.length : 0)}</b><span>devices in DHCP pool</span></div></div>
    <div class="card" style="margin-top:12px"><h2>Latency and loss</h2><table><thead><tr><th>Target</th><th>IP</th><th>Loss</th><th>Avg ms</th><th>Max ms</th><th>Jitter</th><th>TCP 554</th></tr></thead><tbody>${perf || '<tr><td colspan="7" class="muted">No performance run in this scan.</td></tr>'}</tbody></table></div>
    <div class="card" style="margin-top:12px"><h2>All devices</h2><table><thead><tr><th>IP</th><th>Name</th><th>Role</th><th>MAC</th><th>Vendor</th><th>Status</th><th>Ports</th><th></th></tr></thead><tbody>${invRows}</tbody></table></div>`;
}

function historyTab() {
  const scans = (D.history && D.history.scans) || [];
  if (scans.length < 1) return `<div class="card"><h2>No history yet</h2><p>Each build adds a scan. Run the audit again later to compare.</p></div>`;
  const opts = scans.slice().reverse().map(s => `<option value="${esc(s.scan_id)}">${esc(s.scan_id)} · ${s.score == null ? "—" : s.score}/100</option>`).join("");
  const points = scans.map(s => ({ v: s.score }));
  const bps = scans.map(s => ({ v: (s.total_kbps || 0) / 1000 }));
  const cur = scans[scans.length - 1];
  const prev = scans.length > 1 ? scans[scans.length - 2] : null;
  let cmp = "";
  if (prev) {
    const ips = new Set([...Object.keys(cur.devices || {}), ...Object.keys(prev.devices || {})]);
    cmp = Array.from(ips).sort().map(ip => {
      const a = (cur.devices || {})[ip] || {}, b = (prev.devices || {})[ip] || {};
      const du = (a.uptime != null && b.uptime != null) ? (a.uptime - b.uptime) : null;
      const dl = (a.loss != null && b.loss != null) ? (a.loss - b.loss) : null;
      return `<tr><td>${ip}</td><td>${esc(a.name || b.name || "")}</td><td>${num(b.uptime)}</td><td>${num(a.uptime)}</td>
        <td class="${du == null ? "" : du < 0 ? "bad" : "ok"}">${du == null ? "—" : (du > 0 ? "+" : "") + du.toFixed(2)}</td>
        <td>${num(b.loss)}</td><td>${num(a.loss)}</td>
        <td class="${dl == null ? "" : dl > 0 ? "bad" : "ok"}">${dl == null ? "—" : (dl > 0 ? "+" : "") + dl.toFixed(1)}</td></tr>`;
    }).join("");
  }
  const charts = scans.length > 1
    ? `<div class="grid two">
        <div class="chart"><h3>Health score over scans</h3>${lineChart([{ name: "score", points }], { unit: "" })}</div>
        <div class="chart"><h3>Configured camera bandwidth (Mbps)</h3>${lineChart([{ name: "Mbps", points: bps }], { unit: " Mbps" })}</div>
      </div>`
    : `<div class="card"><h2>Trends</h2><p class="muted">Only one scan so far. Run the audit again later; every build adds a point to these charts.</p></div>`;
  return `${charts}
    <div class="card" style="margin-top:12px"><h2>Compare scans</h2>
      <div class="toolbar"><label>Scan A <select id="hA">${opts}</select></label><label>Scan B <select id="hB">${opts}</select></label><button class="btn" id="hGo">Compare</button></div>
      <div id="hOut" class="muted">Pick two scans.</div></div>
    <div class="card" style="margin-top:12px"><h2>${prev ? "Latest vs previous" : "Latest scan"}</h2>
      ${prev ? `<p>Health ${prev.score} → ${cur.score} · devices ${prev.counts.devices} → ${cur.counts.devices} · findings ${prev.alerts} → ${cur.alerts}</p>` : ""}
      <table><thead><tr><th>IP</th><th>Name</th><th>Uptime prev</th><th>Uptime now</th><th>Δ uptime</th><th>Loss prev</th><th>Loss now</th><th>Δ loss</th></tr></thead><tbody>${cmp || '<tr><td colspan="8" class="muted">Only one scan so far.</td></tr>'}</tbody></table></div>`;
}

function reportsTab() {
  const titles = {
    "infrastructure-overview.html": ["Infrastructure overview", "Health, findings and data freshness"],
    "camera-health-report.html": ["Camera health", "Availability and per-camera detail"],
    "network-performance-report.html": ["Network performance", "Latency, loss and topology"],
    "nvr-storage-report.html": ["NVR and storage", "Recorder, disk and retention"],
    "bottleneck-analysis.html": ["Bottleneck analysis", "Ranked candidates with evidence"],
    "scalability-assessment.html": ["Scalability", "100-user capacity model"],
  };
  return `<div class="linkgrid">${D.reports.map(r => `<a class="linkcard" href="../reports/${r}">
    <b>${esc((titles[r] || [r])[0])}</b><span>${esc((titles[r] || [, ""])[1] || "")}</span></a>`).join("")}</div>`;
}

function dataTab() {
  const m = D.health.sources || {};
  const manifest = "raw-data/historical/" + D.scan_id + "/manifest.json";
  return `<div class="card"><h2>Traceability</h2>
    <table><tr><th>Item</th><th>Location</th></tr>
    <tr><td>Scan manifest (files, sizes, SHA-256)</td><td><code>${esc(manifest)}</code></td></tr>
    <tr><td>Raw discovery / NVR / perf / monitoring</td><td><code>raw-data/</code></td></tr>
    <tr><td>Processed datasets</td><td><code>processed-data/</code></td></tr>
    <tr><td>Reports</td><td><code>reports/</code></td></tr>
    <tr><td>Exports</td><td><code>exports/csv</code>, <code>exports/json</code>, <code>exports/pdf</code></td></tr>
    <tr><td>Collector input folder</td><td><code>${esc((m || {}).collector || "results/audit")}</code></td></tr></table></div>
    <div class="card" style="margin-top:12px"><h2>Exports</h2><div class="linkgrid">
      <a class="linkcard" href="../exports/csv/device-inventory.csv"><b>Inventory CSV</b><span>IP, MAC, role, status</span></a>
      <a class="linkcard" href="../exports/csv/camera-bitrates.csv"><b>Camera bitrates CSV</b><span>configured stream values</span></a>
      <a class="linkcard" href="../exports/csv/findings.csv"><b>Findings CSV</b><span>severity, confidence, action</span></a>
      <a class="linkcard" href="../exports/json/health-summary.json"><b>Health JSON</b><span>machine-readable summary</span></a>
      <a class="linkcard" href="../exports/pdf/print-report.html"><b>Print / PDF sheet</b><span>use Print → Save as PDF</span></a>
      <a class="linkcard" href="../reports/infrastructure-overview.html"><b>Full report</b><span>HTML, self-contained</span></a>
    </div></div>
    <div class="card" style="margin-top:12px"><h2>How to refresh</h2>
      <p><code>python adalya_audit.py full --user admin --monitor-min 30</code><br>
      <code>python adalya_dashboard.py build</code><br>
      <code>python adalya_dashboard.py serve --port 8090</code> for automatic refresh while it runs.</p>
      <p class="muted">Every build creates a new scan id; previous raw data and processed snapshots are kept.</p></div>`;
}

/* ---------------------------------------------------------------- router */
const VIEWS = { overview, cameras, nvr: nvrTab, network: networkTab, history: historyTab, reports: reportsTab, data: dataTab };
const startTab = new URLSearchParams(location.search).get("tab") || (location.hash || "").replace("#", "");
let current = VIEWS[startTab] ? startTab : "overview";

function render() {
  $("#scanId").textContent = D.scan_id;
  $$("#tabs button").forEach(x => x.classList.toggle("on", x.dataset.tab === current));
  $("#view").innerHTML = VIEWS[current]();
  if (current === "cameras") bindCameras();
  if (current === "history") bindHistory();
  const sc = D.health.score;
  const cls = sc == null ? "" : sc >= 85 ? "ok" : sc >= 55 ? "warn" : "bad";
  $("#freshness").innerHTML = `generated ${esc(ago(D.generated_at))} · ${D.health.monitoring_active ? "monitoring data present" : "no monitoring samples"} · <span class="${cls}">health ${sc == null ? "—" : sc + "/100"}</span>`;
}

function bindCameras() {
  $$("#camSeg button").forEach(b => b.onclick = () => { camFilter = b.dataset.f; render(); });
  const s = $("#camSearch");
  if (s) s.oninput = () => { camQuery = s.value.toLowerCase(); render(); const n = $("#camSearch"); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } };
}

function bindHistory() {
  const go = $("#hGo");
  if (!go) return;
  go.onclick = () => {
    const a = D.history.scans.find(x => x.scan_id === $("#hA").value);
    const b = D.history.scans.find(x => x.scan_id === $("#hB").value);
    if (!a || !b) return;
    const ips = new Set([...Object.keys(a.devices || {}), ...Object.keys(b.devices || {})]);
    const rows = Array.from(ips).sort().map(ip => {
      const x = (a.devices || {})[ip] || {}, y = (b.devices || {})[ip] || {};
      return `<tr><td>${ip}</td><td>${esc(y.name || x.name || "")}</td><td>${num(x.uptime)}</td><td>${num(y.uptime)}</td><td>${num(x.loss)}</td><td>${num(y.loss)}</td></tr>`;
    }).join("");
    $("#hOut").innerHTML = `<p>${esc(a.scan_id)} → ${esc(b.scan_id)} · health ${a.score} → ${b.score}</p>
      <table><tr><th>IP</th><th>Name</th><th>Uptime A</th><th>Uptime B</th><th>Loss A</th><th>Loss B</th></tr>${rows}</table>`;
  };
}

$$("#tabs button").forEach(b => b.onclick = () => {
  current = b.dataset.tab;
  $$("#tabs button").forEach(x => x.classList.toggle("on", x === b));
  history.replaceState(null, "", "?tab=" + current);
  render();
});

$("#reload").onclick = () => location.reload();

/* live mode: only when served over HTTP; file:// stays a static snapshot */
async function liveCheck() {
  if (location.protocol === "file:") {
    $("#liveDot").className = "dot"; $("#liveText").textContent = "static snapshot";
    $("#mode").textContent = "Opened from disk: no automatic refresh.";
    return;
  }
  try {
    const r = await fetch("live.json", { cache: "no-store" });
    const j = await r.json();
    if (j.scan_id !== D.scan_id) { D = j; render(); }
    const age = (Date.now() - new Date(j.generated_at).getTime()) / 1000;
    $("#liveDot").className = "dot " + (age < 600 ? "live" : "");
    $("#liveText").textContent = age < 120 ? "live" : "last build " + ago(j.generated_at);
    $("#mode").textContent = "Served by adalya_dashboard.py serve; refresh check every 30 s.";
  } catch (e) {
    $("#liveDot").className = "dot"; $("#liveText").textContent = "static";
  }
}
liveCheck();
setInterval(liveCheck, 30000);
render();
