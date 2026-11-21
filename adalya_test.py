"""Adalya site test toolkit. Read-only checks, Python standard library only.

Commands:
  python adalya_test.py check                 one-shot health check of every device in devices.json
  python adalya_test.py watch [--only ...]    continuous ping log, records every drop with its duration
  python adalya_test.py nvr --user admin      probe the Dahua NVR HTTP API (asks for the password)
  python adalya_test.py channels              find which NVR channels have a camera, one snapshot at a time
  python adalya_test.py events                listen to the NVR's person/vehicle/motion event push
  python adalya_test.py snmp --host IP        test whether a device answers SNMP

Nothing here changes any device. Passwords are asked at runtime and never saved.
"""

import argparse
import csv
import getpass
import hashlib
import http.client
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")
IS_WIN = platform.system() == "Windows"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def load_devices(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def norm_mac(mac):
    return re.sub(r"[^0-9a-f]", "", (mac or "").lower())


# ---------------------------------------------------------------- ping / arp / ports

def ping(ip, timeout_ms=1000):
    if IS_WIN:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_ms / 1000 + 3)
    except subprocess.TimeoutExpired:
        return False, None
    ms = round((time.perf_counter() - t0) * 1000)
    ok = r.returncode == 0 and "ttl=" in r.stdout.lower()
    return ok, (ms if ok else None)


def arp_table():
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    table = {}
    for m in re.finditer(r"\(?(\d+\.\d+\.\d+\.\d+)\)?\s+(?:at\s+)?([0-9a-fA-F]{1,2}(?:[-:][0-9a-fA-F]{1,2}){5})", out):
        table[m.group(1)] = norm_mac(m.group(2))
    return table


def tcp_open(ip, port, timeout=1.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- SNMP v2c GET (minimal)

def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, value):
    return bytes([tag]) + _ber_len(len(value)) + value


def _ber_int(v):
    b = v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True)
    return _tlv(0x02, b)


def _ber_oid(oid):
    parts = [int(x) for x in oid.split(".")]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


def _ber_read(data, i):
    tag = data[i]
    i += 1
    ln = data[i]
    i += 1
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(data[i:i + k], "big")
        i += k
    return tag, data[i:i + ln], i + ln


def snmp_get(ip, community="public", oid="1.3.6.1.2.1.1.1.0", timeout=2.0):
    varbind = _tlv(0x30, _ber_oid(oid) + b"\x05\x00")
    pdu = _tlv(0xA0, _ber_int(4242) + _ber_int(0) + _ber_int(0) + _tlv(0x30, varbind))
    msg = _tlv(0x30, _ber_int(1) + _tlv(0x04, community.encode()) + pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, (ip, 161))
        data, _ = s.recvfrom(65535)
    except OSError:
        return None
    finally:
        s.close()
    try:
        _, seq, _ = _ber_read(data, 0)
        i = 0
        for _ in range(2):
            _, _, i = _ber_read(seq, i)
        _, pdu_body, _ = _ber_read(seq, i)
        j = 0
        for _ in range(3):
            _, _, j = _ber_read(pdu_body, j)
        _, vbl, _ = _ber_read(pdu_body, j)
        _, vb, _ = _ber_read(vbl, 0)
        _, _, k = _ber_read(vb, 0)
        tag, val, _ = _ber_read(vb, k)
        if tag == 0x04:
            return val.decode("utf-8", "replace")
        return f"(answered, value tag 0x{tag:02x})"
    except Exception:
        return "(answered, could not parse)"


# ---------------------------------------------------------------- HTTP digest (single attempt, lockout-safe)

def _parse_auth(header):
    return dict(re.findall(r'(\w+)="?([^",]+)"?', header))


