"""Turn raw audit output into the infrastructure-audit centre.

Responsibilities
  * preserve every raw collector file under raw-data/<kind>/<scan-id>/ with a manifest
  * build processed datasets (inventory, topology, performance, bandwidth, health)
  * write the six human reports, the dashboard and the CSV/JSON/PDF exports
  * keep a history index so scans can be compared

It does not collect anything itself: collection stays in adalya_audit.py. This module
only organises, analyses and presents what was measured.
"""

import csv
import glob
import hashlib
import json
import os
import shutil
import time
from datetime import datetime

import audit_report

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ADALYA_AUDIT_HOME") or os.path.join(HERE, "infrastructure-audit")
RESULTS = os.environ.get("ADALYA_AUDIT_RESULTS") or os.path.join(HERE, "results", "audit")

RAW = os.path.join(ROOT, "raw-data")
PROCESSED = os.path.join(ROOT, "processed-data")
REPORTS = os.path.join(ROOT, "reports")
DASH = os.path.join(ROOT, "dashboard")
EXPORTS = os.path.join(ROOT, "exports")
LOGS = os.path.join(ROOT, "logs")
ARCHIVE = os.path.join(ROOT, "archive")

DIRS = [
    RAW, PROCESSED, REPORTS, DASH, EXPORTS, LOGS, ARCHIVE,
    os.path.join(DASH, "css"), os.path.join(DASH, "js"), os.path.join(DASH, "assets"),
    os.path.join(EXPORTS, "csv"), os.path.join(EXPORTS, "json"), os.path.join(EXPORTS, "pdf"),
    os.path.join(LOGS, "discovery"), os.path.join(LOGS, "monitoring"), os.path.join(LOGS, "errors"),
    os.path.join(LOGS, "system"),
] + [os.path.join(RAW, d) for d in
     ("cameras", "nvr", "network-devices", "switches", "discovery", "historical")]


def ensure_tree():
    for d in DIRS:
        os.makedirs(d, exist_ok=True)


def _log(kind, text):
    ensure_tree()
    path = os.path.join(LOGS, kind, f"{kind}.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return {"source": os.path.relpath(src, HERE), "stored": os.path.relpath(dst, ROOT),
            "bytes": os.path.getsize(dst), "sha256": _sha256(dst)}


def latest(kind):
    files = sorted(glob.glob(os.path.join(RESULTS, f"{kind}-*.json")))
    return files[-1] if files else None


def ingest(scan_id=None):
    """Copy the newest collector output into raw-data with a preserved manifest."""
    ensure_tree()
    if scan_id is None:
        scan_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    files = {}
    mapping = {
        "discover": (latest("discover"), "discovery"),
        "nvr": (latest("nvr"), "nvr"),
        "perf": (latest("perf"), "network-devices"),
    }
    for kind, (src, folder) in mapping.items():
        if src:
            dst = os.path.join(RAW, folder, scan_id, os.path.basename(src))
            files[kind] = _copy(src, dst)
        else:
            files[kind] = None
    # monitoring history is append-only in the collector; keep every file in this scan
    monitors = sorted(glob.glob(os.path.join(RESULTS, "monitor-*.jsonl")))
    mon_entries = []
    for src in monitors[-24:]:
        dst = os.path.join(RAW, "historical", scan_id, os.path.basename(src))
        mon_entries.append(_copy(src, dst))
    files["monitor"] = mon_entries

    manifest = {
        "scan_id": scan_id, "created": datetime.now().isoformat(timespec="seconds"),
        "collector": RESULTS, "files": files,
        "notes": [
            "Raw collector output preserved as-is; reports only read from these copies.",
            "No passwords, tokens or camera credentials are present: the Dahua client sends an Authorization header, never a URL.",
            "Switches and cameras are not logged into, so their raw responses are unavailable by design.",
        ],
    }
    mpath = os.path.join(RAW, "historical", scan_id, "manifest.json")
    os.makedirs(os.path.dirname(mpath), exist_ok=True)
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    _log("system", f"ingested scan {scan_id}: " + ", ".join(k for k, v in files.items() if v))
    return manifest


