"""Network discovery, performance measurement and long-running sampling.

Read-only: ICMP, ARP, TCP connects and a single HTTP GET for latency. No device is
configured, reset or authenticated except the NVR (handled in audit_nvr.py). Scans are
rate-limited and every probe has a short timeout, so a broken device cannot stall the
audit or flood the site network.

Reuses adalya_test.py for ping, ARP, TCP and HTTP so there is one implementation of
each primitive in the project.
"""

import csv
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import adalya_test as kit

# Only vendors actually seen on this site (plus two common Dahua/Tenda OUIs).
OUI = {
    "e02efe": "Dahua",
    "3cef8c": "Dahua",
    "b40f3b": "Tenda",
    "70856c": "Ruijie",
    "503dd1": "TP-Link",
}

COMMON_PORTS = (80, 443, 554, 37777, 8000)
PORT_NAMES = {80: "http", 443: "https", 554: "rtsp", 37777: "dahua", 8000: "http-alt"}


def vendor_of(mac):
    return OUI.get(kit.norm_mac(mac)[:6], "")


def load_inventory(path):
    """devices.json -> {ip: device} (hidden entries included, flagged)."""
    try:
        devices = kit.load_devices(path)
    except (OSError, ValueError):
        return {}
    return {d["ip"]: d for d in devices if d.get("ip")}


def _ports(ip, ports, timeout=0.7):
    with ThreadPoolExecutor(max_workers=len(ports)) as ex:
        results = list(ex.map(lambda p: kit.tcp_open(ip, p, timeout), ports))
    return {p: ok for p, ok in zip(ports, results)}


def classify(ip, mac, ports, inventory):
    """Guess a role from inventory first, then ports/vendor."""
    inv = inventory.get(ip)
    if inv:
        return inv.get("type", "unknown"), inv.get("name", ""), True
    vendor = vendor_of(mac)
    if ports.get(37777) or ports.get(554):
        if vendor == "Dahua":
            return "dahua-device", "", False
        return "camera?", "", False
    if ports.get(80) or ports.get(443):
        return "http-device", "", False
    return "unknown", "", False


def sweep(subnet="auto", timeout_ms=350, workers=64, ports=COMMON_PORTS, do_dns=False, limit=1024):
    """ICMP sweep + ARP + a few TCP ports. Returns the discovery document."""
    if subnet == "auto":
        subnet = local_subnet()
    net = ipaddress.ip_network(subnet, strict=False)
    hosts = list(net.hosts())[:limit]
    started = time.time()
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        pings = list(ex.map(lambda ip: kit.ping(str(ip), timeout_ms), hosts))

    arp = kit.arp_table()
    inventory = load_inventory(os.path.join(kit.HERE, "devices.json"))

    alive = [str(ip) for ip, (ok, _) in zip(hosts, pings) if ok]
    port_map = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(lambda ip: (ip, _ports(ip, ports)), alive))
    port_map = dict(results)

    def hostname(ip):
        if not do_dns:
            return ""
        try:
            return socket.gethostbyaddr(ip)[0]
        except OSError:
            return ""

    names = {}
    if do_dns and alive:
        with ThreadPoolExecutor(max_workers=8) as ex:
            names = dict(zip(alive, ex.map(hostname, alive)))

    rows = []
    for ip, (ok, _) in zip(hosts, pings):
        ip = str(ip)
        if not ok:
            continue
        mac = arp.get(ip, "")
        role, name, known = classify(ip, mac, port_map.get(ip, {}), inventory)
        rows.append({
            "ip": ip, "mac": mac, "mac_vendor": vendor_of(mac), "hostname": names.get(ip, ""),
            "role": role, "name": name, "in_inventory": known, "alive": True,
            "ports": {str(p): bool(v) for p, v in (port_map.get(ip) or {}).items()},
        })

    inv_ips = set(inventory)
    seen = {r["ip"] for r in rows}
    missing = sorted(inv_ips - seen)
    # A phone or laptop with no open service is not an "unknown device" worth reporting;
    # only hosts that look like infrastructure (known vendor or a management port).
    unknown = [r for r in rows if not r["in_inventory"]
               and (r["mac_vendor"] or any(r["ports"].values()) or r["role"] != "unknown")]

    # duplicate MAC (same NIC on two IPs) and duplicate IP (inventory expects another MAC)
    by_mac = {}
    for r in rows:
        if r["mac"]:
            by_mac.setdefault(r["mac"], []).append(r["ip"])
    duplicate_mac = {m: ips for m, ips in by_mac.items() if len(ips) > 1}
    wrong_mac = []
    for r in rows:
        inv = inventory.get(r["ip"])
        want = kit.norm_mac(inv.get("mac")) if inv else ""
        if want and r["mac"] and kit.norm_mac(r["mac"]) != want:
            wrong_mac.append({"ip": r["ip"], "expected": want, "seen": kit.norm_mac(r["mac"])})

    pool = [r["ip"] for r in rows if 150 <= int(r["ip"].split(".")[-1]) <= 200
            and r["role"] not in ("unknown",)]
    return {
        "kind": "discover",
        "scan": {"subnet": subnet, "started": datetime.now().isoformat(timespec="seconds"),
                 "duration_s": round(time.perf_counter() - t0, 1), "probes": len(hosts),
                 "ports": list(ports), "dns": bool(do_dns)},
        "summary": {"alive": len(rows), "expected": len(inventory), "missing": len(missing),
                    "unknown": len(unknown), "cameras": sum(1 for r in rows if r["role"] in ("camera", "dahua-device")),
                    "duplicate_mac": len(duplicate_mac), "wrong_mac": len(wrong_mac)},
        "hosts": sorted(rows, key=lambda r: tuple(int(x) for x in r["ip"].split("."))),
        "missing": missing,
        "unknown": [r["ip"] for r in unknown],
        "duplicate_mac": duplicate_mac,
        "wrong_mac": wrong_mac,
        "in_dhcp_pool": pool,
        "finished": time.time(),
    }