def http_get(host, path, user, password, port=80, timeout=5):
    """GET with Digest or Basic auth. Exactly one authenticated attempt, so a wrong
    password costs one failed login, not five."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    if r.status != 401:
        conn.close()
        return r.status, body
    hdr = r.getheader("WWW-Authenticate", "")
    conn.close()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    if hdr.lower().startswith("digest"):
        a = _parse_auth(hdr)
        realm, nonce, qop = a.get("realm", ""), a.get("nonce", ""), a.get("qop", "")
        ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"GET:{path}".encode()).hexdigest()
        cnonce, nc = os.urandom(8).hex(), "00000001"
        if "auth" in qop:
            resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
            auth = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{path}", '
                    f'qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"')
        else:
            resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            auth = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{path}", response="{resp}"'
        if "opaque" in a:
            auth += f', opaque="{a["opaque"]}"'
    else:
        import base64
        auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    conn.request("GET", path, headers={"Authorization": auth})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, body


# ---------------------------------------------------------------- commands

def cmd_check(args):
    devices = load_devices(args.devices)
    os.makedirs(OUT, exist_ok=True)
    print(f"Checking {len(devices)} devices...\n")

    with ThreadPoolExecutor(max_workers=24) as ex:
        pings = list(ex.map(lambda d: ping(d["ip"]), devices))
    arp = arp_table()

    rows = []
    for d, (ok, ms) in zip(devices, pings):
        seen = arp.get(d["ip"], "")
        want = norm_mac(d.get("mac"))
        if not seen:
            mac_note = "no ARP entry" if ok else ""
        elif want and seen != want:
            mac_note = f"WRONG DEVICE ({seen})"
        elif want:
            mac_note = "MAC matches"
        else:
            mac_note = seen
        ports = d.get("ports") or ([37777] if d["type"] == "camera" else [])
        port_res = {p: tcp_open(d["ip"], p) for p in ports} if ok or args.ports_always else {}
        rows.append({
            "name": d["name"], "ip": d["ip"], "type": d["type"],
            "status": "UP" if ok else "DOWN", "ms": ms if ms is not None else "",
            "mac": mac_note,
            "ports": " ".join(f"{p}:{'open' if v else 'closed'}" for p, v in port_res.items()),
        })

    snmp_rows = []
    if not args.no_snmp:
        for d in devices:
            if d["type"] in ("router", "switch", "ap", "nvr") and d["ip"] != "192.168.0.254":
                snmp_rows.append((d["name"], d["ip"], snmp_get(d["ip"], args.community)))

    w = max(len(r["name"]) for r in rows)
    print(f"{'Device'.ljust(w)}  {'IP'.ljust(15)}  Status  ms     MAC check / ports")
    print("-" * (w + 60))
    for r in rows:
        print(f"{r['name'].ljust(w)}  {r['ip'].ljust(15)}  {r['status'].ljust(6)}  {str(r['ms']).ljust(5)}  {r['mac']}  {r['ports']}")

    cams = [r for r in rows if r["type"] == "camera" and not r["ip"].startswith("192.168.1.")]
    up = sum(r["status"] == "UP" for r in cams)
    print(f"\nCameras up on 192.168.0.x: {up} / {len(cams)}")
    wrong = [r for r in rows if r["mac"].startswith("WRONG")]
    if wrong:
        print("IP conflicts / drift: " + ", ".join(f"{r['name']} {r['ip']}" for r in wrong))
    old = [r for r in rows if r["ip"].startswith("192.168.1.") and r["status"] == "UP"]
    if old:
        print("Still answering on old subnet: " + ", ".join(r["name"] for r in old))

    if snmp_rows:
        print(f"\nSNMP (community '{args.community}'):")
        for name, ip, res in snmp_rows:
            print(f"  {name} {ip}: {res if res else 'no answer'}")

    path = os.path.join(OUT, f"check-{stamp()}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    print(f"\nSaved: {path}")


def cmd_watch(args):
    devices = load_devices(args.devices)
    if args.only:
        keys = [k.strip() for k in args.only.split(",")]
        devices = [d for d in devices if any(d["ip"] == k or (d["ip"].startswith("192.168.0.") and d["ip"].split(".")[-1] == k) for k in keys)]
    else:
        devices = [d for d in devices if not d["ip"].startswith("192.168.1.")]
    if not devices:
        print("No devices matched.")
        return
    os.makedirs(OUT, exist_ok=True)
    tag = stamp()
    ev_path = os.path.join(OUT, f"drops-{tag}.csv")
    ev_file = open(ev_path, "w", newline="", encoding="utf-8-sig")
    ev = csv.writer(ev_file)
    ev.writerow(["device", "ip", "down_at", "up_at", "seconds"])

    state = {d["ip"]: {"up": None, "since": None, "drops": 0, "down_total": 0.0, "samples": 0, "ok": 0} for d in devices}
    names = {d["ip"]: d["name"] for d in devices}
    started = time.time()
    print(f"Watching {len(devices)} devices every {args.interval}s. Ctrl+C to stop.")
    print(f"Drops log: {ev_path}\n")
    try:
        with ThreadPoolExecutor(max_workers=min(32, len(devices))) as ex:
            while True:
                t0 = time.time()
                results = list(ex.map(lambda d: (d["ip"], ping(d["ip"], args.timeout)[0]), devices))
                now = time.time()
                for ip, ok in results:
                    s = state[ip]
                    s["samples"] += 1
                    s["ok"] += ok
                    if s["up"] is None:
                        s["up"], s["since"] = ok, now
                        print(f"{datetime.now():%H:%M:%S}  {names[ip]} {ip} starts {'UP' if ok else 'DOWN'}")
                    elif ok != s["up"]:
                        if ok:
                            dur = now - s["since"]
                            s["drops"] += 1
                            s["down_total"] += dur
                            ev.writerow([names[ip], ip, datetime.fromtimestamp(s["since"]).strftime("%H:%M:%S"),
                                         datetime.fromtimestamp(now).strftime("%H:%M:%S"), round(dur)])
                            ev_file.flush()
                            print(f"{datetime.now():%H:%M:%S}  BACK  {names[ip]} {ip} after {round(dur)} s")
                        else:
                            print(f"{datetime.now():%H:%M:%S}  DOWN  {names[ip]} {ip}")
                        s["up"], s["since"] = ok, now
                time.sleep(max(0, args.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        ev_file.close()
    mins = (time.time() - started) / 60
    print(f"\nSummary over {mins:.1f} minutes:")
    sum_path = os.path.join(OUT, f"watch-summary-{tag}.csv")
    with open(sum_path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["device", "ip", "uptime_pct", "drops", "total_down_s", "still_down"])
        for ip, s in state.items():
            pct = 100 * s["ok"] / s["samples"] if s["samples"] else 0
            still = s["up"] is False
            down = s["down_total"] + ((time.time() - s["since"]) if still else 0)
            print(f"  {names[ip]:<28} {ip:<15} up {pct:5.1f}%  drops {s['drops']:<3} down {round(down)} s{'  (still down)' if still else ''}")
            wr.writerow([names[ip], ip, f"{pct:.1f}", s["drops"], round(down), still])
    print(f"Saved: {ev_path}\n       {sum_path}")


NVR_PROBES = [
    ("device type",     "/cgi-bin/magicBox.cgi?action=getDeviceType"),
    ("firmware",        "/cgi-bin/magicBox.cgi?action=getSoftwareVersion"),
    ("current time",    "/cgi-bin/global.cgi?action=getCurrentTime"),
    ("NTP config",      "/cgi-bin/configManager.cgi?action=getConfig&name=NTP"),
    ("channel names",   "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"),
    ("camera states",   "/cgi-bin/LogicDeviceManager.cgi?action=getCameraState&uniqueChannels[0]=-1"),
    ("record mode",     "/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode"),
    ("disks",           "/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo"),
]


def cmd_nvr(args):
    password = getpass.getpass(f"Password for {args.user}@{args.host} (not saved): ")
    os.makedirs(OUT, exist_ok=True)
    folder = os.path.join(OUT, f"nvr-{stamp()}")
    os.makedirs(folder, exist_ok=True)
    print(f"\nProbing {args.host} (read-only GET requests)...\n")
    for label, path in NVR_PROBES:
        try:
            status, body = http_get(args.host, path, args.user, password, args.port)
        except OSError as e:
            print(f"  {label:<15} connection error: {e}")
            return
        text = body.decode("utf-8", "replace")
        if status == 401:
            print(f"  {label:<15} 401 login refused. Stopping now to avoid locking the account.")
            return
        result = "OK" if status == 200 else f"HTTP {status}"
        first = text.strip().splitlines()[0][:70] if text.strip() else ""
        print(f"  {label:<15} {result:<9} {first}")
        safe = re.sub(r"[^a-z0-9]+", "-", label)
        with open(os.path.join(folder, f"{safe}.txt"), "w", encoding="utf-8") as f:
            f.write(f"GET {path}\nHTTP {status}\n\n{text}")
    print(f"\nRaw answers saved in: {folder}")


def cmd_channels(args):
    """Ask the NVR for one snapshot per channel, slowly and in order.

    This is the safe way to find out which channels actually have cameras: one request
    every --delay seconds, never in parallel, so the NVR and the Wi-Fi cameras are not
    hit by a burst. Channels without a camera usually answer HTTP 400 or time out; they
    are logged and skipped, not retried.
    """
    password = getpass.getpass(f"Password for {args.user}@{args.host} (not saved): ")
    os.makedirs(OUT, exist_ok=True)
    tag = stamp()
    csv_path = os.path.join(OUT, f"channels-{tag}.csv")
    try:
        first, last = (int(x) for x in args.range.split("-"))
    except ValueError:
        print("--range must look like 1-32")
        return
    if args.only:
        chans = [int(x) for x in args.only.split(",") if x.strip()]
    else:
        chans = list(range(first, last + 1))

    # One cheap call first: a wrong password stops here, not after 32 attempts.
    status, _ = http_get(args.host, "/cgi-bin/magicBox.cgi?action=getDeviceType",
                         args.user, password, args.port, timeout=args.timeout)
    if status == 401:
        print("Login refused. Stopping now so the NVR account is not locked.")
        return
    if status != 200:
        print(f"NVR did not answer as expected (HTTP {status}).")
        return

    names = {}
    status, body = http_get(args.host, "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle",
                            args.user, password, args.port, timeout=args.timeout)
    if status == 200:
        for m in re.finditer(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)", body.decode("utf-8", "replace")):
            names[int(m.group(1)) + 1] = m.group(2).strip()

    print(f"\nProbing channels {chans[0]}–{chans[-1]} on {args.host}, {args.delay:g} s apart. "
          f"Do not open the panel's camera pages meanwhile.\n")
    print(f"{'Ch':>3}  {'Result':<12} {'HTTP':>5} {'Bytes':>8} {'ms':>6}  NVR channel name")
    print("-" * 72)
    good = []
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["channel", "result", "http", "bytes", "ms", "name", "time"])
        for ch in chans:
            t0 = time.perf_counter()
            try:
                status, body = http_get(args.host, f"/cgi-bin/snapshot.cgi?channel={ch}",
                                        args.user, password, args.port, timeout=args.timeout)
                ms = round((time.perf_counter() - t0) * 1000)
                ok = status == 200 and body[:2] == b"\xff\xd8"
                note = "OK" if ok else f"HTTP {status}"
                if status == 401:
                    print("Login refused mid-way. Stopping now.")
                    break
                if ok:
                    good.append(ch)
                print(f"{ch:>3}  {note:<12} {status:>5} {len(body):>8} {ms:>6}  {names.get(ch, '')}")
                wr.writerow([ch, "OK" if ok else "fail", status, len(body), ms, names.get(ch, ""),
                             datetime.now().strftime("%H:%M:%S")])
            except OSError as e:
                ms = round((time.perf_counter() - t0) * 1000)
                print(f"{ch:>3}  {'timeout/error':<12} {'':>5} {'':>8} {ms:>6}  {names.get(ch, '')}")
                wr.writerow([ch, "error", "", 0, ms, names.get(ch, ""),
                             datetime.now().strftime("%H:%M:%S")])
            f.flush()
            time.sleep(args.delay)
    print("-" * 72)
    print(f"Channels with a picture: {', '.join(map(str, good)) if good else 'none'}")
    print(f"Saved: {csv_path}")
    print("Next: put those channel numbers into devices.json (Ağ cihazları → Cihaz envanteri).")


# ---------------------------------------------------------------- live events (Dahua eventManager attach)

EVENT_CODES = ("VideoMotion,CrossLineDetection,CrossRegionDetection,SmartMotionHuman,"
               "SmartMotionVehicle,AlarmLocal,VideoLoss")


def _stream_auth(path, user, password, challenge):
    """Digest (or Basic) header for a streaming GET."""
    if not challenge.lower().startswith("digest"):
        import base64
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    a = _parse_auth(challenge)
    realm, nonce, qop = a.get("realm", ""), a.get("nonce", ""), a.get("qop", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"GET:{path}".encode()).hexdigest()
    if "auth" in qop:
        cnonce, nc = os.urandom(8).hex(), "00000001"
        resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        h = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{path}", '
             f'qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"')
    else:
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        h = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{path}", response="{resp}"'
    if "opaque" in a:
        h += f', opaque="{a["opaque"]}"'
    return h


def open_event_stream(host, path, user, password, port=80, timeout=90):
    """Open a long-lived GET and return (conn, response). One auth attempt only."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.request("GET", path)
    r = conn.getresponse()
    if r.status == 401:
        hdr = r.getheader("WWW-Authenticate", "")
        r.read()
        conn.close()
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", path, headers={"Authorization": _stream_auth(path, user, password, hdr)})
        r = conn.getresponse()
    if r.status != 200:
        r.read()
        conn.close()
        raise OSError(f"HTTP {r.status}")
    return conn, r


