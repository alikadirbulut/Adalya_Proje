"""Report generation and analysis for the Adalya infrastructure audit.

Consumes the JSON produced by adalya_audit.py (discover / nvr / perf / monitor) and
turns it into:

  * a machine-readable summary (report-*.json),
  * an inventory CSV,
  * a self-contained HTML report with findings, risks and a capacity model.

The capacity model is deliberately explicit about its assumptions: it is a model, not a
measurement, and every number it uses is printed next to the result.
"""

import csv
import glob
import html
import json
import os
from datetime import datetime

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}


def _latest(out_dir, kind):
    files = sorted(glob.glob(os.path.join(out_dir, f"{kind}-*.json")))
    if not files:
        return None, None
    path = files[-1]
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), path
    except (OSError, ValueError):
        return None, path


def load_monitors(out_dir, limit=10):
    """Aggregate monitor-*.jsonl files (recursively, newest first, no duplicates).

    Works on the collector folder or on the preserved raw-data tree, so a report can be
    rebuilt from raw copies alone.
    """
    found = glob.glob(os.path.join(out_dir, "monitor-*.jsonl"))
    found += glob.glob(os.path.join(out_dir, "**", "monitor-*.jsonl"), recursive=True)
    unique = {}
    for path in found:
        unique.setdefault(os.path.basename(path), path)
    files = sorted(unique.values())[-limit:]
    devices, nvr_series, transitions = {}, [], 0
    for path in files:
        try:
            f = open(path, encoding="utf-8")
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("kind") == "nvr":
                    nvr_series.append(row)
                    continue
                ip = row.get("ip")
                if not ip:
                    continue
                d = devices.setdefault(ip, {"name": row.get("name", ""), "samples": 0, "up": 0,
                                            "ms": [], "last": None, "transitions": 0})
                d["samples"] += 1
                if row.get("up"):
                    d["up"] += 1
                    if row.get("ms") is not None:
                        d["ms"].append(row["ms"])
                if d["last"] is not None and bool(d["last"]) != bool(row.get("up")):
                    d["transitions"] += 1
                    transitions += 1
                d["last"] = row.get("up")
    for d in devices.values():
        d["uptime_pct"] = round(100 * d["up"] / d["samples"], 2) if d["samples"] else None
        ms = d["ms"]
        d["avg_ms"] = round(sum(ms) / len(ms)) if ms else None
    return {"devices": devices, "nvr": nvr_series, "transitions": transitions, "files": files}


def _n(count, one, many):
    return f"{count} {one if count == 1 else many}"