def local_subnet():
    """Best-effort /24 of the interface that would reach the internet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.0.1", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip.rsplit(".", 1)[0] + ".0/24"


TIME_RE = re.compile(r"[=<]\s*(\d+)\s*ms", re.I)


def ping_rtt(ip, timeout_ms=700):
    """One ICMP echo with the real RTT from the reply (not the subprocess wall time)."""
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", str(timeout_ms), ip],
                           capture_output=True, text=True, timeout=timeout_ms / 1000 + 3)
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    ok = r.returncode == 0 and "TTL" in r.stdout.upper()
    m = TIME_RE.search(r.stdout)
    return ok, (float(m.group(1)) if m else (0.5 if ok else None))


def ping_stats(ip, count=6, timeout_ms=800):
    """One ping process per target: loss %, min/avg/max, jitter (stddev of samples)."""
    cmd = ["ping", "-n", str(count), "-w", str(timeout_ms), ip]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=count * 1.2 + 6)
    except subprocess.TimeoutExpired:
        return {"ip": ip, "sent": count, "recv": 0, "loss_pct": 100.0,
                "min_ms": None, "avg_ms": None, "max_ms": None, "jitter_ms": None}
    samples = [float(m.group(1)) for m in TIME_RE.finditer(r.stdout)]
    ttl = len(re.findall(r"TTL[=:]", r.stdout, re.I))
    if ttl > len(samples):
        samples += [0.5] * (ttl - len(samples))   # "time<1ms" lines
    recv = max(ttl, len(samples))
    if not recv:
        return {"ip": ip, "sent": count, "recv": 0, "loss_pct": 100.0,
                "min_ms": None, "avg_ms": None, "max_ms": None, "jitter_ms": None}
    avg = sum(samples) / len(samples)
    var = sum((x - avg) ** 2 for x in samples) / len(samples)
    return {"ip": ip, "sent": count, "recv": recv,
            "loss_pct": round(100 * (count - recv) / count, 1),
            "min_ms": min(samples), "avg_ms": round(avg, 1), "max_ms": max(samples),
            "jitter_ms": round(var ** 0.5, 1)}


def tcp_latency(ip, port, tries=3, timeout=1.0):
    best = None
    for _ in range(tries):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                ms = (time.perf_counter() - t0) * 1000
                best = ms if best is None else min(best, ms)
        except OSError:
            pass
    return round(best, 1) if best is not None else None


def perf(rows, seconds=8, ports=(80, 554), nvr_ip=None):
    """Ping + TCP latency for a list of {name, ip, role}; optional NVR HTTP latency."""
    targets = [r for r in rows if r.get("ip")]
    out = {"kind": "perf", "ts": time.time(), "started": datetime.now().isoformat(timespec="seconds"),
           "targets": [], "window_s": seconds}
    for t in targets:
        st = ping_stats(t["ip"], count=max(4, min(30, seconds)))
        st.update({"name": t.get("name", ""), "role": t.get("role", "")})
        for p in ports:
            st.setdefault("tcp_ms", {})[str(p)] = tcp_latency(t["ip"], p)
        if nvr_ip and t["ip"] == nvr_ip:
            t0 = time.perf_counter()
            try:
                kit.http_get(nvr_ip, "/cgi-bin/magicBox.cgi?action=getDeviceType", "", "", timeout=3)
            except OSError:
                pass
            st["http_ms"] = round((time.perf_counter() - t0) * 1000)
        out["targets"].append(st)
    return out


def stream_bitrate(ffmpeg, host, user, password, channel, seconds=8):
    """Measure one NVR sub-stream for a few seconds. The URL (with password) stays in
    process memory only; the returned dict and logs never contain it."""
    from urllib.parse import quote
    url = (f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:554"
           f"/cam/realmonitor?channel={int(channel)}&subtype=1")
    t0 = time.perf_counter()
    try:
        r = subprocess.run([ffmpeg, "-nostdin", "-rtsp_transport", "tcp", "-i", url,
                            "-t", str(seconds), "-c", "copy", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=seconds + 15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"channel": channel, "ok": False, "error": type(e).__name__}
    elapsed = max(0.1, time.perf_counter() - t0)
    m = re.findall(r"video:\s*(\d+)\s*kB", r.stderr)
    if not m:
        return {"channel": channel, "ok": False, "error": "no video bytes reported"}
    kb = int(m[-1])
    kbps = round(kb * 8 / elapsed)
    return {"channel": channel, "ok": True, "seconds": round(elapsed, 1), "kbps": kbps,
            "mb_per_hour": round(kbps * 3600 / 8 / 1000, 1)}


def monitor(targets, minutes, interval, out_dir, nvr=None, nvr_every=60, nvr_ip=None):
    """Sample every device until the deadline; append JSONL + a summary CSV."""
    os.makedirs(out_dir, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl_path = os.path.join(out_dir, f"monitor-{tag}.jsonl")
    deadline = time.time() + minutes * 60
    stats = {t["ip"]: {"name": t.get("name", ""), "samples": 0, "ok": 0, "ms": []} for t in targets}
    last_state = {}
    transitions = 0
    next_nvr = 0
    print(f"Monitoring {len(targets)} devices every {interval:g}s for {minutes:g} min -> {jsonl_path}")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        while time.time() < deadline:
            cycle = time.time()
            with ThreadPoolExecutor(max_workers=min(16, max(1, len(targets)))) as ex:
                results = list(ex.map(lambda t: (t, ping_rtt(t["ip"], 700)), targets))
            for t, (ok, ms) in results:
                s = stats[t["ip"]]
                s["samples"] += 1
                s["ok"] += ok
                if ok:
                    s["ms"].append(ms)
                prev = last_state.get(t["ip"])
                if prev is not None and prev != ok:
                    transitions += 1
                    print(f"{datetime.now():%H:%M:%S}  {'UP  ' if ok else 'DOWN'} {t.get('name') or t['ip']}")
                last_state[t["ip"]] = ok
                f.write(json.dumps({"ts": round(time.time(), 1), "kind": "device", "ip": t["ip"],
                                    "name": t.get("name", ""), "up": bool(ok), "ms": ms},
                                   ensure_ascii=False) + "\n")
            if nvr is not None and time.time() >= next_nvr:
                rec = quick_nvr_state(nvr)
                f.write(json.dumps({"ts": round(time.time(), 1), "kind": "nvr", **rec}) + "\n")
                next_nvr = time.time() + nvr_every
            f.flush()
            time.sleep(max(0.2, interval - (time.time() - cycle)))
    summary_path = os.path.join(out_dir, f"monitor-summary-{tag}.csv")
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ip", "name", "samples", "up", "loss_pct", "min_ms", "avg_ms", "max_ms", "transitions"])
        for ip, s in stats.items():
            samples = s["samples"] or 1
            ms = [m for m in s["ms"] if m is not None]
            w.writerow([ip, s["name"], s["samples"], s["ok"],
                        round(100 * (s["samples"] - s["ok"]) / samples, 1),
                        min(ms) if ms else "", round(sum(ms) / len(ms)) if ms else "",
                        max(ms) if ms else "", transitions])
    print(f"Summary: {summary_path}")
    return {"jsonl": jsonl_path, "summary": summary_path, "transitions": transitions}


def quick_nvr_state(nvr):
    """Two cheap NVR calls: camera states + disk. No configuration change."""
    from nvr import NvrError, NvrAuthError, parse_kv
    out = {"online": None, "offline": None, "disk_pct": None, "error": None}
    try:
        kv = parse_kv(nvr.get("/cgi-bin/LogicDeviceManager.cgi?action=getCameraState&uniqueChannels[0]=-1"))
        states = [v for k, v in kv.items() if k.endswith(".State")]
        out["online"] = sum(1 for v in states if v == "1")
        out["offline"] = sum(1 for v in states if v == "0")
        storage = parse_kv(nvr.get("/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo"))
        total = sum(int(float(v)) for k, v in storage.items() if k.endswith("TotalBytes"))
        used = sum(int(float(v)) for k, v in storage.items() if k.endswith("UsedBytes"))
        out["disk_pct"] = round(100 * used / total) if total else None
    except (NvrError, NvrAuthError, ValueError) as e:
        out["error"] = str(e)
    return out