def iter_events(r, chunk_size=4096):
    """Yield parsed JSON objects from a Dahua multipart event stream."""
    buf = b""
    while True:
        chunk = r.read(chunk_size)
        if not chunk:
            return
        buf += chunk
        while True:
            i = buf.find(b"--myboundary")
            if i < 0:
                if len(buf) > 1_000_000:
                    buf = buf[-4096:]
                break
            j = buf.find(b"\r\n\r\n", i)
            if j < 0:
                break
            head = buf[i:j].decode("latin-1", "replace")
            m = re.search(r"Content-Length:\s*(\d+)", head, re.I)
            if not m:
                buf = buf[j + 4:]
                continue
            n = int(m.group(1))
            start = j + 4
            if len(buf) < start + n:
                break
            body, buf = buf[start:start + n], buf[start + n:]
            try:
                yield json.loads(body)
            except ValueError:
                yield {"_raw": body.decode("latin-1", "replace")}


def event_summary(ev):
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    code = str(ev.get("code") or ev.get("Code") or "?")
    action = str(ev.get("action") or ev.get("Action") or "")
    ch = data.get("Channel", data.get("channel", ""))
    obj = data.get("ObjectType", data.get("ObjectTypes", data.get("objectType", "")))
    name = data.get("Name", data.get("name", ""))
    return code, action, ch, obj, name