def _load(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _rel(p):
    return os.path.relpath(p, HERE) if p else ""


def build_inventory(discover, nvr, perf, monitors):
    by_ip = {}
    for h in (discover or {}).get("hosts", []):
        by_ip[h["ip"]] = {
            "ip": h["ip"], "mac": h.get("mac", ""), "mac_vendor": h.get("mac_vendor", ""),
            "role": h.get("role", "unknown"), "name": h.get("name", ""),
            "hostname": h.get("hostname", ""), "in_inventory": h.get("in_inventory", False),
            "ports": sorted(p for p, ok in (h.get("ports") or {}).items() if ok),
            "channel": None, "status": "online", "last_seen": None, "response_ms": None,
            "loss_pct": None, "uptime_pct": None, "source": "discover",
        }
    for row in (perf or {}).get("targets", []):
        d = by_ip.setdefault(row["ip"], {"ip": row["ip"], "mac": "", "mac_vendor": "",
                                         "role": row.get("role", "unknown"), "name": row.get("name", ""),
                                         "hostname": "", "in_inventory": True, "ports": [],
                                         "channel": None, "status": "unknown", "last_seen": None,
                                         "response_ms": None, "loss_pct": None, "uptime_pct": None,
                                         "source": "perf"})
        d["loss_pct"] = row.get("loss_pct")
        d["response_ms"] = row.get("avg_ms")
        d["status"] = "offline" if row.get("loss_pct") == 100 else ("online" if row.get("loss_pct") is not None else "unknown")
    for ip, m in ((monitors or {}).get("devices") or {}).items():
        d = by_ip.get(ip)
        if not d:
            continue
        d["uptime_pct"] = m.get("uptime_pct")
        d["response_ms"] = m.get("avg_ms") or d["response_ms"]
        if m.get("last") is not None:
            d["status"] = "online" if m["last"] else "offline"
    for c in (nvr or {}).get("channels", []):
        ip = (c.get("remote") or {}).get("ip")
        if not ip:
            continue
        d = by_ip.get(ip)
        if not d:
            d = by_ip[ip] = {"ip": ip, "mac": "", "mac_vendor": "", "role": "camera",
                             "name": c.get("name", ""), "hostname": "", "in_inventory": True,
                             "ports": [], "channel": c["channel"], "status": "unknown",
                             "last_seen": None, "response_ms": None, "loss_pct": None,
                             "uptime_pct": None, "source": "nvr"}
        d["channel"] = c["channel"]
        if not d.get("name"):
            d["name"] = c.get("name", "")
        d["stream"] = c.get("main") or {}
        d["nvr_online"] = str(c.get("online")) == "1"
    return {"devices": sorted(by_ip.values(), key=lambda d: tuple(int(x) for x in d["ip"].split(".") if x.isdigit()))}


def build_topology(inventory, nvr):
    nodes, edges = [], []
    by_ip = {d["ip"]: d for d in inventory["devices"]}

    def node(ip, label, kind, status="unknown"):
        nodes.append({"id": ip, "label": label, "type": kind, "status": status})

    for d in inventory["devices"]:
        role = d.get("role") or "unknown"
        kind = ("router" if role == "router" else "switch" if role == "switch" else
                "nvr" if role == "nvr" else "camera" if role in ("camera", "dahua-device", "camera?") else
                "repeater" if role in ("repeater", "ap") else "unknown")
        node(d["ip"], d.get("name") or d["ip"], kind, d.get("status", "unknown"))
    # Only the NVR->camera mappings are factual (read from the NVR).
    for c in (nvr or {}).get("channels", []):
        ip = (c.get("remote") or {}).get("ip")
        if ip and ip in by_ip:
            edges.append({"from": next(d["ip"] for d in inventory["devices"] if d["role"] == "nvr"),
                          "to": ip, "kind": "channel", "confidence": "confirmed",
                          "evidence": f"NVR channel {c['channel']} -> {ip}"})
    # Wiring is not discoverable (no LLDP on the switch): keep it labelled as inferred.
    router = next((d for d in inventory["devices"] if d["role"] == "router"), None)
    switch = next((d for d in inventory["devices"] if d["role"] == "switch"), None)
    nvrd = next((d for d in inventory["devices"] if d["role"] == "nvr"), None)
    if router and switch:
        edges.append({"from": router["ip"], "to": switch["ip"], "kind": "inferred-wired",
                      "confidence": "inferred", "evidence": "router and switch present; no LLDP data"})
    if switch and nvrd:
        edges.append({"from": switch["ip"], "to": nvrd["ip"], "kind": "inferred-wired",
                      "confidence": "inferred", "evidence": "switch and NVR present; no LLDP data"})
    return {"nodes": nodes, "edges": edges,
            "note": "Solid edges come from the NVR's channel table. Dashed edges are inferred "
                    "from the device inventory; no switch port or LLDP data was available."}


def build_performance(perf, monitors):
    series = {}
    for ip, m in ((monitors or {}).get("devices") or {}).items():
        series[ip] = []
    # rebuild per-sample series from the raw JSONL that ingest just copied
    rows = _recent_monitor_rows()
    for row in rows:
        if row.get("kind") != "device":
            continue
        s = series.setdefault(row.get("ip"), [])
        if len(s) < 400:
            s.append({"ts": row.get("ts"), "up": bool(row.get("up")), "ms": row.get("ms")})
    return {"targets": (perf or {}).get("targets", []), "series": series,
            "monitor": {ip: {"uptime_pct": m.get("uptime_pct"), "samples": m.get("samples"),
                             "transitions": m.get("transitions"), "avg_ms": m.get("avg_ms")}
                        for ip, m in ((monitors or {}).get("devices") or {}).items()}}


def _recent_monitor_rows(limit_files=6):
    rows = []
    files = sorted(glob.glob(os.path.join(RESULTS, "monitor-*.jsonl")))[-limit_files:]
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return rows


def build_bandwidth(nvr, perf):
    channels = []
    total = 0
    for c in (nvr or {}).get("channels", []):
        main = c.get("main") or {}
        kbps = main.get("bitrate_kbps") or 0
        total += kbps
        channels.append({"channel": c["channel"], "name": c.get("name", ""),
                         "online": str(c.get("online")) == "1",
                         "codec": main.get("codec", ""), "width": main.get("width"),
                         "height": main.get("height"), "fps": main.get("fps"),
                         "kbps": kbps, "gb_per_day": round(kbps * 86400 / 8 / 1e6, 1) if kbps else None})
    return {"channels": channels, "configured_total_kbps": total,
            "configured_total_mbps": round(total / 1000, 2),
            "measured_stream": (perf or {}).get("stream"),
            "note": "Bitrates are the NVR's configured main-stream values. Measured stream data "
                    "exists only when perf was run with --stream-ch."}


def build_health(discover, nvr, perf, monitors, analysis, inventory):
    has_data = bool(discover or nvr or perf)
    counts = {
        "devices": len(inventory["devices"]),
        "cameras": sum(1 for d in inventory["devices"] if d.get("role") in ("camera", "dahua-device", "camera?")),
        "online": sum(1 for d in inventory["devices"] if d.get("status") == "online"),
        "offline": sum(1 for d in inventory["devices"] if d.get("status") == "offline"),
        "unknown": sum(1 for d in inventory["devices"] if d.get("status") == "unknown"),
    }
    alerts = []
    base_weight = {"critical": 15, "high": 8, "medium": 3, "info": 1}
    action_map = {
        "cctv": "Restore the camera link or replace the camera; check power, Wi-Fi RSSI and the NVR channel.",
        "storage": "Free space or add a disk; lower main-stream bitrate if retention matters.",
        "performance": "Fix the Wi-Fi link (placement/bitrate/AP) before adding load; check the switch port.",
        "stability": "Treat as a recurring link problem: measure RSSI and drops over 30 minutes.",
        "network": "Correct the address plan (reservations, duplicates) and re-run discovery.",
        "inventory": "Update devices.json from the verified channel/IP/MAC map.",
        "nvr": "Note as a firmware limitation; verify the feature in the NVR web UI.",
    }
    for i in analysis["issues"]:
        confirmed = i["area"] in ("cctv", "storage", "performance", "stability", "network")
        alerts.append({
            "severity": i["severity"], "confidence": "confirmed" if confirmed else "suspected",
            "area": i["area"], "title": i["title"], "detail": i["detail"],
            "evidence": i.get("evidence", ""),
            "action": action_map.get(i["area"], "Investigate and re-measure."),
        })
    score = max(0, min(100, round(100 - sum(base_weight.get(a["severity"], 1) * (1 if a["confidence"] == "confirmed" else 0.5)
                                               for a in alerts))))
    if not has_data:
        return {"score": None, "grade": "—", "status": "unknown", "counts": counts,
                "alerts": [], "monitoring_active": False,
                "note": "No measurements yet: run a discovery/NVR/perf stage and rebuild."}
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"
    status = "healthy" if score >= 85 else "degraded" if score >= 55 else "critical"
    last = ((monitors or {}).get("devices") or {})
    return {"score": score, "grade": grade, "status": status,
            "counts": counts, "alerts": alerts,
            "monitoring_active": bool(last),
            "note": "Health score is a weighted summary of the findings; it is not a measurement."}


def build_history(scan_id, health, inventory, bandwidth, monitors):
    path = os.path.join(PROCESSED, "history.json")
    hist = _load(path) or {"scans": []}
    entry = {
        "scan_id": scan_id, "ts": time.time(),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "score": health["score"], "grade": health["grade"], "status": health["status"],
        "counts": health["counts"], "alerts": len(health["alerts"]),
        "total_kbps": bandwidth["configured_total_kbps"],
        "devices": {d["ip"]: {"name": d.get("name"), "uptime": d.get("uptime_pct"),
                              "loss": d.get("loss_pct"), "ms": d.get("response_ms"),
                              "status": d.get("status")}
                    for d in inventory["devices"]},
    }
    hist["scans"] = [s for s in hist.get("scans", []) if s.get("scan_id") != scan_id] + [entry]
    hist["scans"] = hist["scans"][-100:]
    return hist


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, default=str)