def analyze(discover, nvr, perf, monitors, users=100):
    issues = []

    def add(sev, area, title, detail, evidence=""):
        issues.append({"severity": sev, "area": area, "title": title, "detail": detail, "evidence": evidence})

    # ---- discovery
    if discover:
        missing = discover.get("missing", [])
        if missing:
            shown = ", ".join(missing[:12]) + (" …" if len(missing) > 12 else "")
            add("high", "inventory", f"{_n(len(missing), 'device', 'devices')} from the inventory did not answer",
                f"Missing in this sweep: {shown}", "missing")
        unknown = discover.get("unknown", [])
        if unknown:
            shown = ", ".join(unknown[:12]) + (" …" if len(unknown) > 12 else "")
            add("info", "inventory", f"{_n(len(unknown), 'network device is', 'network devices are')} not in the inventory",
                f"Answered but unknown: {shown}. Check what they are before adding.", "unknown")
        for w in discover.get("wrong_mac", []):
            add("high", "inventory", "IP belongs to a different device than expected",
                f"{w['ip']}: expected MAC {w['expected']}, saw {w['seen']}. DHCP drift or duplicate IP.", w["ip"])
        for mac, ips in (discover.get("duplicate_mac") or {}).items():
            add("medium", "network", "One MAC address on several IPs",
                f"{mac} appears on {', '.join(ips)} – duplicate address or a bridge.", mac)
        if discover.get("in_dhcp_pool"):
            add("medium", "network", "Cameras/infrastructure inside the DHCP pool",
                "These addresses are in .150–.200 and can change after a lease: "
                + ", ".join(discover["in_dhcp_pool"]), "pool")

    # ---- NVR
    if nvr:
        counts = nvr.get("channel_counts", {})
        if counts.get("offline"):
            add("critical", "cctv", "NVR reports offline cameras",
                f"{counts['offline']} of {counts.get('total')} channels offline on the NVR itself.", "channels")
        if counts.get("named") and counts.get("named") < counts.get("total", 0):
            add("info", "cctv", f"{_n(counts['total'] - counts['named'], 'channel', 'channels')} without a name",
                f"{counts['total'] - counts['named']} of {counts['total']} channels have no name in the NVR.", "channels")
        storage = nvr.get("storage", {})
        pct = storage.get("used_pct")
        if pct is not None and pct >= 85:
            add("high", "storage", "NVR disk almost full",
                f"Disk is {pct}% full; recording will overwrite or stop soon.", f"{pct}%")
        elif pct is None:
            add("medium", "storage", "NVR disk usage unknown",
                "storageDevice.cgi did not return usable capacity numbers.", "storage")
        logs = nvr.get("logs", {})
        errs = logs.get("types", {}).get("VideoLoss", 0) + logs.get("types", {}).get("NetAbort", 0)
        if errs:
            add("high", "cctv", "Camera link errors in the NVR log",
                f"{errs} VideoLoss/NetAbort entries in the last {logs.get('window_hours', 24)} h.", "logs")
        for u in nvr.get("unavailable", []):
            add("info", "nvr", f"NVR probe unavailable: {u['probe']}", u.get("error", ""), u["probe"])
        if not nvr.get("device", {}).get("firmware"):
            add("info", "nvr", "NVR firmware not reported", "magicBox getSoftwareVersion did not answer.", "firmware")

    # ---- performance
    if perf:
        for t in perf.get("targets", []):
            if t.get("loss_pct") is not None and t["loss_pct"] >= 5:
                avg = "n/a" if t.get("avg_ms") is None else f"{t['avg_ms']} ms"
                jit = "n/a" if t.get("jitter_ms") is None else f"{t['jitter_ms']} ms"
                add("high" if t["loss_pct"] >= 20 else "medium", "performance",
                    "Packet loss", f"{t.get('name') or t['ip']}: {t['loss_pct']}% loss, "
                    f"avg {avg}, jitter {jit}.", t["ip"])
            elif t.get("avg_ms") and t["avg_ms"] >= 100:
                add("medium", "performance", "High latency",
                    f"{t.get('name') or t['ip']}: avg {t['avg_ms']} ms.", t["ip"])
            if t.get("jitter_ms") and t["jitter_ms"] >= 30:
                add("medium", "performance", "Unstable latency (jitter)",
                    f"{t.get('name') or t['ip']}: jitter {t['jitter_ms']} ms.", t["ip"])

    # ---- monitoring
    if monitors:
        for ip, d in monitors["devices"].items():
            if d.get("uptime_pct") is not None and d["uptime_pct"] < 99 and d["samples"] >= 20:
                add("high" if d["uptime_pct"] < 95 else "medium", "stability",
                    "Uptime below target",
                    f"{d.get('name') or ip}: {d['uptime_pct']}% over {d['samples']} samples, "
                    f"{d['transitions']} transitions.", ip)

    # ---- capacity model
    capacity = capacity_model(nvr, users)

    issues.sort(key=lambda i: SEV_ORDER.get(i["severity"], 9))
    recommendations = recommend(issues, capacity, discover, nvr)
    return {"issues": issues, "capacity": capacity, "recommendations": recommendations}


