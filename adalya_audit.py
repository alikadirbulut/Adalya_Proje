"""Adalya infrastructure audit — discovery, NVR inspection, performance, monitoring, report.

One entry point for the on-site audit:

  python adalya_audit.py discover            # ICMP/ARP/port sweep, compare with devices.json
  python adalya_audit.py nvr --user admin    # deep read-only NVR inspection (asks password)
  python adalya_audit.py perf --seconds 30   # latency, loss, jitter, TCP/HTTP timings
  python adalya_audit.py monitor --minutes 60
  python adalya_audit.py report --users 100  # HTML + JSON + inventory CSV
  python adalya_audit.py full --user admin --monitor-min 30

Security: read-only. The only authenticated access is the NVR, with a single login
attempt; the password is held in memory and never written to a file or log. No camera,
switch or router configuration is touched. Scans are rate-limited and short-timeout.

Reuses adalya_test.py (ping/ARP/TCP/HTTP primitives) and nvr.py (Dahua client) instead
of re-implementing them; audit_nvr/audit_net/audit_report build on those.
"""

import argparse
import csv
import getpass
import json
import os
import shutil
import sys
import time
from datetime import datetime

import adalya_test as kit
import audit_net
import audit_report
from audit_nvr import AuditNvr

OUT = os.path.join(kit.HERE, "results", "audit")


def ensure_out():
    os.makedirs(OUT, exist_ok=True)
    return OUT


def write_json(prefix, doc):
    ensure_out()
    path = os.path.join(OUT, f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, default=str)
    return path


def latest(kind):
    return audit_report._latest(OUT, kind)[0]


def ask_password(args):
    """Ask once per run; keep it in memory only (never written, never logged)."""
    pw = getattr(args, "_nvr_password", None)
    if pw is None:
        pw = getpass.getpass(f"NVR password for {args.user}@{args.host} (not saved): ")
        args._nvr_password = pw
    return pw


def targets_from(discover=None):
    """Discovery hosts when available, else devices.json."""
    if discover and discover.get("hosts"):
        return [{"name": h.get("name") or h.get("hostname") or "", "ip": h["ip"],
                 "role": h.get("role", "")} for h in discover["hosts"]]
    try:
        return [{"name": d.get("name", ""), "ip": d["ip"], "role": d.get("type", "")}
                for d in kit.load_devices(os.path.join(kit.HERE, "devices.json"))]
    except (OSError, ValueError):
        return []


def cmd_discover(args):
    print(f"Sweeping {args.subnet} (timeout {args.timeout} ms, {args.workers} workers)...")
    ports = tuple(int(p) for p in args.ports.split(",") if p.strip())
    doc = audit_net.sweep(args.subnet, timeout_ms=args.timeout, workers=args.workers,
                          ports=ports, do_dns=args.dns, limit=args.limit)
    path = write_json("discover", doc)
    s = doc["summary"]
    print(f"  {s['alive']} hosts answered, {s['expected']} expected, {s['missing']} missing, "
          f"{s['unknown']} unknown")
    for h in doc["hosts"]:
        flag = "" if h["in_inventory"] else "  NEW"
        print(f"  {h['ip']:<15} {h['mac']:<18} {h['mac_vendor']:<8} {h['role']:<14} {h['name']}{flag}")
    csv_path = path.replace(".json", ".csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ip", "mac", "vendor", "role", "name", "ports", "known"])
        for h in doc["hosts"]:
            w.writerow([h["ip"], h["mac"], h["mac_vendor"], h["role"], h["name"],
                        " ".join(p for p, ok in h["ports"].items() if ok),
                        "yes" if h["in_inventory"] else "no"])
    print(f"Saved: {path}\n       {csv_path}")
    return doc