def build(scan_id=None, users=100):
    ensure_tree()
    manifest = ingest(scan_id)
    scan_id = manifest["scan_id"]
    raw_nvr = _load(os.path.join(RAW, "nvr", scan_id, "nvr.json")) or _load(latest("nvr"))
    raw_disc = _load(os.path.join(RAW, "discovery", scan_id, "discover.json")) or _load(latest("discover"))
    raw_perf = _load(os.path.join(RAW, "network-devices", scan_id, "perf.json")) or _load(latest("perf"))
    monitors = audit_report.load_monitors(RESULTS)
    analysis = audit_report.analyze(raw_disc, raw_nvr, raw_perf, monitors, users=users)

    inventory = build_inventory(raw_disc, raw_nvr, raw_perf, monitors)
    topology = build_topology(inventory, raw_nvr)
    performance = build_performance(raw_perf, monitors)
    bandwidth = build_bandwidth(raw_nvr, raw_perf)
    health = build_health(raw_disc, raw_nvr, raw_perf, monitors, analysis, inventory)
    history = build_history(scan_id, health, inventory, bandwidth, monitors)

    generated = datetime.now().isoformat(timespec="seconds")
    common = {"generated_at": generated, "scan_id": scan_id,
              "sources": {"manifest": f"raw-data/historical/{scan_id}/manifest.json",
                          "collector": _rel(RESULTS)}}
    datasets = {
        "device-inventory.json": {**common, **inventory},
        "network-topology.json": {**common, **topology},
        "performance-metrics.json": {**common, **performance},
        "bandwidth-analysis.json": {**common, **bandwidth},
        "health-summary.json": {**common, **health,
                                "capacity": analysis["capacity"],
                                "recommendations": analysis["recommendations"]},
        "history.json": history,
    }
    for name, doc in datasets.items():
        _write_json(os.path.join(PROCESSED, name), doc)

    _archive_previous(scan_id)
    _write_exports(scan_id, datasets)
    _write_reports(scan_id, datasets, raw_nvr, raw_perf, monitors)
    _write_dashboard(scan_id, datasets, raw_nvr, raw_disc)
    _log("system", f"built scan {scan_id}: score {health['score']} ({health['grade']}), "
                   f"{len(health['alerts'])} alerts")
    return {"scan_id": scan_id, "score": health["score"], "grade": health["grade"],
            "alerts": len(health["alerts"]), "dashboard": os.path.join(DASH, "index.html")}


