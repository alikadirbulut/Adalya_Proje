"""Adalya Panel — local management site for the Adalya cameras and network.
Python standard library only. Dedicated to the memory of Esin Noğan.

  python adalya_panel.py --demo            simulated site and NVR, to try every page off-site
  python adalya_panel.py                   live, http://127.0.0.1:8080
  python adalya_panel.py --lan             also reachable from other screens on the site network
  python adalya_panel.py --nvr-user admin  connect to the NVR at start (asks for the password)

First live start asks you to set an admin password in the browser.
Devices are only read, never changed. Data lives in the "data" folder next to this file.
"""

import argparse
import csv
import getpass
import hashlib
import hmac
import io
import ipaddress
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import adalya_test as kit
from nvr import TIME_FMT, TIME_RE, DemoNvr, Nvr, NvrAuthError, NvrError

VERSION = "2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEVICES = os.path.join(HERE, "devices.json")
HTML_PATH = os.path.join(HERE, "panel.html")
PLAN_PATH = os.path.join(HERE, "site-plan.png")
DATA = os.path.join(HERE, "data")
DB_PATH = os.path.join(DATA, "panel.db")
CFG_PATH = os.path.join(DATA, "config.json")
SITE_PATH = os.path.join(HERE, "site.json")
MEDIA = os.path.join(HERE, "media")
DEMO_USER = {"username": "demo", "name": "Demo", "role": "admin"}
USERNAME_RE = re.compile(r"^[a-z0-9._-]{2,32}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SITE_DEFAULT = {
    "site_name": "Adalya sitesi", "support_contact": "",
    "memorial": {"name": "Esin Noğan", "birth": "", "death": "", "profession": "", "note": "",
                 "created_by": "", "photo": "", "photo_x": 50, "photo_y": 35},
    "labels": [],
}

lock = threading.Lock()
devices, state, sessions, known_paths = [], {}, {}, set()
cfg = {}
site = {}
nvr = None
nvr_state = {"info": None, "error": None, "checked": None}
login_fail = {"count": 0, "until": 0.0}
started = time.time()
args = None