def cmd_events(args):
    """Listen to the NVR's smart-event push stream for a few minutes.

    Dahua NVRs push events (motion, tripwire, intrusion, human/vehicle) on a long-lived
    HTTP GET. This prints them and saves every raw payload, so we can see exactly what
    the box sends before building the panel feature.
    """
    password = getpass.getpass(f"Password for {args.user}@{args.host} (not saved): ")
    os.makedirs(OUT, exist_ok=True)
    codes = re.sub(r"\s+", "", args.codes) or EVENT_CODES
    path = f"/cgi-bin/eventManager.cgi?action=attach&codes=[{codes}]&heartbeat=5"
    csvless = os.path.join(OUT, f"events-{stamp()}.jsonl")
    print(f"\nListening on {args.host} for {args.minutes:g} min. Walk in front of a camera / drive past one.\n")
    try:
        conn, r = open_event_stream(args.host, path, args.user, password, args.port)
    except OSError as e:
        print(f"Could not open the event stream: {e}")
        print("If this says HTTP 400, the NVR may not support eventManager attach or needs different codes.")
        return
    deadline = time.time() + args.minutes * 60
    seen = {}
    lines = 0
    print(f"{'Time':<10} {'Code':<22} {'Action':<7} {'Ch':<4} {'Object':<8} Name")
    print("-" * 78)
    with open(csvless, "w", encoding="utf-8") as f:
        try:
            for ev in iter_events(r):
                if time.time() > deadline:
                    break
                code, action, ch, obj, name = event_summary(ev)
                if code.lower() == "heartbeat":
                    continue
                seen[code] = seen.get(code, 0) + 1
                lines += 1
                print(f"{datetime.now():%H:%M:%S}  {code:<22} {action:<7} {str(ch):<4} {str(obj):<8} {name}")
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush()
        except (OSError, KeyboardInterrupt):
            pass
        finally:
            try:
                r.close()
                conn.close()
            except OSError:
                pass
    print("-" * 78)
    if lines:
        print("Event types: " + ", ".join(f"{k} ×{v}" for k, v in seen.items()))
    else:
        print("No events in that window. Either nobody moved, the smart event is not enabled,")
        print("or the NVR does not support this stream (check Event → Smart Motion / IVS).")
    print(f"Raw payloads: {csvless}")