def capacity_model(nvr, users):
    """Explicit, assumption-driven model. Nothing here is measured except where noted."""
    channels = (nvr or {}).get("channel_counts", {}).get("named") or 18
    online = [c for c in (nvr or {}).get("channels", []) if str(c.get("online")) == "1"]
    bitrates = []
    for c in online:
        b = (c.get("main") or {}).get("bitrate_kbps")
        if b:
            bitrates.append(b)
    per_cam_mbps = round(sum(bitrates) / len(bitrates) / 1000, 2) if bitrates else 1.5

    peak_users = round(users * 0.20)
    poll_s = 3.0
    status_kb = 3
    api_rps = round(peak_users / poll_s, 1)
    api_mbps = round(api_rps * status_kb * 8 / 1000, 3)

    staff_streams = 4
    stream_mbps = 1.5
    video_mbps = round(staff_streams * stream_mbps, 1)

    gb_per_day = round(channels * per_cam_mbps * 86400 / 8 / 1000, 1)
    storage = (nvr or {}).get("storage", {})
    total_gb, used_pct = storage.get("total_gb"), storage.get("used_pct")
    retention_days = None
    if total_gb and used_pct is not None:
        free = total_gb * (100 - used_pct) / 100
        retention_days = round(free / gb_per_day, 1) if gb_per_day else None

    return {
        "users": users,
        "assumptions": {
            "peak_concurrent_pct": 20, "status_poll_s": poll_s, "status_payload_kb": status_kb,
            "staff_live_streams": staff_streams, "stream_mbps": stream_mbps,
            "camera_main_mbps": per_cam_mbps, "channels": channels,
        },
        "api": {"peak_concurrent_users": peak_users, "requests_per_s": api_rps, "bandwidth_mbps": api_mbps,
                "per_day_requests": int(api_rps * 86400 * 0.35)},
        "video": {"concurrent_streams": staff_streams, "bandwidth_mbps": video_mbps,
                  "note": "staff live view only; residents are not assumed to stream"},
        "recording": {"gb_per_day": gb_per_day, "disk_total_gb": total_gb,
                      "disk_used_pct": used_pct, "estimated_retention_days": retention_days},
        "verdict": verdict(api_rps, api_mbps, video_mbps, retention_days),
    }


def verdict(api_rps, api_mbps, video_mbps, retention_days):
    parts = []
    parts.append(f"The API load ({api_rps} req/s, ~{api_mbps} Mbps) is small for one PC and SQLite.")
    if video_mbps <= 10:
        parts.append(f"Live video for staff ({video_mbps} Mbps) fits a normal LAN, but the camera Wi-Fi uplink is the limit, not the LAN.")
    else:
        parts.append(f"Live video ({video_mbps} Mbps) is beyond what the current 2.4 GHz camera network can carry reliably.")
    if retention_days is not None and retention_days < 14:
        parts.append(f"Recording retention is only ~{retention_days} days; plan disk or bitrate changes.")
    return " ".join(parts)


def recommend(issues, capacity, discover, nvr):
    recs = []
    sev = {i["severity"] for i in issues}
    if "critical" in sev:
        recs.append("Restore the offline cameras and stabilise the NVR's camera links before adding features.")
    if any(i["area"] == "performance" for i in issues):
        recs.append("Fix the weak camera links (bitrate/placement/AP) before increasing snapshot or live-video use; "
                    "software cannot compensate for packet loss.")
    if any(i["area"] == "storage" for i in issues):
        recs.append("Check the NVR disk and retention; add storage or lower main-stream bitrate if retention < 14 days.")
    if any(i["area"] == "network" for i in issues):
        recs.append("Clean up addresses: reservations for all cameras, remove duplicates, keep devices out of the DHCP pool.")
    if any(i["area"] == "inventory" for i in issues):
        recs.append("Update devices.json from the verified channel/IP/MAC mapping; the audit and the panel both depend on it.")
    recs.append("Keep the panel's snapshot budget at 4/s (or lower) and live streams at 2 until the camera links are stable.")
    recs.append("For 100 users: one panel PC and SQLite are sufficient for API traffic; the real limits are the camera "
                "Wi-Fi, the single router and the single NVR.")
    recs.append("Before exposing anything to the internet, finish the remote-access decision (VPN + portal, HTTPS) "
                "and the KVKK documents.")
    return recs