def cmd_nvr(args):
    password = ask_password(args)
    client = AuditNvr(args.host, args.user, password, port=args.port)
    print(f"Inspecting {args.host} (read-only)...")
    doc = client.collect(log_hours=args.log_hours)
    path = write_json("nvr", doc)
    dev, counts, storage = doc["device"], doc["channel_counts"], doc["storage"]
    print(f"  {dev.get('type') or 'NVR'} · firmware {dev.get('firmware') or '?'} · serial {dev.get('serial') or '?'}")
    print(f"  channels: {counts['named']} named / {counts['total']} · online {counts['online']} · offline {counts['offline']}")
    print(f"  disk: {storage.get('used_pct')}% of {storage.get('total_gb')} GB · states {', '.join(storage.get('states', [])) or '?'}")
    if doc["logs"]["types"]:
        print("  log types (24 h): " + ", ".join(f"{k}×{v}" for k, v in doc["logs"]["types"].items()))
    for u in doc["unavailable"]:
        print(f"  unavailable: {u['probe']} ({u['error'][:70]})")
    print(f"Saved: {path}")
    return doc


def cmd_perf(args):
    discover = latest("discover")
    targets = targets_from(discover)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        targets = [t for t in targets if t["ip"] in wanted or t["ip"].rsplit(".", 1)[-1] in wanted]
    if not targets:
        print("No targets. Run discover first or check devices.json.")
        return None
    print(f"Measuring {len(targets)} targets ({args.seconds}s window)...")
    doc = audit_net.perf(targets, seconds=args.seconds, ports=(80, 554),
                         nvr_ip=args.nvr_host)
    path = write_json("perf", doc)
    for t in doc["targets"]:
        print(f"  {t['ip']:<15} loss {str(t['loss_pct']):>4}%  avg {str(t['avg_ms']):>6} ms  "
              f"jitter {str(t['jitter_ms']):>5} ms  tcp80 {t['tcp_ms'].get('80')}  tcp554 {t['tcp_ms'].get('554')}")
    if args.stream_ch:
        password = ask_password(args)
        ff = shutil.which("ffmpeg")
        if ff:
            print(f"  measuring channel {args.stream_ch} for {args.stream_seconds}s...")
            m = audit_net.stream_bitrate(ff, args.nvr_host, args.user, password, args.stream_ch,
                                         args.stream_seconds)
            doc["stream"] = m
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2, default=str)
            print(f"  stream: {m}")
    print(f"Saved: {path}")
    return doc


def cmd_monitor(args):
    discover = latest("discover")
    targets = targets_from(discover)
    if not targets:
        print("No targets. Run discover first or check devices.json.")
        return None
    nvr = None
    if args.user:
        password = ask_password(args)
        nvr = AuditNvr(args.host, args.user, password, port=args.port)
    res = audit_net.monitor(targets, args.minutes, args.interval, ensure_out(), nvr=nvr,
                            nvr_every=args.nvr_every)
    print(f"Saved: {res['jsonl']}\n       {res['summary']}")
    return res


def cmd_report(args):
    discover = latest("discover")
    nvr = latest("nvr")
    perf = latest("perf")
    monitors = audit_report.load_monitors(OUT)
    if not any((discover, nvr, perf, monitors["devices"])):
        print("Nothing to report yet. Run discover / nvr / perf first.")
        return None
    res = audit_report.write_report(OUT, discover, nvr, perf, monitors, users=args.users)
    print(f"Report:  {res['html']}")
    print(f"JSON:    {res['json']}")
    print(f"CSV:     {res['inventory_csv']}")
    print(f"Findings: {res['issues']}")
    return res


def cmd_full(args):
    print("== 1/5 discovery ==")
    discover = cmd_discover(args)
    if args.user:
        ask_password(args)   # once, reused by the NVR stage and monitoring
    nvr_doc = None
    if args.user:
        print("\n== 2/5 NVR inspection ==")
        nvr_doc = cmd_nvr(args)
    print("\n== 3/5 performance ==")
    cmd_perf(args)
    if not args.no_monitor:
        print(f"\n== 4/5 monitoring for {args.monitor_min:g} min ==")
        cmd_monitor(args)
    print("\n== 5/6 report ==")
    cmd_report(args)
    print("\n== 6/6 infrastructure-audit centre ==")
    import audit_store
    res = audit_store.build(users=args.users)
    print(f"  scan {res['scan_id']} · health {res['score']}/100 ({res['grade']}) · {res['alerts']} findings")
    print(f"  dashboard: {res['dashboard']}")