# ---------------------------------------------------------------- storage

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS events (ip TEXT, name TEXT, down_at REAL, up_at REAL, seconds REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS notes (ts REAL, user TEXT, device TEXT, text TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS audit (ts REAL, user TEXT, role TEXT, action TEXT, detail TEXT)")
    return conn


def load_cfg():
    global cfg
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg.setdefault("nvr_host", "192.168.0.23")
    cfg.setdefault("nvr_user", "admin")
    users = cfg.setdefault("users", {})
    if "admin" in cfg and isinstance(cfg["admin"], dict) and "salt" in cfg["admin"]:
        users.setdefault("yonetici", {"name": "Yönetici", "role": "admin", "pw": cfg.pop("admin")})
    if "staff" in cfg and isinstance(cfg["staff"], dict):
        users.setdefault("gorevli", {"name": "Görevli", "role": "staff", "pw": cfg.pop("staff")})
    for old in ("memorial_note", "support_contact"):
        cfg.pop(old, None)


def load_site():
    global site
    data = {}
    if os.path.exists(SITE_PATH):
        with open(SITE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    site = {**SITE_DEFAULT, **data}
    site["memorial"] = {**SITE_DEFAULT["memorial"], **data.get("memorial", {})}
    site["labels"] = [l for l in data.get("labels", []) if isinstance(l, dict)]


def save_site():
    tmp = SITE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(site, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SITE_PATH)


def photo_path():
    name = site["memorial"].get("photo") or ""
    p = os.path.join(MEDIA, os.path.basename(name))
    return p if name and os.path.isfile(p) else None


def site_payload(full):
    if not site:
        load_site()
    m = site["memorial"]
    out = {"site_name": site["site_name"],
           "memorial": {"name": m["name"], "birth": m["birth"], "death": m["death"]}}
    if full:
        out = {**out, "support_contact": site["support_contact"], "labels": site["labels"],
               "memorial": {**m, "has_photo": bool(photo_path())}}
    return out


def admins():
    return [u for u, r in cfg["users"].items() if r["role"] == "admin"]


def tr_genitive(name):
    """Esin Noğan -> Esin Noğan'ın (Turkish possessive suffix by vowel harmony)."""
    name = name.strip()
    if not name:
        return ""
    vowels = [c for c in name.lower() if c in "aıoueiöü"]
    last = vowels[-1] if vowels else "e"
    v = {"a": "ı", "ı": "ı", "o": "u", "u": "u", "e": "i", "i": "i", "ö": "ü", "ü": "ü"}[last]
    return f"{name}'{'n' if name[-1].lower() in 'aıoueiöü' else ''}{v}n"


def save_cfg():
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)


def hash_pw(pw, salt=None):
    salt = salt or os.urandom(16).hex()
    return {"salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()}


def check_pw(pw, rec):
    return bool(rec) and hmac.compare_digest(hash_pw(pw, rec["salt"])["hash"], rec["hash"])


def audit(user, action, detail=""):
    conn = db()
    conn.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (time.time(), user["name"], user["role"], action, detail))
    conn.commit()
    conn.close()


def load_devices():
    global devices
    with open(DEVICES, encoding="utf-8") as f:
        devices = json.load(f)
    for d in devices:
        state.setdefault(d["ip"], {"up": None, "since": None, "misses": 0, "first_miss": None,
                                   "rtt": None, "mac_seen": "", "checked": None})


def save_devices():
    tmp = DEVICES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join("  " + json.dumps(d, ensure_ascii=False) for d in devices) + "\n]\n")
    os.replace(tmp, DEVICES)


def channel_names():
    return {d["channel"]: d["name"] for d in devices if d.get("channel")}


# ---------------------------------------------------------------- monitoring

_demo_down_until = {}


def demo_ping(ip):
    now = time.time()
    if ip.startswith("192.168.1.") or ip in ("192.168.0.128", "192.168.0.129", "192.168.0.10"):
        return False, None
    rates = {"192.168.0.106": 0.012, "192.168.0.108": 0.008, "192.168.0.12": 0.004}
    if ip in rates:
        if _demo_down_until.get(ip, 0) > now:
            return False, None
        if random.random() < rates[ip]:
            _demo_down_until[ip] = now + random.uniform(20, 40)
            return False, None
        return True, random.randint(40, 180)
    return True, random.randint(2, 30)


def poll_once():
    probe = (lambda d: demo_ping(d["ip"])) if args.demo else (lambda d: kit.ping(d["ip"], 800))
    targets = list(devices)
    with ThreadPoolExecutor(max_workers=32) as ex:
        results = list(ex.map(probe, targets))
    now = time.time()
    conn = db()
    with lock:
        for d, (ok, rtt) in zip(targets, results):
            s = state.setdefault(d["ip"], {"up": None, "since": None, "misses": 0, "first_miss": None,
                                           "rtt": None, "mac_seen": "", "checked": None})
            s["checked"] = now
            if ok:
                s["rtt"] = rtt
                if s["up"] is False:
                    conn.execute("INSERT INTO events VALUES (?,?,?,?,?)", (d["ip"], d["name"], s["since"], now, now - s["since"]))
                if s["up"] is not True:
                    s["up"], s["since"] = True, now
                s["misses"], s["first_miss"] = 0, None
            else:
                s["misses"] += 1
                if s["first_miss"] is None:
                    s["first_miss"] = now
                if s["up"] is None or (s["up"] and s["misses"] >= args.misses):
                    s["up"], s["since"] = False, s["first_miss"]
    conn.commit()
    conn.close()


def refresh_arp():
    table = {d["ip"]: kit.norm_mac(d.get("mac")) for d in devices if state[d["ip"]]["up"]} if args.demo else kit.arp_table()
    with lock:
        for d in devices:
            state[d["ip"]]["mac_seen"] = table.get(d["ip"], "")


def refresh_nvr():
    global nvr
    client = nvr
    if not client:
        return
    try:
        info = client.info()
        nvr_state.update(info=info, error=None, checked=time.time())
    except NvrAuthError as e:
        nvr_state.update(error=str(e), checked=time.time())
    except NvrError as e:
        nvr_state.update(error=str(e), checked=time.time())


def poller():
    last_arp = last_nvr = 0
    while True:
        t0 = time.time()
        try:
            poll_once()
            if t0 - last_arp > 60:
                refresh_arp()
                last_arp = t0
            if nvr and not nvr.locked and t0 - last_nvr > 300:
                refresh_nvr()
                last_nvr = t0
        except Exception as e:
            print("poll error:", e)
        time.sleep(max(0.2, args.interval - (time.time() - t0)))


# ---------------------------------------------------------------- helpers

def day_start(ts):
    d = datetime.fromtimestamp(ts)
    return datetime(d.year, d.month, d.day).timestamp()


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime(TIME_FMT) if ts else ""


def tparam(qs, key):
    v = qs.get(key, "")
    if not TIME_RE.match(v):
        raise ValueError("Tarih/saat biçimi YYYY-AA-GG SS:DD:ss olmalı")
    return v


def dparam(qs, key, default):
    v = qs.get(key) or default
    try:
        return datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Tarih biçimi YYYY-AA-GG olmalı")


def chparam(qs):
    try:
        ch = int(qs.get("ch", ""))
    except ValueError:
        raise ValueError("Kanal numarası geçersiz")
    if not 1 <= ch <= 64:
        raise ValueError("Kanal 1–64 arasında olmalı")
    return ch


def need_nvr():
    if not nvr:
        raise NvrError("NVR bağlı değil. Ayarlar sayfasından bağlanın.")
    return nvr


def csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M")


def events_between(start_ts, end_ts):
    conn = db()
    rows = conn.execute("SELECT name, ip, down_at, up_at, seconds FROM events WHERE down_at >= ? AND down_at < ? ORDER BY down_at",
                        (start_ts, end_ts)).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------- payloads

def status_payload():
    now = time.time()
    window = min(86400, now - started) or 1
    conn = db()
    rows = conn.execute("SELECT ip, name, down_at, up_at, seconds FROM events WHERE up_at > ? ORDER BY up_at DESC",
                        (now - 86400,)).fetchall()
    conn.close()
    today = day_start(now)
    out = []
    with lock:
        for d in devices:
            s = state.get(d["ip"], {})
            evs = [r for r in rows if r[0] == d["ip"]]
            down = sum(min(r[3], now) - max(r[2], now - window) for r in evs if r[3] > now - window)
            if s.get("up") is False and s.get("since"):
                down += now - max(s["since"], now - window)
            want, seen = kit.norm_mac(d.get("mac")), s.get("mac_seen", "")
            mac = "unknown" if not seen or not want else ("ok" if seen == want else "wrong")
            out.append({
                "name": d["name"], "ip": d["ip"], "type": d["type"], "hidden": d.get("hidden", False),
                "channel": d.get("channel"), "x": d.get("x"), "y": d.get("y"),
                "up": s.get("up"), "since": s.get("since"), "rtt": s.get("rtt"), "mac": mac,
                "drops_today": sum(1 for r in evs if r[3] >= today),
                "uptime": round(100 * max(0.0, 1 - down / window), 1),
            })
    events = [{"ip": r[0], "name": r[1], "down_at": r[2], "up_at": r[3], "seconds": round(r[4])} for r in rows[:60]]
    for d in out:
        if d["up"] is False and d["since"] and not d["hidden"]:
            events.append({"ip": d["ip"], "name": d["name"], "down_at": d["since"], "up_at": None, "seconds": round(now - d["since"])})
    events.sort(key=lambda e: e["up_at"] or now + 1, reverse=True)
    return {"now": now, "started": started, "demo": args.demo, "devices": out, "events": events[:60],
            "nvr": nvr_summary(), "has_plan": bool(plan_file()[0]),
            "plan_ver": int(os.path.getmtime(plan_file()[0])) if plan_file()[0] else 0}


def nvr_summary():
    info = nvr_state.get("info") or {}
    return {"connected": bool(nvr) and not nvr.locked, "demo": bool(nvr and nvr.demo),
            "host": (nvr.host if nvr else cfg.get("nvr_host")), "user": (nvr.user if nvr else cfg.get("nvr_user")),
            "error": nvr_state.get("error"), "checked": nvr_state.get("checked"),
            "type": info.get("type"), "firmware": info.get("firmware"), "time": info.get("time"),
            "disk_pct": info.get("disk_pct"), "disk_total_gb": info.get("disk_total_gb"),
            "disk_states": info.get("disk_states", []), "channels": info.get("channels", {})}


# ---------------------------------------------------------------- routes

ROUTES = {}


def route(method, path, need="user"):
    def deco(fn):
        fn.need = need
        ROUTES[(method, path)] = fn
        return fn
    return deco


@route("GET", "/", need="none")
def r_index(h, qs, user):
    with open(HTML_PATH, encoding="utf-8") as f:
        h.raw(200, f.read().encode("utf-8"), "text/html; charset=utf-8")


@route("GET", "/api/me", need="none")
def r_me(h, qs, user):
    h.json(200, {"setup_needed": not args.demo and not admins(), "user": user, "demo": args.demo,
                 "site": site_payload(bool(user)), "version": VERSION, "lan": args.lan})


def clean_user_fields(b, creating):
    username = str(b.get("username", "")).strip().lower()
    name = str(b.get("name", "")).strip()[:40]
    role = b.get("role", "staff")
    pw = str(b.get("password", ""))
    if not USERNAME_RE.match(username):
        raise ValueError("Kullanıcı adı 2–32 karakter olmalı: küçük harf, rakam, nokta, tire (ör. ahmet.bekci)")
    if not name:
        raise ValueError("Ad soyad gerekli")
    if role not in ("admin", "staff"):
        raise ValueError("Rol geçersiz")
    if (creating or pw) and len(pw) < 8:
        raise ValueError("Şifre en az 8 karakter olmalı")
    return username, name, role, pw


@route("POST", "/api/setup", need="none")
def r_setup(h, qs, user):
    if args.demo or admins():
        return h.json(400, {"error": "Kurulum zaten yapılmış"})
    username, name, _, pw = clean_user_fields({**h.body(), "role": "admin"}, True)
    cfg["users"][username] = {"name": name, "role": "admin", "pw": hash_pw(pw)}
    save_cfg()
    u = {"username": username, "name": name, "role": "admin"}
    h.start_session(u)
    audit(u, "Kurulum", f"İlk yönetici: {username}")


@route("POST", "/api/login", need="none")
def r_login(h, qs, user):
    now = time.time()
    if login_fail["until"] > now:
        return h.json(429, {"error": f"Çok fazla hatalı deneme. {int(login_fail['until'] - now)} sn sonra tekrar deneyin."})
    b = h.body()
    username, pw = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    rec = cfg["users"].get(username)
    if not rec or not check_pw(pw, rec.get("pw")):
        login_fail["count"] += 1
        if login_fail["count"] >= 5:
            login_fail.update(count=0, until=now + 30)
        return h.json(401, {"error": "Kullanıcı adı veya şifre hatalı"})
    login_fail["count"] = 0
    u = {"username": username, "name": rec["name"], "role": rec["role"]}
    h.start_session(u)
    audit(u, "Giriş", "")


@route("GET", "/api/users", need="admin")
def r_users(h, qs, user):
    h.json(200, {"users": [{"username": k, "name": v["name"], "role": v["role"]}
                           for k, v in sorted(cfg["users"].items(), key=lambda kv: (kv[1]["role"] != "admin", kv[1]["name"]))]})


@route("POST", "/api/users", need="admin")
def r_user_save(h, qs, user):
    if args.demo:
        return h.json(400, {"error": "Demo modunda kullanıcı eklenmez"})
    b = h.body()
    creating = b.get("username", "").strip().lower() not in cfg["users"]
    username, name, role, pw = clean_user_fields(b, creating)
    if not creating and role != "admin" and cfg["users"][username]["role"] == "admin" and admins() == [username]:
        return h.json(400, {"error": "Son yöneticinin yetkisi kaldırılamaz"})
    rec = cfg["users"].get(username, {})
    rec.update(name=name, role=role)
    if pw:
        rec["pw"] = hash_pw(pw)
        for tok in [t for t, s in sessions.items() if s["username"] == username and username != user["username"]]:
            sessions.pop(tok, None)
    cfg["users"][username] = rec
    save_cfg()
    audit(user, "Kullanıcı eklendi" if creating else "Kullanıcı güncellendi", f"{username} ({'yönetici' if role == 'admin' else 'görevli'}){' · şifre değişti' if pw and not creating else ''}")
    h.json(200, {"ok": True})


@route("POST", "/api/users/delete", need="admin")
def r_user_delete(h, qs, user):
    username = str(h.body().get("username", "")).strip().lower()
    if username not in cfg["users"]:
        return h.json(404, {"error": "Kullanıcı bulunamadı"})
    if username == user["username"]:
        return h.json(400, {"error": "Kendi hesabınızı silemezsiniz"})
    if admins() == [username]:
        return h.json(400, {"error": "Son yönetici silinemez"})
    cfg["users"].pop(username)
    save_cfg()
    for tok in [t for t, s in sessions.items() if s["username"] == username]:
        sessions.pop(tok, None)
    audit(user, "Kullanıcı silindi", username)
    h.json(200, {"ok": True})


@route("POST", "/api/me/password")
def r_my_password(h, qs, user):
    if args.demo:
        return h.json(400, {"error": "Demo modunda şifre değiştirilmez"})
    b = h.body()
    rec = cfg["users"].get(user["username"])
    if not rec or not check_pw(str(b.get("current", "")), rec["pw"]):
        return h.json(400, {"error": "Mevcut şifre hatalı"})
    if len(str(b.get("new", ""))) < 8:
        return h.json(400, {"error": "Yeni şifre en az 8 karakter olmalı"})
    rec["pw"] = hash_pw(str(b["new"]))
    save_cfg()
    audit(user, "Şifresini değiştirdi", "")
    h.json(200, {"ok": True})


@route("POST", "/api/logout", need="none")
def r_logout(h, qs, user):
    sessions.pop(h.cookie_token(), None)
    h.json(200, {"ok": True}, {"Set-Cookie": "adalya_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})


@route("GET", "/api/status")
def r_status(h, qs, user):
    h.json(200, status_payload())


@route("GET", "/api/device")
def r_device(h, qs, user):
    ip = qs.get("ip", "")
    now = time.time()
    conn = db()
    rows = conn.execute("SELECT down_at, up_at FROM events WHERE ip=? AND up_at > ? ORDER BY down_at", (ip, now - 86400)).fetchall()
    notes = conn.execute("SELECT ts, user, text FROM notes WHERE device=? ORDER BY ts DESC LIMIT 10", (ip,)).fetchall()
    conn.close()
    spans = [list(r) for r in rows]
    with lock:
        s = state.get(ip) or {}
        if s.get("up") is False and s.get("since"):
            spans.append([s["since"], now])
    d = next((x for x in devices if x["ip"] == ip), {})
    h.json(200, {"ip": ip, "name": d.get("name"), "mac_expected": d.get("mac", ""), "mac_seen": s.get("mac_seen", ""),
                 "spans": spans, "now": now, "started": started,
                 "notes": [{"ts": n[0], "user": n[1], "text": n[2]} for n in notes]})


@route("POST", "/api/position", need="admin")
def r_position(h, qs, user):
    b = h.body()
    x, y = b.get("x"), b.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return h.json(400, {"error": "Konum geçersiz"})
    with lock:
        for d in devices:
            if d["ip"] == b.get("ip"):
                d["x"], d["y"] = round(max(0, min(90, x)), 1), round(max(0, min(90, y)), 1)
                save_devices()
                return h.json(200, {"ok": True})
    h.json(404, {"error": "Cihaz bulunamadı"})


@route("GET", "/api/events")
def r_events(h, qs, user):
    a = dparam(qs, "from", datetime.now().strftime("%Y-%m-%d"))
    b = dparam(qs, "to", datetime.now().strftime("%Y-%m-%d")) + timedelta(days=1)
    rows = events_between(a.timestamp(), b.timestamp())
    h.json(200, {"events": [{"name": r[0], "ip": r[1], "down_at": r[2], "up_at": r[3], "seconds": round(r[4])} for r in rows]})


@route("GET", "/api/events.csv")
def r_events_csv(h, qs, user):
    a = dparam(qs, "from", datetime.now().strftime("%Y-%m-%d"))
    b = dparam(qs, "to", datetime.now().strftime("%Y-%m-%d")) + timedelta(days=1)
    rows = events_between(a.timestamp(), b.timestamp())
    audit(user, "Olayları indirdi", f"{a:%Y-%m-%d} – {b - timedelta(days=1):%Y-%m-%d}, {len(rows)} satır")
    data = csv_bytes(["cihaz", "ip", "kopma", "geri_gelme", "sure_sn"],
                     [[r[0], r[1], fmt_ts(r[2]), fmt_ts(r[3]), round(r[4])] for r in rows])
    h.download(data, f"adalya-olaylar-{a:%Y%m%d}-{b - timedelta(days=1):%Y%m%d}.csv", "text/csv; charset=utf-8")


@route("GET", "/api/notes")
def r_notes(h, qs, user):
    conn = db()
    rows = conn.execute("SELECT ts, user, device, text FROM notes ORDER BY ts DESC LIMIT 300").fetchall()
    conn.close()
    names = {d["ip"]: d["name"] for d in devices}
    h.json(200, {"notes": [{"ts": r[0], "user": r[1], "device": r[2], "device_name": names.get(r[2], ""), "text": r[3]} for r in rows]})


@route("POST", "/api/notes")
def r_add_note(h, qs, user):
    b = h.body()
    text = str(b.get("text", "")).strip()[:2000]
    device = str(b.get("device", ""))[:40]
    if not text:
        return h.json(400, {"error": "Not boş olamaz"})
    conn = db()
    conn.execute("INSERT INTO notes VALUES (?,?,?,?)", (time.time(), user["name"], device, text))
    conn.commit()
    conn.close()
    h.json(200, {"ok": True})


@route("GET", "/api/audit", need="admin")
def r_audit(h, qs, user):
    conn = db()
    rows = conn.execute("SELECT ts, user, role, action, detail FROM audit ORDER BY ts DESC LIMIT 300").fetchall()
    conn.close()
    h.json(200, {"audit": [{"ts": r[0], "user": r[1], "role": r[2], "action": r[3], "detail": r[4]} for r in rows]})


@route("GET", "/api/nvr/status")
def r_nvr_status(h, qs, user):
    h.json(200, nvr_summary())


@route("POST", "/api/nvr/connect", need="admin")
def r_nvr_connect(h, qs, user):
    global nvr
    if args.demo:
        return h.json(400, {"error": "Demo modunda gerçek NVR'ye bağlanılmaz"})
    b = h.body()
    host, uname, pw = str(b.get("host", "")).strip(), str(b.get("user", "")).strip(), str(b.get("password", ""))
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return h.json(400, {"error": "NVR adresi IP olmalı (ör. 192.168.0.23)"})
    if not ip.is_private:
        return h.json(400, {"error": "Sadece yerel ağdaki bir NVR'ye bağlanılabilir"})
    if not uname or not pw:
        return h.json(400, {"error": "Kullanıcı adı ve şifre gerekli"})
    client = Nvr(host, uname, pw)
    info = client.info()
    nvr = client
    nvr_state.update(info=info, error=None, checked=time.time())
    cfg["nvr_host"], cfg["nvr_user"] = host, uname
    save_cfg()
    audit(user, "NVR bağlantısı", f"{uname}@{host}")
    h.json(200, nvr_summary())


@route("POST", "/api/nvr/disconnect", need="admin")
def r_nvr_disconnect(h, qs, user):
    global nvr
    if args.demo:
        return h.json(400, {"error": "Demo modunda kapatılamaz"})
    nvr = None
    nvr_state.update(info=None, error=None)
    h.json(200, nvr_summary())


@route("GET", "/api/nvr/snapshot")
def r_snapshot(h, qs, user):
    ctype, data = need_nvr().snapshot(chparam(qs))
    h.raw(200, data, ctype)


@route("GET", "/api/nvr/records")
def r_records(h, qs, user):
    ch, start, end = chparam(qs), tparam(qs, "start"), tparam(qs, "end")
    files = need_nvr().records(ch, start, end)
    with lock:
        if len(known_paths) > 20000:
            known_paths.clear()
        known_paths.update(f["path"] for f in files)
    h.json(200, {"files": files})


@route("GET", "/api/nvr/download")
def r_download(h, qs, user):
    ch, path = chparam(qs), qs.get("path", "")
    if path not in known_paths or ".." in path:
        raise ValueError("Bu dosya önce Kayıtlar sayfasında aranmalı")
    length, chunks = need_nvr().stream_file(path)
    name = f"{re.sub(r'[^\w-]+', '-', channel_names().get(ch, f'kanal{ch}')).strip('-')}_kanal{ch}_{os.path.basename(path)}"
    audit(user, "Kayıt indirdi", f"Kanal {ch} · {path}")
    h.stream(chunks, name, length)


@route("GET", "/api/nvr/logs")
def r_nvr_logs(h, qs, user):
    h.json(200, {"logs": need_nvr().logs(tparam(qs, "start"), tparam(qs, "end"))})


@route("GET", "/api/nvr/logs.csv")
def r_nvr_logs_csv(h, qs, user):
    start, end = tparam(qs, "start"), tparam(qs, "end")
    rows = need_nvr().logs(start, end)
    audit(user, "NVR loglarını indirdi", f"{start} – {end}, {len(rows)} satır")
    data = csv_bytes(["zaman", "tur", "kullanici", "ayrinti"], [[r["time"], r["type"], r["user"], r["detail"]] for r in rows])
    h.download(data, f"adalya-nvr-log-{start[:10]}-{end[:10]}.csv", "text/csv; charset=utf-8")


@route("GET", "/api/export.zip")
def r_export(h, qs, user):
    try:
        days = max(1, min(90, int(qs.get("days", "7"))))
    except ValueError:
        raise ValueError("Gün sayısı geçersiz")
    end = datetime.now()
    start = end - timedelta(days=days)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        rows = events_between(start.timestamp(), end.timestamp() + 1)
        z.writestr("olaylar.csv", csv_bytes(["cihaz", "ip", "kopma", "geri_gelme", "sure_sn"],
                                            [[r[0], r[1], fmt_ts(r[2]), fmt_ts(r[3]), round(r[4])] for r in rows]))
        conn = db()
        notes = conn.execute("SELECT ts, user, device, text FROM notes WHERE ts >= ? ORDER BY ts", (start.timestamp(),)).fetchall()
        aud = conn.execute("SELECT ts, user, role, action, detail FROM audit WHERE ts >= ? ORDER BY ts", (start.timestamp(),)).fetchall()
        conn.close()
        z.writestr("bakim-notlari.csv", csv_bytes(["zaman", "kisi", "cihaz", "not"], [[fmt_ts(n[0]), n[1], n[2], n[3]] for n in notes]))
        z.writestr("disa-aktarim-kaydi.csv", csv_bytes(["zaman", "kisi", "rol", "islem", "ayrinti"],
                                                       [[fmt_ts(a[0]), a[1], a[2], a[3], a[4]] for a in aud]))
        status = status_payload()
        z.writestr("cihaz-durumu.csv", csv_bytes(["cihaz", "ip", "tur", "kanal", "durum", "bugun_kopma", "erisim_24s"],
                                                 [[d["name"], d["ip"], d["type"], d.get("channel") or "",
                                                   "acik" if d["up"] else "kapali" if d["up"] is False else "bilinmiyor",
                                                   d["drops_today"], d["uptime"]] for d in status["devices"]]))
        with open(DEVICES, encoding="utf-8") as f:
            z.writestr("devices.json", f.read())
        nvr_note = "NVR bağlı değildi; NVR logları pakete eklenmedi."
        if nvr and not nvr.locked:
            try:
                logs = nvr.logs(start.strftime(TIME_FMT), end.strftime(TIME_FMT))
                z.writestr("nvr-log.csv", csv_bytes(["zaman", "tur", "kullanici", "ayrinti"],
                                                    [[r["time"], r["type"], r["user"], r["detail"]] for r in logs]))
                z.writestr("nvr-bilgi.json", json.dumps(nvr_summary(), ensure_ascii=False, indent=2))
                nvr_note = f"NVR logları eklendi ({len(logs)} satır)."
            except (NvrError, NvrAuthError) as e:
                nvr_note = f"NVR logları alınamadı: {e}"
        z.writestr("BENI-OKU.txt", (
            f"Adalya sitesi dışa aktarım paketi\nOluşturan: {user['name']} ({user['role']})\n"
            f"Zaman: {end:%Y-%m-%d %H:%M}\nKapsam: son {days} gün ({start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M})\n"
            f"{nvr_note}\nVideo kayıtları bu pakette yoktur; Kayıtlar sayfasından kamera ve saat seçerek indirilir.\n\n"
            "Bu dosyalar kişisel veri içerebilir (KVKK). Yalnızca yetkili kişilerle paylaşın, işiniz bitince silin.\n\n"
            f"{tr_genitive(site['memorial']['name'])} anısına\n").encode("utf-8"))
    audit(user, "Tam paket indirdi", f"Son {days} gün")
    h.download(buf.getvalue(), f"adalya-disa-aktarim-{stamp()}.zip", "application/zip")


@route("POST", "/api/site", need="admin")
def r_site(h, qs, user):
    b = h.body()
    changed = []
    if "site_name" in b:
        site["site_name"] = str(b["site_name"]).strip()[:60] or "Adalya sitesi"
        changed.append("site adı")
    if "support_contact" in b:
        site["support_contact"] = str(b["support_contact"])[:300]
        changed.append("destek iletişimi")
    if isinstance(b.get("memorial"), dict):
        m, src = site["memorial"], b["memorial"]
        for k, limit in (("name", 80), ("profession", 80), ("note", 4000), ("created_by", 120)):
            if k in src:
                m[k] = str(src[k])[:limit]
        for k in ("birth", "death"):
            if k in src:
                v = str(src[k])
                if v and not DATE_RE.match(v):
                    raise ValueError("Tarih biçimi YYYY-AA-GG olmalı")
                m[k] = v
        for k in ("photo_x", "photo_y"):
            if k in src:
                try:
                    m[k] = max(0, min(100, int(src[k])))
                except (TypeError, ValueError):
                    raise ValueError("Fotoğraf konumu 0–100 olmalı")
        changed.append("anma bölümü")
    if isinstance(b.get("labels"), list):
        labels = []
        for l in b["labels"][:40]:
            if not isinstance(l, dict) or not str(l.get("text", "")).strip():
                continue
            try:
                x, y = float(l.get("x", 45)), float(l.get("y", 45))
            except (TypeError, ValueError):
                raise ValueError("Etiket konumu geçersiz")
            labels.append({"text": str(l["text"]).strip()[:30], "x": round(max(0, min(90, x)), 1), "y": round(max(0, min(90, y)), 1)})
        site["labels"] = labels
        changed.append("harita etiketleri")
    save_site()
    if changed:
        audit(user, "Ayarlar", ", ".join(changed))
    h.json(200, {"ok": True, "site": site_payload(True)})


@route("POST", "/api/memorial/photo", need="admin")
def r_photo(h, qs, user):
    data = str(h.body(max_bytes=12_000_000).get("data", ""))
    m = re.match(r"^data:image/(jpeg|png|webp);base64,(.+)$", data, re.S)
    if not m:
        raise ValueError("Fotoğraf JPG, PNG veya WEBP olmalı")
    import base64
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except ValueError:
        raise ValueError("Fotoğraf okunamadı")
    if len(raw) > 8_000_000:
        raise ValueError("Fotoğraf en fazla 8 MB olabilir")
    magic = {"jpeg": raw[:2] == b"\xff\xd8", "png": raw[:8] == b"\x89PNG\r\n\x1a\n", "webp": raw[8:12] == b"WEBP"}
    if not magic[m.group(1)]:
        raise ValueError("Dosya bir fotoğraf değil")
    os.makedirs(MEDIA, exist_ok=True)
    ext = {"jpeg": "jpg", "png": "png", "webp": "webp"}[m.group(1)]
    old = photo_path()
    name = f"memorial-{int(time.time())}.{ext}"
    with open(os.path.join(MEDIA, name), "wb") as f:
        f.write(raw)
    site["memorial"]["photo"] = name
    save_site()
    if old and os.path.basename(old) != name:
        try:
            os.replace(old, old + ".eski")
        except OSError:
            pass
    audit(user, "Ayarlar", "anma fotoğrafı değişti")
    h.json(200, {"ok": True, "site": site_payload(True)})


@route("GET", "/media/memorial")
def r_photo_get(h, qs, user):
    p = photo_path()
    if not p:
        return h.json(404, {"error": "Fotoğraf yok"})
    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(p.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    with open(p, "rb") as f:
        h.raw(200, f.read(), ctype)


def plan_file():
    for ext, ctype in (("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg"), ("webp", "image/webp")):
        p = os.path.join(HERE, f"site-plan.{ext}")
        if os.path.isfile(p):
            return p, ctype
    return None, None


@route("GET", "/site-plan.png")
def r_plan(h, qs, user):
    p, ctype = plan_file()
    if not p:
        return h.json(404, {"error": "Yok"})
    with open(p, "rb") as f:
        h.raw(200, f.read(), ctype)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "AdalyaPanel/" + VERSION

    def log_message(self, *a):
        pass

    def cookie_token(self):
        m = re.search(r"adalya_session=([A-Za-z0-9_-]+)", self.headers.get("Cookie", ""))
        return m.group(1) if m else ""

    def current_user(self):
        if args.demo:
            return DEMO_USER
        s = sessions.get(self.cookie_token())
        if s and s["exp"] > time.time():
            rec = cfg["users"].get(s["username"])
            if not rec:
                return None
            s["exp"] = time.time() + 12 * 3600
            return {"username": s["username"], "name": rec["name"], "role": rec["role"]}
        return None

    def start_session(self, u):
        tok = secrets.token_urlsafe(32)
        sessions[tok] = {**u, "exp": time.time() + 12 * 3600}
        self.json(200, {"ok": True, "user": u},
                  {"Set-Cookie": f"adalya_session={tok}; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict"})

    def body(self, max_bytes=100_000):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > max_bytes:
            raise ValueError("İstek çok büyük")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Geçersiz istek")
        if not isinstance(data, dict):
            raise ValueError("Geçersiz istek")
        return data

    def raw(self, code, data, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, code, obj, extra=None):
        self.raw(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def download(self, data, filename, ctype):
        self.raw(200, data, ctype, {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    def stream(self, chunks, filename, length):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        if length:
            self.send_header("Content-Length", length)
        self.end_headers()
        for c in chunks:
            self.wfile.write(c)

    def error(self, code, msg):
        if self.path.startswith("/api/") and not any(self.path.startswith(p) for p in
                                                     ("/api/nvr/download", "/api/events.csv", "/api/nvr/logs.csv", "/api/export.zip")):
            return self.json(code, {"error": msg})
        page = (f"<!doctype html><meta charset='utf-8'><title>Adalya Paneli</title>"
                f"<body style='font-family:Segoe UI,sans-serif;padding:40px;max-width:640px;margin:auto'>"
                f"<h2>İşlem tamamlanamadı</h2><p>{msg}</p><p><a href='/'>Panele dön</a></p></body>")
        self.raw(code, page.encode("utf-8"), "text/html; charset=utf-8")

    def dispatch(self, method):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        fn = ROUTES.get((method, u.path))
        if not fn:
            return self.error(404, "Sayfa bulunamadı")
        user = self.current_user()
        if fn.need in ("user", "admin") and not user:
            return self.error(401, "Giriş gerekli")
        if fn.need == "admin" and user["role"] != "admin":
            return self.error(403, "Bu işlem yönetici yetkisi ister")
        if method == "POST" and self.headers.get("Origin") and urlparse(self.headers["Origin"]).netloc != self.headers.get("Host"):
            return self.error(403, "Farklı bir siteden gelen istek reddedildi")
        try:
            fn(self, qs, user)
        except (NvrError, NvrAuthError) as e:
            self.error(502, str(e))
        except ValueError as e:
            self.error(400, str(e))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")


def main():
    global args, nvr, DATA, DB_PATH, CFG_PATH
    p = argparse.ArgumentParser(description="Adalya Panel")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--lan", action="store_true", help="listen on all interfaces (site screens)")
    p.add_argument("--demo", action="store_true", help="simulated site and NVR")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between ping rounds")
    p.add_argument("--misses", type=int, default=2, help="missed pings before a device counts as down")
    p.add_argument("--nvr-host")
    p.add_argument("--nvr-user")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    if args.demo:
        DATA = os.path.join(HERE, "data-demo")
        DB_PATH, CFG_PATH = os.path.join(DATA, "panel.db"), os.path.join(DATA, "config.json")
    os.makedirs(DATA, exist_ok=True)
    if args.demo and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    load_cfg()
    load_site()
    load_devices()
    db().close()

    if args.demo:
        nvr = DemoNvr(channel_names())
        nvr_state.update(info=nvr.info(), checked=time.time())
    elif args.nvr_user:
        host = args.nvr_host or cfg.get("nvr_host", "192.168.0.23")
        pw = getpass.getpass(f"NVR password for {args.nvr_user}@{host} (not saved): ")
        client = Nvr(host, args.nvr_user, pw)
        try:
            nvr_state.update(info=client.info(), checked=time.time())
            nvr = client
            print(f"NVR connected: {nvr_state['info'].get('type')}")
        except (NvrError, NvrAuthError) as e:
            print(f"NVR not connected: {e}")

    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Adalya Panel {VERSION} running at {url}{'  (demo)' if args.demo else ''}")
    if args.lan:
        print(f"Other screens on the site network: http://<this-computer-ip>:{args.port}")
    print("Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