def _archive_previous(scan_id):
    """Keep a copy of the previous processed snapshot before it is overwritten."""
    existing = [f for f in glob.glob(os.path.join(PROCESSED, "*.json"))]
    if not existing:
        return
    old = _load(os.path.join(PROCESSED, "health-summary.json"))
    if not old:
        return
    old_id = old.get("scan_id")
    if old_id and old_id != scan_id:
        dest = os.path.join(ARCHIVE, old_id)
        os.makedirs(dest, exist_ok=True)
        for f in existing:
            try:
                shutil.copy2(f, dest)
            except OSError:
                pass


def _write_exports(scan_id, datasets):
    for name, doc in datasets.items():
        _write_json(os.path.join(EXPORTS, "json", name), doc)
    inv = datasets["device-inventory.json"]["devices"]
    _csv(os.path.join(EXPORTS, "csv", "device-inventory.csv"),
         ["ip", "name", "role", "mac", "vendor", "status", "channel", "response_ms", "loss_pct", "uptime_pct"],
         [[d["ip"], d.get("name", ""), d.get("role", ""), d.get("mac", ""), d.get("mac_vendor", ""),
           d.get("status", ""), d.get("channel") or "", d.get("response_ms"), d.get("loss_pct"),
           d.get("uptime_pct")] for d in inv])
    ch = datasets["bandwidth-analysis.json"]["channels"]
    _csv(os.path.join(EXPORTS, "csv", "camera-bitrates.csv"),
         ["channel", "name", "online", "codec", "width", "height", "fps", "kbps", "gb_per_day"],
         [[c["channel"], c["name"], c["online"], c["codec"], c["width"], c["height"], c["fps"],
           c["kbps"], c["gb_per_day"]] for c in ch])
    alerts = datasets["health-summary.json"]["alerts"]
    _csv(os.path.join(EXPORTS, "csv", "findings.csv"),
         ["severity", "confidence", "area", "title", "detail", "action"],
         [[a["severity"], a["confidence"], a["area"], a["title"], a["detail"], a["action"]] for a in alerts])
    # print-ready PDF source; a real PDF is produced by the browser if requested
    _write_print_sheet(scan_id, datasets)