def main():
    p = argparse.ArgumentParser(description="Adalya infrastructure audit (read-only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="sweep the subnet and compare with devices.json")
    d.add_argument("--subnet", default="auto", help="CIDR, or 'auto' for the local /24")
    d.add_argument("--timeout", type=int, default=350, help="ICMP timeout in ms")
    d.add_argument("--workers", type=int, default=64)
    d.add_argument("--limit", type=int, default=1024, help="max addresses to probe")
    d.add_argument("--ports", default="80,443,554,37777,8000")
    d.add_argument("--dns", action="store_true", help="reverse-DNS every answering host (slower)")
    d.set_defaults(func=cmd_discover)

    n = sub.add_parser("nvr", help="deep read-only NVR inspection")
    n.add_argument("--host", default="192.168.0.254")
    n.add_argument("--port", type=int, default=80)
    n.add_argument("--user", default="admin")
    n.add_argument("--log-hours", type=int, default=24)
    n.set_defaults(func=cmd_nvr)

    f = sub.add_parser("perf", help="latency, loss, jitter and TCP timings")
    f.add_argument("--seconds", type=int, default=8)
    f.add_argument("--only", help="comma list of IPs or last octets")
    f.add_argument("--nvr-host", default="192.168.0.254")
    f.add_argument("--user", default="admin")
    f.add_argument("--stream-ch", type=int, help="also measure one NVR sub-stream (asks password)")
    f.add_argument("--stream-seconds", type=int, default=8)
    f.set_defaults(func=cmd_perf)

    m = sub.add_parser("monitor", help="sample device/NVR health over time")
    m.add_argument("--minutes", type=float, default=30)
    m.add_argument("--interval", type=float, default=5)
    m.add_argument("--nvr-every", type=float, default=60)
    m.add_argument("--host", default="192.168.0.254")
    m.add_argument("--port", type=int, default=80)
    m.add_argument("--user", default="", help="NVR user; leave empty to skip NVR sampling")
    m.set_defaults(func=cmd_monitor)

    r = sub.add_parser("report", help="build HTML/JSON/CSV from the latest files")
    r.add_argument("--users", type=int, default=100)
    r.set_defaults(func=cmd_report)

    u = sub.add_parser("full", help="discover + nvr + perf + monitor + report")
    u.add_argument("--subnet", default="auto")
    u.add_argument("--timeout", type=int, default=350)
    u.add_argument("--workers", type=int, default=64)
    u.add_argument("--limit", type=int, default=1024)
    u.add_argument("--ports", default="80,443,554,37777,8000")
    u.add_argument("--dns", action="store_true")
    u.add_argument("--host", default="192.168.0.254")
    u.add_argument("--port", type=int, default=80)
    u.add_argument("--user", default="admin")
    u.add_argument("--log-hours", type=int, default=24)
    u.add_argument("--seconds", type=int, default=8)
    u.add_argument("--only", default=None)
    u.add_argument("--nvr-host", default="192.168.0.254")
    u.add_argument("--stream-ch", type=int, default=None)
    u.add_argument("--stream-seconds", type=int, default=8)
    u.add_argument("--minutes", type=float, default=30, dest="monitor_minutes")
    u.add_argument("--interval", type=float, default=5)
    u.add_argument("--nvr-every", type=float, default=60)
    u.add_argument("--monitor-min", type=float, default=10)
    u.add_argument("--no-monitor", action="store_true")
    u.add_argument("--users", type=int, default=100)
    u.set_defaults(func=cmd_full)

    args = p.parse_args()
    if args.cmd == "full":
        args.minutes = args.monitor_min
    ensure_out()
    args.func(args)


if __name__ == "__main__":
    main()