def render_html(discover, nvr, perf, monitors, analysis, generated=None):
    e = html.escape
    when = generated or datetime.now().strftime("%Y-%m-%d %H:%M")
    s = (discover or {}).get("summary", {})
    counts = (nvr or {}).get("channel_counts", {})
    cap = analysis["capacity"]
    issues = analysis["issues"]

    def table(headers, rows, cls=""):
        head = "".join(f"<th>{e(str(h))}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in row) + "</tr>" for row in rows)
        return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

    inv_rows = []
    for h in (discover or {}).get("hosts", []):
        ports = " ".join(p for p, ok in (h.get("ports") or {}).items() if ok)
        inv_rows.append([h["ip"], h.get("mac", ""), h.get("mac_vendor", ""), h.get("role", ""),
                         h.get("name", ""), ports, "yes" if h.get("in_inventory") else "no"])

    ch_rows = []
    for c in (nvr or {}).get("channels", []):
        main = c.get("main") or {}
        ch_rows.append([c["channel"], c.get("name", ""), c.get("online", ""),
                        main.get("codec", ""), f"{main.get('width', '')}×{main.get('height', '')}".strip("×"),
                        main.get("fps", ""), main.get("bitrate_kbps", ""), (c.get("remote") or {}).get("ip", "")])

    perf_rows = [[t.get("name") or t["ip"], t["ip"], t.get("loss_pct"), t.get("avg_ms"),
                  t.get("max_ms"), t.get("jitter_ms"), (t.get("tcp_ms") or {}).get("80", ""),
                  (t.get("tcp_ms") or {}).get("554", "")] for t in (perf or {}).get("targets", [])]

    mon_rows = [[d.get("name") or ip, ip, d.get("uptime_pct"), d.get("samples"), d.get("avg_ms"),
                 d.get("transitions")] for ip, d in (monitors or {}).get("devices", {}).items()]

    issue_rows = [[i["severity"], i["area"], i["title"], i["detail"]] for i in issues]
    recs = "".join(f"<li>{e(r)}</li>" for r in analysis["recommendations"])
    assumptions = cap["assumptions"]

    css = """
    body{font:14px/1.55 Segoe UI,system-ui,sans-serif;margin:0;background:#f5f4f1;color:#1c1f24}
    main{max-width:1100px;margin:0 auto;padding:28px}
    h1{font-size:26px;margin:0 0 4px} h2{font-size:18px;margin:28px 0 8px}
    .sub{color:#5c636e} .cards{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}
    .card{background:#fff;border:1px solid #e5e1d8;border-radius:10px;padding:12px 16px;min-width:150px}
    .card b{display:block;font-size:22px} .card span{color:#5c636e;font-size:12px}
    table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e1d8;border-radius:8px;overflow:hidden}
    th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #eee9df;font-size:13px}
    th{background:#faf8f4;font-weight:600} tr:last-child td{border-bottom:0}
    .critical{color:#a11} .high{color:#b35c00} .medium{color:#7a6a00} .info{color:#555}
    .note{background:#fff8e8;border-left:4px solid #d8a53a;padding:10px 12px;border-radius:6px}
    ul{padding-left:20px} code{background:#eee;padding:1px 4px;border-radius:3px}
    small{color:#6b7280}
    @media print{body{background:#fff} main{max-width:none}}
    """
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Adalya infrastructure audit — {e(when)}</title><style>{css}</style></head><body><main>
<h1>Adalya Sitesi — infrastructure audit</h1>
<div class="sub">Generated {e(when)} · subnet {e(str((discover or {}).get('scan', {}).get('subnet', '—')))}</div>

<div class="cards">
<div class="card"><b>{s.get('alive', '—')}</b><span>hosts answering</span></div>
<div class="card"><b>{counts.get('online', '—')} / {counts.get('total', '—')}</b><span>NVR channels online</span></div>
<div class="card"><b>{len(issues)}</b><span>findings</span></div>
<div class="card"><b>{cap['recording'].get('estimated_retention_days') or '—'}</b><span>estimated recording days</span></div>
</div>

<h2>Findings</h2>
{table(['Severity', 'Area', 'Finding', 'Detail'], issue_rows)}

<h2>NVR channels</h2>
{table(['Ch', 'Name', 'Online', 'Codec', 'Resolution', 'FPS', 'kbps', 'Camera IP'], ch_rows)}

<h2>Network inventory</h2>
{table(['IP', 'MAC', 'Vendor', 'Role', 'Inventory name', 'Open ports', 'Known'], inv_rows)}

<h2>Latency and loss</h2>
{table(['Target', 'IP', 'Loss %', 'Avg ms', 'Max ms', 'Jitter ms', 'TCP 80 ms', 'TCP 554 ms'], perf_rows)}

<h2>Monitoring</h2>
{table(['Target', 'IP', 'Uptime %', 'Samples', 'Avg ms', 'Transitions'], mon_rows)}
<small>{"Source files: " + e(", ".join(os.path.basename(p) for p in monitors.get("files", []))) if monitors else "No monitor run in this report."}</small>

<h2>Capacity model for {cap['users']} users</h2>
<div class="note">{e(cap['verdict'])}</div>
{table(['Assumption', 'Value'], [
    ['peak concurrent users', f"{assumptions['peak_concurrent_pct']}% → {cap['api']['peak_concurrent_users']}"],
    ['status polling interval', f"{assumptions['status_poll_s']} s"],
    ['API requests / second', cap['api']['requests_per_s']],
    ['API bandwidth', f"{cap['api']['bandwidth_mbps']} Mbps"],
    ['staff live streams', assumptions['staff_live_streams']],
    ['video bandwidth', f"{cap['video']['bandwidth_mbps']} Mbps"],
    ['camera bitrate used', f"{assumptions['camera_main_mbps']} Mbps"],
    ['recording volume', f"{cap['recording']['gb_per_day']} GB/day"],
    ['estimated retention', f"{cap['recording'].get('estimated_retention_days') or 'unknown'} days"],
])}
<small>This is a model, not a measurement. The bitrate is the average configured main-stream
bitrate reported by the NVR; the concurrency and polling figures are assumptions stated above.</small>

<h2>Recommended next steps</h2><ol>{recs}</ol>

<h2>Method and limits</h2>
<p><small>Read-only probes: ICMP, ARP, TCP connect (80/443/554/37777/8000), one unauthenticated HTTP GET,
and the Dahua HTTP API with a single login attempt. Nothing was configured, reset or brute-forced.
Cameras were not logged into directly; their model/firmware may therefore be missing. Wi-Fi signal
strength and per-camera airtime cannot be measured from the wired side and must come from the router
client list and the site walk.</small></p>
</main></body></html>"""


def write_report(out_dir, discover, nvr, perf, monitors, users=100):
    analysis = analyze(discover, nvr, perf, monitors, users=users)
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    doc = {"generated": datetime.now().isoformat(timespec="seconds"), "users": users,
           "discover": discover, "nvr": nvr, "perf": perf,
           "monitoring": monitors, "analysis": analysis}
    json_path = os.path.join(out_dir, f"report-{tag}.json")
    html_path = os.path.join(out_dir, f"report-{tag}.html")
    csv_path = os.path.join(out_dir, f"inventory-{tag}.csv")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, default=str)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(discover, nvr, perf, monitors, analysis))
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ip", "mac", "vendor", "role", "name", "ports", "known"])
        for h in (discover or {}).get("hosts", []):
            ports = " ".join(p for p, ok in (h.get("ports") or {}).items() if ok)
            w.writerow([h["ip"], h.get("mac", ""), h.get("mac_vendor", ""), h.get("role", ""),
                        h.get("name", ""), ports, "yes" if h.get("in_inventory") else "no"])
    return {"json": json_path, "html": html_path, "inventory_csv": csv_path,
            "issues": len(analysis["issues"])}