def cmd_snmp(args):
    for community in args.community.split(","):
        res = snmp_get(args.host, community.strip())
        print(f"{args.host} community '{community.strip()}': {res if res else 'no answer'}")


def main():
    p = argparse.ArgumentParser(description="Adalya site test toolkit (read-only)")
    p.add_argument("--devices", default=os.path.join(HERE, "devices.json"))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="one-shot health check")
    c.add_argument("--community", default="public")
    c.add_argument("--no-snmp", action="store_true")
    c.add_argument("--ports-always", action="store_true", help="test ports even if ping fails")
    c.set_defaults(func=cmd_check)

    w = sub.add_parser("watch", help="continuous ping log with drop durations")
    w.add_argument("--only", help="comma list of last octets or IPs, e.g. 106,108,12")
    w.add_argument("--interval", type=float, default=1.0)
    w.add_argument("--timeout", type=int, default=800, help="ping timeout in ms")
    w.set_defaults(func=cmd_watch)

    n = sub.add_parser("nvr", help="probe the Dahua NVR HTTP API")
    n.add_argument("--host", default="192.168.0.254")
    n.add_argument("--port", type=int, default=80)
    n.add_argument("--user", default="admin")
    n.set_defaults(func=cmd_nvr)

    c = sub.add_parser("channels", help="find which NVR channels have a camera (one at a time)")
    c.add_argument("--host", default="192.168.0.254")
    c.add_argument("--port", type=int, default=80)
    c.add_argument("--user", default="admin")
    c.add_argument("--range", default="1-32", help="channel range, e.g. 1-32")
    c.add_argument("--only", help="comma list of channels instead of the range")
    c.add_argument("--delay", type=float, default=2.0, help="seconds between channels (default 2)")
    c.add_argument("--timeout", type=float, default=6.0, help="per-request timeout in seconds")
    c.set_defaults(func=cmd_channels)

    e = sub.add_parser("events", help="listen to NVR person/vehicle/motion events for a few minutes")
    e.add_argument("--host", default="192.168.0.254")
    e.add_argument("--port", type=int, default=80)
    e.add_argument("--user", default="admin")
    e.add_argument("--minutes", type=float, default=10, help="how long to listen (default 10)")
    e.add_argument("--codes", default=EVENT_CODES, help="comma list of Dahua event codes")
    e.set_defaults(func=cmd_events)

    s = sub.add_parser("snmp", help="test SNMP on a device")
    s.add_argument("--host", required=True)
    s.add_argument("--community", default="public,private")
    s.set_defaults(func=cmd_snmp)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