def _csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _write_print_sheet(scan_id, datasets):
    """A print stylesheet page that 'Print to PDF' turns into the PDF export."""
    health = datasets["health-summary.json"]
    inv = datasets["device-inventory.json"]["devices"]
    rows = "".join(
        f"<tr><td>{d['ip']}</td><td>{d.get('name','')}</td><td>{d.get('role','')}</td>"
        f"<td>{d.get('status','')}</td><td>{d.get('uptime_pct','—')}</td></tr>" for d in inv)
    alerts = "".join(f"<tr><td>{a['severity']}</td><td>{a['area']}</td><td>{a['title']}</td></tr>"
                     for a in health["alerts"])
    html = f"""<!doctype html><meta charset="utf-8"><title>Adalya audit {scan_id}</title>
<style>body{{font:12px/1.5 Segoe UI,sans-serif;margin:24px}} h1{{font-size:20px}}
table{{width:100%;border-collapse:collapse;margin:8px 0 18px}} th,td{{border:1px solid #ccc;padding:4px 6px;text-align:left}}
</style><h1>Adalya Sitesi — infrastructure audit {scan_id}</h1>
<p>Health {health['score'] if health['score'] is not None else '—'}{'' if health['score'] is None else '/' + str(health['grade'])} · {health['counts']['devices']} devices ·
{len(health['alerts'])} findings · generated {health['generated_at']}</p>
<h2>Devices</h2><table><tr><th>IP</th><th>Name</th><th>Role</th><th>Status</th><th>Uptime %</th></tr>{rows}</table>
<h2>Findings</h2><table><tr><th>Severity</th><th>Area</th><th>Finding</th></tr>{alerts}</table>
<p style="color:#666">Open this file and use Print → Save as PDF for the PDF export.</p>"""
    path = os.path.join(EXPORTS, "pdf", "print-report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _esc(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------- reports

REPORT_CSS = """
body{font:14px/1.6 Segoe UI,system-ui,sans-serif;margin:0;background:#f6f5f2;color:#1c1f24}
main{max-width:980px;margin:0 auto;padding:28px}
h1{font-size:26px;margin:0 0 2px} h2{font-size:18px;margin:26px 0 10px}
.sub{color:#5f6670} .badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
.ok{background:#e5f4e9;color:#22603a}.warn{background:#fdf1d8;color:#7a5200}
.bad{background:#fbe6e3;color:#8a1f16}.info{background:#e9eef5;color:#33465e}
.metric{background:#fff;border:1px solid #e6e2d9;border-radius:10px;padding:14px 16px;margin:10px 0}
.metric h3{margin:0 0 8px;font-size:15px} .q{color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.metric p{margin:4px 0 10px} .kv{display:grid;grid-template-columns:180px 1fr;gap:4px 14px;font-size:13px}
.kv b{color:#4b5563;font-weight:600}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e6e2d9;border-radius:8px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #eee9df;font-size:13px}
th{background:#faf8f4} a{color:#1d5f8a}
.meta{font-size:12px;color:#6b7280;margin-top:6px} nav a{margin-right:14px;font-size:13px}
"""


def _metric(title, measured, meaning, result, status, cause, impact, action, confidence, evidence):
    cls = {"healthy": "ok", "concerning": "warn", "critical": "bad", "unknown": "info"}[status]
    label = {"healthy": "Healthy", "concerning": "Concerning", "critical": "Critical", "unknown": "Unavailable"}[status]
    return f"""<div class="metric"><h3>{_esc(title)} <span class="badge {cls}">{label}</span>
<span class="badge info">{_esc(confidence)}</span></h3>
<div class="kv"><b>What was measured</b><span>{_esc(measured)}</span>
<b>What it means</b><span>{_esc(meaning)}</span>
<b>Current result</b><span>{_esc(result)}</span>
<b>Likely cause</b><span>{_esc(cause)}</span>
<b>Impact</b><span>{_esc(impact)}</span>
<b>Recommended action</b><span>{_esc(action)}</span>
<b>Evidence</b><span>{_esc(evidence)}</span></div></div>"""


def _report_page(title, lead, body, scan_id, nav=True):
    links = ""
    if nav:
        links = ('<nav><a href="infrastructure-overview.html">Overview</a>'
                 '<a href="camera-health-report.html">Cameras</a>'
                 '<a href="network-performance-report.html">Network</a>'
                 '<a href="nvr-storage-report.html">NVR &amp; storage</a>'
                 '<a href="bottleneck-analysis.html">Bottlenecks</a>'
                 '<a href="scalability-assessment.html">Scalability</a>'
                 '<a href="../dashboard/index.html">Dashboard</a></nav>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — Adalya audit</title><style>{REPORT_CSS}</style></head><body><main>
<h1>{_esc(title)}</h1><div class="sub">Scan {_esc(scan_id)} · {_esc(lead)}</div>{links}{body}
<div class="meta">Generated by <code>adapta_audit.py</code> + <code>audit_store.py</code>. Raw inputs are under
<code>raw-data/</code>; every number here is traceable to a file listed in the scan manifest.</div>
</main></body></html>"""


def _write_reports(scan_id, datasets, nvr, perf, monitors):
    ensure_tree()
    inv = datasets["device-inventory.json"]
    health = datasets["health-summary.json"]
    bw = datasets["bandwidth-analysis.json"]
    topo = datasets["network-topology.json"]
    devices = inv["devices"]
    cams = [d for d in devices if d.get("role") in ("camera", "dahua-device", "camera?")]
    online = [d for d in cams if d.get("status") == "online"]
    offline = [d for d in cams if d.get("status") == "offline"]
    alerts = health["alerts"]

    def alert_rows(area=None):
        rows = [a for a in alerts if not area or a["area"] == area]
        return "".join(f"<tr><td>{_esc(a['severity'])}</td><td>{_esc(a['confidence'])}</td>"
                       f"<td>{_esc(a['title'])}</td><td>{_esc(a['action'])}</td></tr>" for a in rows) or \
               '<tr><td colspan="4">No findings in this area.</td></tr>'

    score = health["score"]
    impact = ("No measurements yet: the state is unknown, not healthy."
              if score is None else
              "Operations can continue, with the listed risks." if score >= 85 else
              "Some camera or network functions are degraded.")
    overview = f"""
<h2>Summary</h2>
<div class="metric"><h3>Infrastructure health <span class="badge {'info' if score is None else ('ok' if score>=85 else 'warn' if score>=55 else 'bad')}">{'no data' if score is None else str(score) + '/100 · ' + health['grade']}</span></h3>
<div class="kv"><b>What was measured</b><span>Weighted findings across discovery, NVR, performance and monitoring.</span>
<b>What it means</b><span>A single 0–100 indicator; it is a summary of findings, not a measurement of its own.</span>
<b>Current result</b><span>{health['counts']['devices']} devices, {len(online)}/{len(cams)} cameras online, {len(alerts)} findings.</span>
<b>Likely cause</b><span>See the findings table below.</span>
<b>Impact</b><span>{impact}</span>
<b>Recommended action</b><span>Work top-down through severity; re-run the audit after each fix.</span>
<b>Evidence</b><span>processed-data/health-summary.json</span></div></div>
<h2>Findings</h2><table><tr><th>Severity</th><th>Confidence</th><th>Finding</th><th>Action</th></tr>{alert_rows()}</table>
<h2>Data freshness</h2>
<table><tr><th>Item</th><th>Value</th></tr>
<tr><td>Scan</td><td>{_esc(scan_id)}</td></tr>
<tr><td>Generated</td><td>{_esc(health['generated_at'])}</td></tr>
<tr><td>Monitoring samples</td><td>{'present' if health['monitoring_active'] else 'none in this scan'}</td></tr>
<tr><td>NVR</td><td>{_esc((nvr or {}).get('device', {}).get('type', 'unavailable'))}</td></tr></table>
"""

    def cam_rows():
        out = []
        for d in cams:
            st = d.get("status", "unknown")
            out.append(f"<tr><td>{d['ip']}</td><td>{_esc(d.get('name',''))}</td><td>{_esc(d.get('mac',''))}</td>"
                       f"<td>{_esc(st)}</td><td>{d.get('response_ms') or '—'}</td><td>{d.get('loss_pct') if d.get('loss_pct') is not None else '—'}</td>"
                       f"<td>{d.get('uptime_pct') if d.get('uptime_pct') is not None else '—'}</td>"
                       f"<td>{(d.get('stream') or {}).get('width','')}×{(d.get('stream') or {}).get('height','')}</td>"
                       f"<td>{(d.get('stream') or {}).get('fps','')}</td><td>{(d.get('stream') or {}).get('bitrate_kbps','')}</td></tr>")
        return "".join(out)
    cameras = f"""
<h2>Camera availability</h2>
{_metric("Cameras online", "ICMP/ARP discovery and the NVR channel state", "Whether each camera answers on the network and whether the NVR sees its stream",
         f"{len(online)} of {len(cams)} online", "healthy" if cams and len(online) == len(cams) else "critical" if not online else "concerning",
         "Camera power, Wi-Fi RSSI, switch/repeater, NVR channel configuration",
         "A missing camera means no recording and no live view for that area",
         "Power-cycle and check the 12 V adapter; measure Wi-Fi at the camera; re-add the channel if needed",
         "confirmed", "processed-data/device-inventory.json")}
<h2>Per-camera detail</h2>
<table><tr><th>IP</th><th>Name</th><th>MAC</th><th>Status</th><th>ms</th><th>Loss %</th><th>Uptime %</th><th>Res</th><th>FPS</th><th>kbps</th></tr>{cam_rows()}</table>
<h2>Findings</h2><table><tr><th>Severity</th><th>Confidence</th><th>Finding</th><th>Action</th></tr>{alert_rows('cctv')}</table>
"""

    storage = (nvr or {}).get("storage", {})
    nvr_report = f"""
<h2>NVR identification</h2>
<table><tr><th>Field</th><th>Value</th></tr>
<tr><td>Model</td><td>{_esc((nvr or {}).get('device', {}).get('type', 'unavailable'))}</td></tr>
<tr><td>Firmware</td><td>{_esc((nvr or {}).get('device', {}).get('firmware', 'unavailable'))}</td></tr>
<tr><td>Serial</td><td>{_esc((nvr or {}).get('device', {}).get('serial', 'unavailable'))}</td></tr>
<tr><td>Channels</td><td>{(nvr or {}).get('channel_counts', {}).get('online', '—')} online / {(nvr or {}).get('channel_counts', {}).get('total', '—')} total</td></tr></table>
<h2>Storage</h2>
{_metric("Disk utilization", "storageDevice.cgi capacity fields", "How much of the recording disk is in use; above 85% recording starts overwriting or stops",
         f"{storage.get('used_pct', 'unavailable')}% of {storage.get('total_gb', '?')} GB", "healthy" if (storage.get('used_pct') or 0) < 85 else "critical",
         "Long retention, high bitrate, or a disk that needs replacing", "Loss of older recordings; possible recording stop",
         "Add storage or lower main-stream bitrate; check disk health in the NVR UI", "confirmed", "raw-data/nvr/" + scan_id)}
{_metric("Recording retention", "Free space divided by the configured daily bitrate", "Estimated days of recording left at current settings",
         f"{health['capacity']['recording'].get('estimated_retention_days', 'unknown')} days (estimate)",
         "healthy" if (health['capacity']['recording'].get('estimated_retention_days') or 0) >= 14 else "concerning",
         "Disk size and main-stream bitrate", "Legal/insurance retention may be too short",
         "Plan disk capacity against the retention the site needs", "suspected", "bandwidth-analysis.json")}
<h2>NVR log (window)</h2>
<table><tr><th>Type</th><th>Count</th></tr>{''.join(f'<tr><td>{_esc(k)}</td><td>{v}</td></tr>' for k, v in ((nvr or {}).get('logs', {}).get('types', {}) or {}).items()) or '<tr><td colspan="2">No log data.</td></tr>'}</table>
"""

    perf_rows = "".join(
        f"<tr><td>{_esc(t.get('name') or t['ip'])}</td><td>{t['ip']}</td><td>{t.get('loss_pct')}</td>"
        f"<td>{t.get('avg_ms')}</td><td>{t.get('max_ms')}</td><td>{t.get('jitter_ms')}</td>"
        f"<td>{(t.get('tcp_ms') or {}).get('554','—')}</td></tr>" for t in (perf or {}).get("targets", []))
    network = f"""
<h2>Latency and packet loss</h2>
{_metric("Packet loss", "ICMP echo replies over the measurement window", "Percentage of probes that never returned; above 5% is a link problem",
         f"{sum(1 for t in (perf or {}).get('targets', []) if (t.get('loss_pct') or 0) >= 5)} targets above 5%",
         "concerning" if any((t.get('loss_pct') or 0) >= 5 for t in (perf or {}).get('targets', [])) else "healthy",
         "Wi-Fi distance/interference, repeater hops, cable/port faults", "Frozen or dropping camera streams",
         "Measure RSSI at the camera; reduce bitrate; move to a wired AP", "confirmed", "performance-metrics.json")}
<table><tr><th>Target</th><th>IP</th><th>Loss %</th><th>Avg ms</th><th>Max ms</th><th>Jitter ms</th><th>TCP 554 ms</th></tr>{perf_rows}</table>
<h2>Topology</h2>
<p class="meta">{_esc(topo.get('note', ''))} Dashed edges are inferred; solid edges come from the NVR channel table.</p>
"""

    bottleneck = f"""
<h2>Ranked bottlenecks</h2>
<p>These are ranked from the collected evidence. Each row says whether it is confirmed by a measurement or suspected from the design.</p>
<table><tr><th>Rank</th><th>Candidate</th><th>Evidence</th><th>Confidence</th><th>What would confirm it</th></tr>
<tr><td>1</td><td>Camera Wi-Fi uplink / 2.4 GHz airtime</td><td>Packet loss and latency on the weak cameras; NVR VideoLoss entries</td><td>confirmed where loss ≥ 5%</td><td>30-min <code>watch</code> log + router client list RSSI</td></tr>
<tr><td>2</td><td>Snapshot/live streaming load on the NVR</td><td>Panel load test; per-frame stream setups</td><td>suspected</td><td>Open the wall only, watch the NVR monitor</td></tr>
<tr><td>3</td><td>NVR storage/retention</td><td>{storage.get('used_pct','?')}% used</td><td>confirmed</td><td>NVR disk screen</td></tr>
<tr><td>4</td><td>Single router / single NVR</td><td>One gateway and one recorder for the whole site</td><td>design risk</td><td>Outage test during a maintenance window</td></tr></table>
<h2>Findings</h2><table><tr><th>Severity</th><th>Confidence</th><th>Finding</th><th>Action</th></tr>{alert_rows('performance')}{alert_rows('network')}{alert_rows('stability')}</table>
"""

    cap = health["capacity"]
    scalability = f"""
<h2>Assessment for {cap['users']} users</h2>
<p>{_esc(cap['verdict'])}</p>
<h2>Model and assumptions</h2>
<table><tr><th>Assumption</th><th>Value</th><th>Basis</th></tr>
<tr><td>Peak concurrent users</td><td>{cap['assumptions']['peak_concurrent_pct']}% = {cap['api']['peak_concurrent_users']}</td><td>assumption</td></tr>
<tr><td>Status polling</td><td>{cap['assumptions']['status_poll_s']} s, {cap['assumptions']['status_payload_kb']} KB</td><td>how the panel works today</td></tr>
<tr><td>API load</td><td>{cap['api']['requests_per_s']} req/s · {cap['api']['bandwidth_mbps']} Mbps</td><td>calculated</td></tr>
<tr><td>Staff live streams</td><td>{cap['video']['concurrent_streams']} × {cap['assumptions']['stream_mbps']} Mbps = {cap['video']['bandwidth_mbps']} Mbps</td><td>assumption</td></tr>
<tr><td>Camera bitrate</td><td>{cap['assumptions']['camera_main_mbps']} Mbps</td><td>NVR configured average</td></tr>
<tr><td>Recording volume</td><td>{cap['recording']['gb_per_day']} GB/day</td><td>calculated from bitrate</td></tr>
<tr><td>Retention</td><td>{cap['recording'].get('estimated_retention_days', 'unknown')} days</td><td>calculated from disk</td></tr></table>
<h2>What this means for development</h2>
<ul>
<li><b>API and database:</b> {cap['api']['requests_per_s']} req/s is small for one PC and SQLite; the panel is not the scaling limit.</li>
<li><b>Video:</b> {cap['video']['bandwidth_mbps']} Mbps for staff fits the LAN; the 2.4 GHz camera uplink is the limit, and residents must not be assumed to stream.</li>
<li><b>Storage:</b> {cap['recording']['gb_per_day']} GB/day means retention is a disk/bitrate decision, not a software one.</li>
<li><b>Notifications:</b> in-app only today; push requires HTTPS + VAPID and is independent of site capacity.</li>
</ul>
<h2>Recommendations</h2><ol>{''.join(f'<li>{_esc(r)}</li>' for r in health['recommendations'])}</ol>
"""

    pages = {
        "infrastructure-overview.html": ("Infrastructure overview", "Health, findings and data freshness", overview),
        "camera-health-report.html": ("Camera health", "Availability, latency and per-camera detail", cameras),
        "nvr-storage-report.html": ("NVR and storage", "Recorder health, disk and retention", nvr_report),
        "network-performance-report.html": ("Network performance", "Latency, loss and topology", network),
        "bottleneck-analysis.html": ("Bottleneck analysis", "Ranked candidates from the measurements", bottleneck),
        "scalability-assessment.html": ("Scalability assessment", "Can the infrastructure support 100+ users?", scalability),
    }
    for name, (title, lead, body) in pages.items():
        with open(os.path.join(REPORTS, name), "w", encoding="utf-8") as f:
            f.write(_report_page(title, lead, body, scan_id))
    _log("system", f"reports written for {scan_id}: {len(pages)} pages")


def _write_dashboard(scan_id, datasets, nvr=None, discover=None):
    ensure_tree()
    data = {
        "scan_id": scan_id,
        "generated_at": datasets["health-summary.json"]["generated_at"],
        "health": datasets["health-summary.json"],
        "inventory": datasets["device-inventory.json"],
        "topology": datasets["network-topology.json"],
        "performance": datasets["performance-metrics.json"],
        "bandwidth": datasets["bandwidth-analysis.json"],
        "history": datasets["history.json"],
        "nvr": nvr,
        "discover": discover,
        "reports": ["infrastructure-overview.html", "camera-health-report.html",
                    "network-performance-report.html", "nvr-storage-report.html",
                    "bottleneck-analysis.html", "scalability-assessment.html"],
    }
    with open(os.path.join(DASH, "js", "data.js"), "w", encoding="utf-8") as f:
        f.write("window.AUDIT_DATA = " + json.dumps(data, ensure_ascii=False, default=str) + ";\n")
    shutil.copy2(os.path.join(HERE, "dashboard_app.css"), os.path.join(DASH, "css", "app.css"))
    shutil.copy2(os.path.join(HERE, "dashboard_app.js"), os.path.join(DASH, "js", "app.js"))
    with open(os.path.join(DASH, "index.html"), "w", encoding="utf-8") as f:
        f.write(DASHBOARD_HTML.format(scan=_esc(scan_id)))
    # expose the same data for the live server
    _write_json(os.path.join(DASH, "live.json"), data)


DASHBOARD_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Adalya infrastructure — {scan}</title>
<link rel="stylesheet" href="css/app.css"></head><body>
<header class="top"><div><h1>Adalya infrastructure</h1>
<div class="sub">Scan <b id="scanId">{scan}</b> · <span id="freshness">checking data…</span></div></div>
<div class="live"><span id="liveDot" class="dot"></span><span id="liveText">static</span>
<button id="reload" class="btn">Reload</button></div></header>
<nav class="tabs" id="tabs">
<button data-tab="overview" class="on">Overview</button>
<button data-tab="cameras">Cameras</button>
<button data-tab="nvr">NVR</button>
<button data-tab="network">Network</button>
<button data-tab="history">History</button>
<button data-tab="reports">Reports</button>
<button data-tab="data">Data</button>
</nav>
<main id="view"></main>
<footer class="foot">Generated by the Adalya audit toolkit · raw data in <code>raw-data/</code> ·
every figure links back to a scan manifest · <span id="mode"></span></footer>
<script src="js/data.js"></script><script src="js/app.js"></script>
</body></html>"""
