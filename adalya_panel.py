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
ROLES = {"admin": "Yönetici", "staff": "Güvenlik", "resident": "Site sakini"}
REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS flats (id INTEGER PRIMARY KEY, blok TEXT, kapi TEXT, daire TEXT, arsa_payi REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY, flat_id INTEGER, name TEXT, phone TEXT, email TEXT, kind TEXT,
    move_in TEXT, move_out TEXT, emergency TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS vehicles (id INTEGER PRIMARY KEY, flat_id INTEGER, plate TEXT, plate_key TEXT, model TEXT,
    color TEXT, parking TEXT, sticker TEXT);
CREATE TABLE IF NOT EXISTS visitors (id INTEGER PRIMARY KEY, ts_in REAL, ts_out REAL, name TEXT, flat_id INTEGER,
    plate TEXT, purpose TEXT, user TEXT);
CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, ts REAL, flat_id INTEGER, user TEXT, text TEXT,
    status TEXT, reply TEXT, closed_by TEXT, closed_at REAL);
CREATE TABLE IF NOT EXISTS charges (id INTEGER PRIMARY KEY, flat_id INTEGER, period TEXT, kind TEXT, amount REAL,
    due_date TEXT, note TEXT, ts REAL, user TEXT);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, flat_id INTEGER, date TEXT, amount REAL, method TEXT,
    note TEXT, user TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS flat_cams (flat_id INTEGER PRIMARY KEY, channels TEXT);
"""
USERNAME_RE = re.compile(r"^[a-z0-9._-]{2,32}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SITE_DEFAULT = {
    "site_name": "Adalya sitesi", "support_contact": "",
    "memorial": {"name": "Esin Noğan", "birth": "", "death": "", "profession": "", "note": "",
                 "created_by": "", "photo": "", "photo_x": 50, "photo_y": 35},
    "labels": [],
    "plan": {"rotation": 34, "trim_white": True},
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
    conn.executescript(REGISTRY_SQL)
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
    site["plan"] = {**SITE_DEFAULT["plan"], **data.get("plan", {})}


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
        out = {**out, "support_contact": site["support_contact"], "labels": site["labels"], "plan": site["plan"],
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


def route(method, path, need="staff"):
    """need: none (anyone), user (any signed-in role, residents too), staff (yönetici + güvenlik), admin."""
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
    if user and user["role"] == "resident":
        user = {**user, "cams": len(allowed_channels(user.get("flat_id")))}
    h.json(200, {"setup_needed": not args.demo and not admins(), "user": user, "demo": args.demo,
                 "site": site_payload(bool(user)), "version": VERSION, "lan": args.lan})


def clean_user_fields(b, creating):
    username = str(b.get("username", "")).strip().lower()
    name = str(b.get("name", "")).strip()[:40]
    role = b.get("role", "staff")
    pw = str(b.get("password", ""))
    flat_id = None
    if not USERNAME_RE.match(username):
        raise ValueError("Kullanıcı adı 2–32 karakter olmalı: küçük harf, rakam, nokta, tire (ör. ahmet.bekci)")
    if not name:
        raise ValueError("Ad soyad gerekli")
    if role not in ROLES:
        raise ValueError("Rol geçersiz")
    if role == "resident":
        try:
            flat_id = int(b.get("flat_id"))
        except (TypeError, ValueError):
            raise ValueError("Site sakini için daire seçin")
        conn = db()
        ok = conn.execute("SELECT 1 FROM flats WHERE id=?", (flat_id,)).fetchone()
        conn.close()
        if not ok:
            raise ValueError("Daire bulunamadı")
    if (creating or pw) and len(pw) < 8:
        raise ValueError("Şifre en az 8 karakter olmalı")
    return username, name, role, pw, flat_id


@route("POST", "/api/setup", need="none")
def r_setup(h, qs, user):
    if args.demo or admins():
        return h.json(400, {"error": "Kurulum zaten yapılmış"})
    username, name, _, pw, _ = clean_user_fields({**h.body(), "role": "admin"}, True)
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
    u = {"username": username, "name": rec["name"], "role": rec["role"], "flat_id": rec.get("flat_id")}
    h.start_session(u)
    audit(u, "Giriş", "")


@route("GET", "/api/users", need="admin")
def r_users(h, qs, user):
    h.json(200, {"users": [{"username": k, "name": v["name"], "role": v["role"], "flat_id": v.get("flat_id")}
                           for k, v in sorted(cfg["users"].items(), key=lambda kv: (kv[1]["role"] != "admin", kv[1]["name"]))]})


@route("POST", "/api/users", need="admin")
def r_user_save(h, qs, user):
    if args.demo:
        return h.json(400, {"error": "Demo modunda kullanıcı eklenmez"})
    b = h.body()
    creating = b.get("username", "").strip().lower() not in cfg["users"]
    username, name, role, pw, flat_id = clean_user_fields(b, creating)
    if not creating and role != "admin" and cfg["users"][username]["role"] == "admin" and admins() == [username]:
        return h.json(400, {"error": "Son yöneticinin yetkisi kaldırılamaz"})
    rec = cfg["users"].get(username, {})
    rec.update(name=name, role=role)
    if flat_id:
        rec["flat_id"] = flat_id
    else:
        rec.pop("flat_id", None)
    if pw:
        rec["pw"] = hash_pw(pw)
        for tok in [t for t, s in sessions.items() if s["username"] == username and username != user["username"]]:
            sessions.pop(tok, None)
    cfg["users"][username] = rec
    save_cfg()
    audit(user, "Kullanıcı eklendi" if creating else "Kullanıcı güncellendi", f"{username} ({ROLES[role].lower()}){' · şifre değişti' if pw and not creating else ''}")
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


@route("POST", "/api/me/password", need="user")
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
                d["x"], d["y"] = round(max(0, min(200, x)), 1), round(max(0, min(200, y)), 1)
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


@route("GET", "/api/nvr/snapshot", need="user")
def r_snapshot(h, qs, user):
    ch = chparam(qs)
    if not resident_can_watch(user, ch):
        return h.json(403, {"error": "Bu kameraya erişiminiz yok"})
    ctype, data = need_nvr().snapshot(ch)
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
            labels.append({"text": str(l["text"]).strip()[:30], "x": round(max(0, min(200, x)), 1), "y": round(max(0, min(200, y)), 1)})
        site["labels"] = labels
        changed.append("harita etiketleri")
    if isinstance(b.get("plan"), dict):
        p = b["plan"]
        if "rotation" in p:
            try:
                site["plan"]["rotation"] = round(max(-180.0, min(180.0, float(p["rotation"]))), 1)
            except (TypeError, ValueError):
                raise ValueError("Döndürme açısı geçersiz")
        if "trim_white" in p:
            site["plan"]["trim_white"] = bool(p["trim_white"])
        changed.append("harita görseli")
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


@route("GET", "/media/memorial", need="user")
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


@route("GET", "/site-plan.png", need="user")
def r_plan(h, qs, user):
    p, ctype = plan_file()
    if not p:
        return h.json(404, {"error": "Yok"})
    with open(p, "rb") as f:
        h.raw(200, f.read(), ctype)


# ---------------------------------------------------------------- devices (inventory only; never touches the device)

DEVICE_TYPES = ("camera", "ap", "router", "switch", "nvr", "repeater")
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


@route("GET", "/api/devices", need="admin")
def r_devices(h, qs, user):
    with lock:
        h.json(200, {"devices": [dict(d) for d in devices]})


@route("POST", "/api/devices", need="admin")
def r_device_save(h, qs, user):
    b = h.body()
    orig = str(b.get("orig_ip", "")).strip()
    name = str(b.get("name", "")).strip()[:60]
    ip = str(b.get("ip", "")).strip()
    typ = b.get("type")
    mac = kit.norm_mac(str(b.get("mac", "")))
    mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2)).upper() if len(mac) == 12 else ""
    if not name:
        raise ValueError("Cihaz adı gerekli")
    try:
        if not ipaddress.ip_address(ip).is_private:
            raise ValueError
    except ValueError:
        raise ValueError("IP adresi yerel ağda olmalı (ör. 192.168.0.130)")
    if typ not in DEVICE_TYPES:
        raise ValueError("Cihaz türü geçersiz")
    if b.get("mac") and not mac:
        raise ValueError("MAC biçimi AA:BB:CC:DD:EE:FF olmalı")
    ch = b.get("channel")
    if ch in ("", None):
        ch = None
    else:
        try:
            ch = int(ch)
        except (TypeError, ValueError):
            raise ValueError("Kanal numarası geçersiz")
        if not 1 <= ch <= 64:
            raise ValueError("Kanal 1–64 arasında olmalı")
    ports = [int(p) for p in re.findall(r"\d+", str(b.get("ports", "80")))[:6] if 0 < int(p) < 65536] or [80]
    with lock:
        cur = next((d for d in devices if d["ip"] == orig), None) if orig else None
        if orig and not cur:
            return h.json(404, {"error": "Cihaz bulunamadı"})
        if any(d["ip"] == ip and d is not cur for d in devices):
            raise ValueError(f"{ip} başka bir cihazda kayıtlı")
        if ch and any(d.get("channel") == ch and d is not cur and not d.get("hidden") for d in devices):
            raise ValueError(f"Kanal {ch} başka bir kamerada kayıtlı")
        rec = cur if cur else {}
        rec.update(name=name, ip=ip, mac=mac, type=typ, ports=ports)
        if ch and typ == "camera":
            rec["channel"] = ch
        else:
            rec.pop("channel", None)
        if b.get("hidden"):
            rec["hidden"] = True
        else:
            rec.pop("hidden", None)
        if not cur:
            rec.setdefault("x", 45)
            rec.setdefault("y", 45)
            devices.append(rec)
        if orig and orig != ip:
            state.pop(orig, None)
        state.setdefault(ip, {"up": None, "since": None, "misses": 0, "first_miss": None,
                              "rtt": None, "mac_seen": "", "checked": None})
        save_devices()
    audit(user, "Cihaz güncellendi" if cur else "Cihaz eklendi", f"{name} · {ip}{f' · kanal {ch}' if ch else ''}")
    h.json(200, {"ok": True})


@route("POST", "/api/devices/delete", need="admin")
def r_device_delete(h, qs, user):
    ip = str(h.body().get("ip", ""))
    with lock:
        d = next((x for x in devices if x["ip"] == ip), None)
        if not d:
            return h.json(404, {"error": "Cihaz bulunamadı"})
        devices.remove(d)
        state.pop(ip, None)
        save_devices()
    audit(user, "Cihaz silindi", f"{d['name']} · {ip}")
    h.json(200, {"ok": True})


# ---------------------------------------------------------------- residents registry (KVKK: personal data)

KINDS = {"malik": "Malik", "kiraci": "Kiracı", "hane": "Hane üyesi"}
TR_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iIsSgGuUoOcC")


def fold(s):
    return re.sub(r"[^a-z0-9]", "", str(s).translate(TR_FOLD).lower())


def plate_key(p):
    return re.sub(r"[^A-Z0-9]", "", str(p).translate(TR_FOLD).upper())


def txt(b, k, n=120):
    return str(b.get(k) or "").strip()[:n]


def opt_date(b, k):
    v = txt(b, k, 10)
    if v and not DATE_RE.match(v):
        raise ValueError("Tarih biçimi YYYY-AA-GG olmalı")
    return v


def flat_label(f):
    return f"{f['blok']} Blok · Daire {f['daire']}" if f.get("blok") else f"Daire {f.get('daire', '')}"


def registry(conn, flat_id=None, full=True):
    q = "SELECT id, blok, kapi, daire, arsa_payi, notes FROM flats"
    rows = conn.execute(q + (" WHERE id=?" if flat_id else ""), (flat_id,) if flat_id else ()).fetchall()
    flats = {r[0]: {"id": r[0], "blok": r[1], "kapi": r[2], "daire": r[3], "arsa_payi": r[4], "notes": r[5] or "",
                    "people": [], "vehicles": []} for r in rows}
    for r in conn.execute("SELECT id, flat_id, name, phone, email, kind, move_in, move_out, emergency, notes FROM people ORDER BY CASE kind WHEN 'malik' THEN 0 WHEN 'kiraci' THEN 1 ELSE 2 END, name"):
        if r[1] in flats:
            flats[r[1]]["people"].append({"id": r[0], "flat_id": r[1], "name": r[2], "phone": r[3], "email": r[4], "kind": r[5],
                                          "move_in": r[6], "move_out": r[7], "emergency": r[8], "notes": r[9] or ""})
    for r in conn.execute("SELECT id, flat_id, plate, model, color, parking, sticker FROM vehicles ORDER BY plate"):
        if r[1] in flats:
            flats[r[1]]["vehicles"].append({"id": r[0], "flat_id": r[1], "plate": r[2], "model": r[3], "color": r[4],
                                            "parking": r[5], "sticker": r[6]})
    out = sorted(flats.values(), key=lambda f: (f["blok"] or "", int(f["daire"]) if str(f["daire"]).isdigit() else 0, str(f["daire"])))
    if not full:  # Güvenlik: names, phones, emergency contacts and vehicles only (KVKK data minimisation)
        for f in out:
            f.pop("arsa_payi"); f.pop("notes")
            for p in f["people"]:
                for k in ("email", "notes", "move_in"):
                    p.pop(k)
            f["people"] = [p for p in f["people"] if not p["move_out"]]
    return out


@route("GET", "/api/registry")
def r_registry(h, qs, user):
    conn = db()
    flats = registry(conn, full=user["role"] == "admin")
    conn.close()
    h.json(200, {"flats": flats, "kinds": KINDS})


@route("POST", "/api/flats", need="admin")
def r_flat_save(h, qs, user):
    b = h.body()
    blok, daire = txt(b, "blok", 10).upper(), txt(b, "daire", 10)
    if not daire:
        raise ValueError("Daire numarası gerekli")
    ap = b.get("arsa_payi")
    try:
        ap = float(str(ap).replace(",", ".")) if ap not in (None, "") else None
    except ValueError:
        raise ValueError("Arsa payı sayı olmalı")
    conn = db()
    fid = b.get("id")
    dup = conn.execute("SELECT id FROM flats WHERE blok=? AND daire=?", (blok, daire)).fetchone()
    if dup and dup[0] != fid:
        conn.close()
        raise ValueError(f"{blok} Blok · Daire {daire} zaten kayıtlı")
    vals = (blok, txt(b, "kapi", 10), daire, ap, txt(b, "notes", 500))
    if fid:
        conn.execute("UPDATE flats SET blok=?, kapi=?, daire=?, arsa_payi=?, notes=? WHERE id=?", (*vals, fid))
    else:
        fid = conn.execute("INSERT INTO flats (blok, kapi, daire, arsa_payi, notes) VALUES (?,?,?,?,?)", vals).lastrowid
    conn.commit()
    conn.close()
    audit(user, "Daire kaydı", f"{blok} Blok · Daire {daire}")
    h.json(200, {"ok": True, "id": fid})


@route("POST", "/api/flats/delete", need="admin")
def r_flat_delete(h, qs, user):
    fid = h.body().get("id")
    conn = db()
    f = conn.execute("SELECT blok, daire FROM flats WHERE id=?", (fid,)).fetchone()
    if not f:
        conn.close()
        return h.json(404, {"error": "Daire bulunamadı"})
    for t in ("people", "vehicles"):
        conn.execute(f"DELETE FROM {t} WHERE flat_id=?", (fid,))
    conn.execute("DELETE FROM flats WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    audit(user, "Daire silindi", f"{f[0]} Blok · Daire {f[1]} (kişi ve araçlarıyla)")
    h.json(200, {"ok": True})


def need_flat(conn, fid):
    if not conn.execute("SELECT 1 FROM flats WHERE id=?", (fid,)).fetchone():
        conn.close()
        raise ValueError("Daire bulunamadı")


@route("POST", "/api/people", need="admin")
def r_person_save(h, qs, user):
    b = h.body()
    name = txt(b, "name", 80)
    if not name:
        raise ValueError("Ad soyad gerekli")
    kind = b.get("kind") if b.get("kind") in KINDS else "malik"
    email = txt(b, "email", 120)
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("E-posta adresi geçersiz")
    phone = re.sub(r"[^\d+]", "", txt(b, "phone", 30))
    conn = db()
    need_flat(conn, b.get("flat_id"))
    vals = (b["flat_id"], name, phone, email, kind, opt_date(b, "move_in"), opt_date(b, "move_out"), txt(b, "emergency", 120), txt(b, "notes", 500))
    if b.get("id"):
        conn.execute("UPDATE people SET flat_id=?, name=?, phone=?, email=?, kind=?, move_in=?, move_out=?, emergency=?, notes=? WHERE id=?", (*vals, b["id"]))
    else:
        conn.execute("INSERT INTO people (flat_id, name, phone, email, kind, move_in, move_out, emergency, notes) VALUES (?,?,?,?,?,?,?,?,?)", vals)
    conn.commit()
    conn.close()
    audit(user, "Kişi kaydı", name)
    h.json(200, {"ok": True})


@route("POST", "/api/vehicles", need="admin")
def r_vehicle_save(h, qs, user):
    b = h.body()
    plate = re.sub(r"\s+", " ", txt(b, "plate", 15).translate(TR_FOLD).upper())
    if len(plate_key(plate)) < 4:
        raise ValueError("Plaka geçersiz")
    conn = db()
    need_flat(conn, b.get("flat_id"))
    dup = conn.execute("SELECT id FROM vehicles WHERE plate_key=?", (plate_key(plate),)).fetchone()
    if dup and dup[0] != b.get("id"):
        conn.close()
        raise ValueError(f"{plate} başka bir dairede kayıtlı")
    vals = (b["flat_id"], plate, plate_key(plate), txt(b, "model", 60), txt(b, "color", 30), txt(b, "parking", 20), txt(b, "sticker", 20))
    if b.get("id"):
        conn.execute("UPDATE vehicles SET flat_id=?, plate=?, plate_key=?, model=?, color=?, parking=?, sticker=? WHERE id=?", (*vals, b["id"]))
    else:
        conn.execute("INSERT INTO vehicles (flat_id, plate, plate_key, model, color, parking, sticker) VALUES (?,?,?,?,?,?,?)", vals)
    conn.commit()
    conn.close()
    audit(user, "Araç kaydı", plate)
    h.json(200, {"ok": True})


@route("POST", "/api/people/delete", need="admin")
def r_person_delete(h, qs, user):
    return delete_row(h, user, "people", "name", "Kişi silindi")


@route("POST", "/api/vehicles/delete", need="admin")
def r_vehicle_delete(h, qs, user):
    return delete_row(h, user, "vehicles", "plate", "Araç silindi")


def delete_row(h, user, table, label, action):
    rid = h.body().get("id")
    conn = db()
    r = conn.execute(f"SELECT {label} FROM {table} WHERE id=?", (rid,)).fetchone()
    if r:
        conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
        conn.commit()
    conn.close()
    if not r:
        return h.json(404, {"error": "Kayıt bulunamadı"})
    audit(user, action, r[0])
    h.json(200, {"ok": True})


# Import: Excel (.xlsx) or CSV, one row per person and/or vehicle, flat columns repeated.
IMPORT_COLS = {
    "blok": "blok", "kapi": "kapi", "kapino": "kapi", "daire": "daire", "daireno": "daire", "no": "daire",
    "arsapayi": "arsa_payi", "adsoyad": "name", "ad": "name", "isim": "name", "adisoyadi": "name", "kisi": "name",
    "telefon": "phone", "tel": "phone", "gsm": "phone", "cep": "phone", "eposta": "email", "email": "email", "mail": "email",
    "durum": "kind", "tip": "kind", "maliksakin": "kind", "giris": "move_in", "tasinma": "move_in", "giristarihi": "move_in",
    "acil": "emergency", "acildurumkisisi": "emergency", "plaka": "plate", "arac": "model", "markamodel": "model",
    "marka": "model", "renk": "color", "otopark": "parking", "parkyeri": "parking", "etiket": "sticker", "etiketno": "sticker",
    "stiker": "sticker", "not": "notes", "aciklama": "notes",
}
TEMPLATE_HEADER = ["Blok", "Kapı", "Daire", "Arsa payı", "Ad soyad", "Telefon", "E-posta", "Durum", "Giriş tarihi",
                   "Acil durum kişisi", "Plaka", "Marka model", "Renk", "Otopark", "Etiket no"]


def read_xlsx(raw):
    """Minimal .xlsx reader (first sheet, values only); standard library only."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in si.iter() if t.tag.endswith("}t")))
        sheet = next((n for n in sorted(names) if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), None)
        if not sheet:
            raise ValueError("Excel dosyasında sayfa bulunamadı")
        rows = []
        for row in ET.fromstring(z.read(sheet)).iter():
            if not row.tag.endswith("}row"):
                continue
            vals = {}
            for c in row:
                if not c.tag.endswith("}c"):
                    continue
                col = 0
                for ch in re.match(r"[A-Z]+", c.get("r", "A")).group(0):
                    col = col * 26 + ord(ch) - 64
                t, v = c.get("t"), None
                if t == "inlineStr":
                    v = "".join(x.text or "" for x in c.iter() if x.tag.endswith("}t"))
                else:
                    ve = next((x for x in c if x.tag.endswith("}v")), None)
                    v = ve.text if ve is not None else ""
                    if t == "s" and v:
                        v = shared[int(v)]
                    elif v and re.match(r"^-?\d+\.0+$", v):
                        v = v.split(".")[0]
                vals[col - 1] = v or ""
            if vals:
                rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
        return rows


def parse_import(data):
    import base64
    m = re.match(r"^data:[^;,]*;base64,(.+)$", data, re.S)
    raw = base64.b64decode(m.group(1)) if m else data.encode("utf-8")
    if len(raw) > 5_000_000:
        raise ValueError("Dosya en fazla 5 MB olabilir")
    if raw[:2] == b"PK":
        try:
            rows = read_xlsx(raw)
        except (zipfile.BadZipFile, KeyError) as e:
            raise ValueError(f"Excel dosyası okunamadı: {e}")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1254")
        first = text.split("\n", 1)[0]
        rows = list(csv.reader(io.StringIO(text), delimiter=";" if first.count(";") > first.count(",") else ","))
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("Dosyada başlık satırı ve en az bir kayıt olmalı")
    cols = [IMPORT_COLS.get(fold(c)) for c in rows[0]]
    if "daire" not in cols:
        raise ValueError("Başlık satırında en az 'Daire' sütunu olmalı (şablonu indirip kullanın)")
    return [{k: str(r[i]).strip() for i, k in enumerate(cols) if k and i < len(r)} for r in rows[1:]]


@route("POST", "/api/registry/import", need="admin")
def r_import(h, qs, user):
    b = h.body(max_bytes=8_000_000)
    recs = parse_import(str(b.get("data", "")))
    preview = bool(b.get("preview"))
    conn = db()
    n = {"flats": 0, "people": 0, "vehicles": 0, "updated": 0}
    errors, new_flats = [], set()
    for i, r in enumerate(recs, start=2):
        blok, daire = r.get("blok", "").upper()[:10], r.get("daire", "")[:10]
        if not daire:
            errors.append(f"Satır {i}: daire boş")
            continue
        f = conn.execute("SELECT id FROM flats WHERE blok=? AND daire=?", (blok, daire)).fetchone()
        ap = r.get("arsa_payi", "").replace(",", ".")
        try:
            ap = float(ap) if ap else None
        except ValueError:
            errors.append(f"Satır {i}: arsa payı sayı değil ({r['arsa_payi']})")
            ap = None
        if f:
            fid = f[0]
            if not preview and (r.get("kapi") or ap is not None):
                conn.execute("UPDATE flats SET kapi=COALESCE(NULLIF(?,''), kapi), arsa_payi=COALESCE(?, arsa_payi) WHERE id=?", (r.get("kapi", ""), ap, fid))
        elif preview and (blok, daire) in new_flats:
            fid = None
        else:
            new_flats.add((blok, daire))
            n["flats"] += 1
            fid = None if preview else conn.execute("INSERT INTO flats (blok, kapi, daire, arsa_payi, notes) VALUES (?,?,?,?,'')",
                                                    (blok, r.get("kapi", "")[:10], daire, ap)).lastrowid
        if r.get("name"):
            kind = fold(r.get("kind", ""))
            kind = "kiraci" if kind.startswith("kira") else "hane" if kind.startswith("hane") or kind.startswith("aile") else "malik"
            mi = r.get("move_in", "")
            mi = mi if DATE_RE.match(mi) else ""
            p = fid and conn.execute("SELECT id FROM people WHERE flat_id=? AND name=?", (fid, r["name"][:80])).fetchone()
            if p:
                n["updated"] += 1
            else:
                n["people"] += 1
            if not preview:
                vals = (re.sub(r"[^\d+]", "", r.get("phone", ""))[:30], r.get("email", "")[:120], kind, mi, r.get("emergency", "")[:120])
                if p:
                    conn.execute("UPDATE people SET phone=COALESCE(NULLIF(?,''),phone), email=COALESCE(NULLIF(?,''),email), kind=?, "
                                 "move_in=COALESCE(NULLIF(?,''),move_in), emergency=COALESCE(NULLIF(?,''),emergency) WHERE id=?", (*vals, p[0]))
                else:
                    conn.execute("INSERT INTO people (flat_id, name, phone, email, kind, move_in, move_out, emergency, notes) VALUES (?,?,?,?,?,?,'',?,'')",
                                 (fid, r["name"][:80], *vals))
        if r.get("plate"):
            plate = re.sub(r"\s+", " ", r["plate"].translate(TR_FOLD).upper())[:15]
            if len(plate_key(plate)) < 4:
                errors.append(f"Satır {i}: plaka geçersiz ({r['plate']})")
            else:
                v = conn.execute("SELECT id FROM vehicles WHERE plate_key=?", (plate_key(plate),)).fetchone()
                if v:
                    n["updated"] += 1
                else:
                    n["vehicles"] += 1
                if not preview:
                    vals = (fid, plate, plate_key(plate), r.get("model", "")[:60], r.get("color", "")[:30], r.get("parking", "")[:20], r.get("sticker", "")[:20])
                    if v:
                        conn.execute("UPDATE vehicles SET flat_id=?, plate=?, plate_key=?, model=?, color=?, parking=?, sticker=? WHERE id=?", (*vals, v[0]))
                    else:
                        conn.execute("INSERT INTO vehicles (flat_id, plate, plate_key, model, color, parking, sticker) VALUES (?,?,?,?,?,?,?)", vals)
    if not preview:
        conn.commit()
        audit(user, "Sakin listesi içe aktarıldı", f"{len(recs)} satır · {n['flats']} yeni daire, {n['people']} yeni kişi, {n['vehicles']} yeni araç, {n['updated']} güncelleme")
    conn.close()
    h.json(200, {"ok": True, "rows": len(recs), "counts": n, "errors": errors[:50], "preview": preview})


@route("GET", "/api/registry-template.csv", need="admin")
def r_template(h, qs, user):
    h.download(csv_bytes(TEMPLATE_HEADER, [["A", "1", "1", "1/24", "Örnek Kişi", "05001112233", "ornek@eposta.com", "Malik",
                                            "2024-06-01", "Yakını 05002223344", "07 ABC 123", "Renault Clio", "Beyaz", "A-12", "125"]]),
               "adalya-sakin-listesi-sablon.csv", "text/csv; charset=utf-8")


@route("GET", "/api/registry.csv", need="admin")
def r_registry_csv(h, qs, user):
    conn = db()
    flats = registry(conn)
    conn.close()
    rows = []
    for f in flats:
        people, cars = f["people"] or [{}], f["vehicles"]
        for i in range(max(len(people), len(cars))):
            p, v = people[i] if i < len(people) else {}, cars[i] if i < len(cars) else {}
            rows.append([f["blok"], f["kapi"], f["daire"], f["arsa_payi"] if f["arsa_payi"] is not None else "", p.get("name", ""), p.get("phone", ""),
                         p.get("email", ""), KINDS.get(p.get("kind"), ""), p.get("move_in", ""), p.get("emergency", ""),
                         v.get("plate", ""), v.get("model", ""), v.get("color", ""), v.get("parking", ""), v.get("sticker", "")])
    audit(user, "Sakin listesini indirdi", f"{len(flats)} daire")
    h.download(csv_bytes(TEMPLATE_HEADER, rows), f"adalya-sakin-listesi-{stamp()}.csv", "text/csv; charset=utf-8")


@route("GET", "/api/plate")
def r_plate(h, qs, user):
    q = plate_key(qs.get("q", ""))
    if len(q) < 3:
        raise ValueError("En az 3 karakter yazın")
    conn = db()
    rows = conn.execute("SELECT v.plate, v.model, v.color, v.parking, v.sticker, f.id, f.blok, f.daire FROM vehicles v "
                        "JOIN flats f ON f.id=v.flat_id WHERE v.plate_key LIKE ? ORDER BY v.plate LIMIT 20", (f"%{q}%",)).fetchall()
    out = []
    for r in rows:
        ppl = conn.execute("SELECT name, phone, kind FROM people WHERE flat_id=? AND COALESCE(move_out,'')='' ORDER BY kind, name", (r[5],)).fetchall()
        out.append({"plate": r[0], "model": r[1], "color": r[2], "parking": r[3], "sticker": r[4], "flat_id": r[5],
                    "flat": flat_label({"blok": r[6], "daire": r[7]}),
                    "people": [{"name": p[0], "phone": p[1], "kind": p[2]} for p in ppl]})
    vis = conn.execute("SELECT ts_in, name, plate FROM visitors WHERE REPLACE(UPPER(plate),' ','') LIKE ? ORDER BY ts_in DESC LIMIT 5", (f"%{q}%",)).fetchall()
    conn.close()
    audit(user, "Plaka sorgusu", f"{qs.get('q', '')[:15]} · {len(out)} sonuç")
    h.json(200, {"results": out, "visitors": [{"ts": v[0], "name": v[1], "plate": v[2]} for v in vis]})


@route("GET", "/api/visitors")
def r_visitors(h, qs, user):
    d = dparam(qs, "date", datetime.now().strftime("%Y-%m-%d"))
    conn = db()
    rows = conn.execute("SELECT v.id, v.ts_in, v.ts_out, v.name, v.flat_id, v.plate, v.purpose, v.user, f.blok, f.daire FROM visitors v "
                        "LEFT JOIN flats f ON f.id=v.flat_id WHERE v.ts_in >= ? AND v.ts_in < ? OR (v.ts_out IS NULL AND v.ts_in < ?) "
                        "ORDER BY v.ts_in DESC", (d.timestamp(), (d + timedelta(days=1)).timestamp(), (d + timedelta(days=1)).timestamp())).fetchall()
    conn.close()
    h.json(200, {"visitors": [{"id": r[0], "ts_in": r[1], "ts_out": r[2], "name": r[3], "flat_id": r[4], "plate": r[5], "purpose": r[6],
                               "user": r[7], "flat": flat_label({"blok": r[8], "daire": r[9]}) if r[9] else ""} for r in rows]})


@route("POST", "/api/visitors")
def r_visitor_add(h, qs, user):
    b = h.body()
    name = txt(b, "name", 80)
    if not name:
        raise ValueError("Ziyaretçinin adını yazın")
    fid = b.get("flat_id") or None
    conn = db()
    if fid:
        need_flat(conn, fid)
    plate = re.sub(r"\s+", " ", txt(b, "plate", 15).translate(TR_FOLD).upper())
    conn.execute("INSERT INTO visitors (ts_in, ts_out, name, flat_id, plate, purpose, user) VALUES (?,?,?,?,?,?,?)",
                 (time.time(), None, name, fid, plate, txt(b, "purpose", 120), user["name"]))
    conn.commit()
    conn.close()
    h.json(200, {"ok": True})


@route("POST", "/api/visitors/out")
def r_visitor_out(h, qs, user):
    conn = db()
    conn.execute("UPDATE visitors SET ts_out=? WHERE id=? AND ts_out IS NULL", (time.time(), h.body().get("id")))
    conn.commit()
    conn.close()
    h.json(200, {"ok": True})


@route("GET", "/api/my", need="user")
def r_my(h, qs, user):
    if user["role"] != "resident" or not user.get("flat_id"):
        return h.json(400, {"error": "Bu sayfa site sakinleri içindir"})
    conn = db()
    flats = registry(conn, user["flat_id"])
    reqs = conn.execute("SELECT id, ts, text, status, reply FROM requests WHERE flat_id=? ORDER BY ts DESC LIMIT 30", (user["flat_id"],)).fetchall()
    conn.close()
    f = flats[0] if flats else None
    if f:
        for p in f["people"]:
            p.pop("notes", None)
        f.pop("notes", None)
    conn = db()
    dues = {"state": dues_state(conn, [user["flat_id"]])[user["flat_id"]], "ledger": ledger(conn, user["flat_id"])} if f else None
    conn.close()
    ds = get_setting("dues", DUES_DEFAULT)
    if dues:
        dues.update(iban=ds["iban"], account_name=ds["account_name"], late_pct=ds["late_pct"], due_day=ds["due_day"])
    h.json(200, {"flat": f, "kinds": KINDS, "dues": dues, "methods": PAY_METHODS,
                 "requests": [{"id": r[0], "ts": r[1], "text": r[2], "status": r[3], "reply": r[4]} for r in reqs]})


@route("POST", "/api/my/request", need="user")
def r_my_request(h, qs, user):
    if user["role"] != "resident" or not user.get("flat_id"):
        return h.json(400, {"error": "Bu işlem site sakinleri içindir"})
    t = txt(h.body(), "text", 2000)
    if not t:
        raise ValueError("Talebinizi yazın")
    conn = db()
    conn.execute("INSERT INTO requests (ts, flat_id, user, text, status) VALUES (?,?,?,?, 'acik')", (time.time(), user["flat_id"], user["name"], t))
    conn.commit()
    conn.close()
    h.json(200, {"ok": True})


@route("GET", "/api/requests", need="admin")
def r_requests(h, qs, user):
    conn = db()
    rows = conn.execute("SELECT r.id, r.ts, r.user, r.text, r.status, r.reply, r.closed_by, r.closed_at, f.blok, f.daire FROM requests r "
                        "LEFT JOIN flats f ON f.id=r.flat_id ORDER BY r.status='kapali', r.ts DESC LIMIT 300").fetchall()
    conn.close()
    h.json(200, {"requests": [{"id": r[0], "ts": r[1], "user": r[2], "text": r[3], "status": r[4], "reply": r[5], "closed_by": r[6],
                               "closed_at": r[7], "flat": flat_label({"blok": r[8], "daire": r[9]}) if r[9] else ""} for r in rows]})


@route("POST", "/api/requests/close", need="admin")
def r_request_close(h, qs, user):
    b = h.body()
    conn = db()
    conn.execute("UPDATE requests SET status='kapali', reply=?, closed_by=?, closed_at=? WHERE id=?",
                 (txt(b, "reply", 2000), user["name"], time.time(), b.get("id")))
    conn.commit()
    conn.close()
    h.json(200, {"ok": True})


@route("POST", "/api/messages/log", need="admin")
def r_message_log(h, qs, user):
    b = h.body()
    audit(user, "Mesaj hazırlandı", f"{txt(b, 'channel', 20)} · {int(b.get('count') or 0)} alıcı · {txt(b, 'subject', 80)}")
    h.json(200, {"ok": True})


def seed_demo_registry():
    """Fictional residents for demo mode only (data-demo/, recreated on every demo start)."""
    rnd = random.Random(7)
    first = ["Ayşe", "Mehmet", "Fatma", "Mustafa", "Zeynep", "Ahmet", "Elif", "Emre", "Selin", "Burak", "Deniz", "Cem", "Ece", "Can", "Derya", "Oğuz"]
    last = ["Yılmaz", "Kaya", "Demir", "Şahin", "Çelik", "Yıldız", "Aydın", "Öztürk", "Arslan", "Doğan", "Kılıç", "Aslan"]
    cars = [("Renault Clio", "Beyaz"), ("Fiat Egea", "Gri"), ("Toyota Corolla", "Siyah"), ("VW Passat", "Lacivert"), ("Hyundai i20", "Kırmızı"), ("Dacia Duster", "Turuncu")]
    conn = db()
    n = 0
    for blok in "ABC":
        for d in range(1, 9):
            fid = conn.execute("INSERT INTO flats (blok, kapi, daire, arsa_payi, notes) VALUES (?,?,?,?,'')", (blok, "1", str(d), 1.0)).lastrowid
            ln = rnd.choice(last)
            for k, kind in enumerate(["malik", "hane"] if d % 3 else ["malik", "kiraci"]):
                nm = f"{rnd.choice(first)} {ln}"
                conn.execute("INSERT INTO people (flat_id, name, phone, email, kind, move_in, move_out, emergency, notes) VALUES (?,?,?,?,?,?,'',?,'')",
                             (fid, nm, f"0500{rnd.randint(1000000, 9999999)}", f"{fold(nm.split()[0])}.{fold(ln)}@ornek.com" if k == 0 else "",
                              kind, "2023-05-01", ""))
            for _ in range(rnd.choice([0, 1, 1, 2])):
                n += 1
                m, c = rnd.choice(cars)
                plate = f"07 {rnd.choice(['ABC', 'DK', 'FR', 'HT', 'KLM', 'S'])} {rnd.randint(10, 999)}"
                conn.execute("INSERT INTO vehicles (flat_id, plate, plate_key, model, color, parking, sticker) VALUES (?,?,?,?,?,?,?)",
                             (fid, plate, plate_key(plate), m, c, f"{blok}-{n}", str(100 + n)))
    now = time.time()
    conn.execute("INSERT INTO visitors (ts_in, ts_out, name, flat_id, plate, purpose, user) VALUES (?,?,?,?,?,?,?)", (now - 5400, now - 3600, "Kargo görevlisi", 3, "07 KRG 12", "Teslimat", "Demo güvenlik"))
    conn.execute("INSERT INTO visitors (ts_in, ts_out, name, flat_id, plate, purpose, user) VALUES (?,?,?,?,?,?,?)", (now - 1200, None, "Usta (kombi)", 10, "", "Tamir", "Demo güvenlik"))
    conn.execute("INSERT INTO requests (ts, flat_id, user, text, status) VALUES (?,?,?,?, 'acik')", (now - 86400, 1, "Demo site sakini", "Aracımın plakası değişti: yeni plaka 07 YEN 101."))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dues', ?)", (json.dumps({**DUES_DEFAULT, "amount": 1500.0, "iban": "TR00 0000 0000 0000 0000 0000 00", "account_name": "Adalya Sitesi Yönetimi (demo)"}),))
    today = datetime.now()
    flats = [r[0] for r in conn.execute("SELECT id FROM flats")]
    for back in range(5, -1, -1):
        y, m = today.year, today.month - back
        while m < 1:
            y, m = y - 1, m + 12
        period = f"{y}-{m:02d}"
        for fid in flats:
            conn.execute("INSERT INTO charges (flat_id, period, kind, amount, due_date, note, ts, user) VALUES (?,?,?,?,?,?,?,?)",
                         (fid, period, "aidat", 1500.0, f"{period}-10", "", now, "Demo"))
            late_payer = fid % 7 == 0 or (fid % 5 == 0 and back < 3) or (fid == 1 and back < 2)
            if not late_payer and (back > 0 or rnd.random() < 0.6):
                conn.execute("INSERT INTO payments (flat_id, date, amount, method, note, user, ts) VALUES (?,?,?,?,?,?,?)",
                             (fid, f"{period}-{rnd.randint(1, 12):02d}", 1500.0, rnd.choice(["havale", "havale", "nakit"]), "", "Demo", now))
    conn.commit()
    conn.close()
    put_setting("cam_access", {"enabled": True, "all": [3], "blok": suggest_cam_access(["A", "B", "C"])})


# ---------------------------------------------------------------- settings stored in the database (data/, never committed)

DUES_DEFAULT = {"mode": "esit", "amount": 0.0, "total": 0.0, "due_day": 10, "late_pct": 5.0, "iban": "", "account_name": ""}
CAM_DEFAULT = {"enabled": False, "all": [], "blok": {}}


def get_setting(key, default):
    conn = db()
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    try:
        return {**default, **json.loads(r[0])} if r else dict(default)
    except (ValueError, TypeError):
        return dict(default)


def put_setting(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()
    conn.close()


def money(v, field="Tutar"):
    """Accepts 1500, '1500', '1.500,50' (Turkish) or '1500,50'."""
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        x = round(float(s), 2)
    except (TypeError, ValueError):
        raise ValueError(f"{field} sayı olmalı")
    if x < 0 or x > 10_000_000:
        raise ValueError(f"{field} geçersiz")
    return x


# ---------------------------------------------------------------- aidat (dues)

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PAY_METHODS = {"havale": "Havale/EFT", "nakit": "Nakit", "kart": "Kart", "diger": "Diğer"}


def months_between(d0, d1):
    """Started months of delay from due date d0 to d1 (both YYYY-MM-DD); 0 if not late."""
    a, b = datetime.strptime(d0, "%Y-%m-%d"), datetime.strptime(d1, "%Y-%m-%d")
    if b <= a:
        return 0
    m = (b.year - a.year) * 12 + b.month - a.month
    return m + (1 if b.day > a.day else 0) or 1


def dues_state(conn, flat_ids=None, today=None):
    """Per flat: charged, paid, balance, overdue (oldest charges are paid first), late fee estimate."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    late = get_setting("dues", DUES_DEFAULT)["late_pct"] / 100
    charges, paid = {}, {}
    for fid, amt, due, period, kind in conn.execute("SELECT flat_id, amount, due_date, period, kind FROM charges ORDER BY due_date, id"):
        charges.setdefault(fid, []).append((amt, due, period, kind))
    for fid, s in conn.execute("SELECT flat_id, SUM(amount) FROM payments GROUP BY flat_id"):
        paid[fid] = s or 0
    ids = flat_ids if flat_ids is not None else {r[0] for r in conn.execute("SELECT id FROM flats")}
    out = {}
    for fid in ids:
        left = paid.get(fid, 0.0)
        overdue = fee = 0.0
        months, oldest, open_items = 0, None, []
        for amt, due, period, kind in charges.get(fid, []):
            cover = min(left, amt)
            left -= cover
            rest = round(amt - cover, 2)
            if rest > 0.004:
                open_items.append({"period": period, "kind": kind, "due_date": due, "open": rest})
                if due < today:
                    overdue += rest
                    months += 1
                    oldest = oldest or due
                    fee += rest * late * months_between(due, today)
        charged = sum(c[0] for c in charges.get(fid, []))
        out[fid] = {"charged": round(charged, 2), "paid": round(paid.get(fid, 0.0), 2), "balance": round(charged - paid.get(fid, 0.0), 2),
                    "overdue": round(overdue, 2), "overdue_count": months, "oldest_due": oldest, "late_fee": round(fee, 2),
                    "open": open_items}
    return out


def ledger(conn, fid):
    rows = [{"type": "charge", "id": r[0], "date": r[1], "period": r[2], "kind": r[3], "amount": r[4], "note": r[5] or ""}
            for r in conn.execute("SELECT id, due_date, period, kind, amount, note FROM charges WHERE flat_id=?", (fid,))]
    rows += [{"type": "payment", "id": r[0], "date": r[1], "method": r[2], "amount": r[3], "note": r[4] or "", "user": r[5]}
             for r in conn.execute("SELECT id, date, method, amount, note, user FROM payments WHERE flat_id=?", (fid,))]
    rows.sort(key=lambda r: (r["date"], 0 if r["type"] == "charge" else 1, r["id"]))
    bal = 0.0
    for r in rows:
        bal += r["amount"] if r["type"] == "charge" else -r["amount"]
        r["balance"] = round(bal, 2)
    return rows


def flat_share(dues, flats):
    """Monthly aidat per flat id: equal amount, or the monthly total split by arsa payı."""
    if dues["mode"] == "arsa":
        tot = sum(f[1] or 0 for f in flats)
        return {f[0]: round(dues["total"] * (f[1] or 0) / tot, 2) if tot else 0 for f in flats}
    return {f[0]: dues["amount"] for f in flats}


@route("GET", "/api/dues", need="admin")
def r_dues(h, qs, user):
    conn = db()
    st = dues_state(conn)
    month = datetime.now().strftime("%Y-%m")
    m_charged = conn.execute("SELECT COALESCE(SUM(amount),0) FROM charges WHERE period=?", (month,)).fetchone()[0]
    m_paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE substr(date,1,7)=?", (month,)).fetchone()[0]
    recent = conn.execute("SELECT p.id, p.date, p.amount, p.method, p.note, p.user, f.blok, f.daire, p.flat_id FROM payments p "
                          "LEFT JOIN flats f ON f.id=p.flat_id ORDER BY p.date DESC, p.id DESC LIMIT 30").fetchall()
    periods = [r[0] for r in conn.execute("SELECT DISTINCT period FROM charges WHERE kind='aidat' ORDER BY period DESC LIMIT 24")]
    conn.close()
    h.json(200, {"settings": get_setting("dues", DUES_DEFAULT), "flats": {str(k): v for k, v in st.items()}, "month": month,
                 "month_charged": round(m_charged, 2), "month_paid": round(m_paid, 2), "periods": periods, "methods": PAY_METHODS,
                 "recent": [{"id": r[0], "date": r[1], "amount": r[2], "method": r[3], "note": r[4], "user": r[5], "flat_id": r[8],
                             "flat": flat_label({"blok": r[6], "daire": r[7]}) if r[7] else ""} for r in recent]})


@route("GET", "/api/dues/ledger", need="admin")
def r_dues_ledger(h, qs, user):
    try:
        fid = int(qs.get("flat_id", ""))
    except ValueError:
        raise ValueError("Daire seçin")
    conn = db()
    rows, st = ledger(conn, fid), dues_state(conn, [fid])[fid]
    conn.close()
    h.json(200, {"ledger": rows, "state": st})


@route("POST", "/api/dues/settings", need="admin")
def r_dues_settings(h, qs, user):
    b = h.body()
    d = get_setting("dues", DUES_DEFAULT)
    d["mode"] = "arsa" if b.get("mode") == "arsa" else "esit"
    d["amount"], d["total"] = money(b.get("amount") or 0, "Aylık aidat"), money(b.get("total") or 0, "Aylık toplam")
    try:
        d["due_day"] = max(1, min(28, int(b.get("due_day") or 10)))
        d["late_pct"] = max(0.0, min(5.0, float(b.get("late_pct") if b.get("late_pct") not in (None, "") else 5)))
    except (TypeError, ValueError):
        raise ValueError("Son ödeme günü ve gecikme oranı sayı olmalı")
    d["iban"] = re.sub(r"\s+", " ", txt(b, "iban", 40).upper())
    d["account_name"] = txt(b, "account_name", 80)
    put_setting("dues", d)
    audit(user, "Aidat ayarları", f"{'arsa payına göre' if d['mode'] == 'arsa' else 'eşit'} · {d['total'] if d['mode'] == 'arsa' else d['amount']} TL · son gün {d['due_day']}")
    h.json(200, {"ok": True, "settings": d})


@route("POST", "/api/dues/generate", need="admin")
def r_dues_generate(h, qs, user):
    b = h.body()
    period = txt(b, "period", 7)
    if not PERIOD_RE.match(period):
        raise ValueError("Dönem YYYY-AA olmalı")
    d = get_setting("dues", DUES_DEFAULT)
    if (d["mode"] == "esit" and d["amount"] <= 0) or (d["mode"] == "arsa" and d["total"] <= 0):
        raise ValueError("Önce aidat tutarını ayarlayın")
    conn = db()
    flats = conn.execute("SELECT id, arsa_payi FROM flats").fetchall()
    share = flat_share(d, flats)
    due = f"{period}-{d['due_day']:02d}"
    have = {r[0] for r in conn.execute("SELECT flat_id FROM charges WHERE period=? AND kind='aidat'", (period,))}
    made, skipped, zero = 0, len(have), 0
    for fid, _ in flats:
        if fid in have:
            continue
        if share[fid] <= 0:
            zero += 1
            continue
        made += 1
        if not b.get("preview"):
            conn.execute("INSERT INTO charges (flat_id, period, kind, amount, due_date, note, ts, user) VALUES (?,?,?,?,?,?,?,?)",
                         (fid, period, "aidat", share[fid], due, "", time.time(), user["name"]))
    if not b.get("preview"):
        conn.commit()
        audit(user, "Aidat tahakkuku", f"{period} · {made} daire")
    conn.close()
    h.json(200, {"ok": True, "made": made, "skipped": skipped, "zero": zero, "due_date": due,
                 "total": round(sum(share[f] for f, _ in flats if f not in have), 2)})


def flat_ids_param(conn, b):
    ids = b.get("flat_ids")
    if not isinstance(ids, list) or not ids:
        conn.close()
        raise ValueError("Daire seçin")
    ids = [int(i) for i in ids[:1000]]
    found = {r[0] for r in conn.execute(f"SELECT id FROM flats WHERE id IN ({','.join('?' * len(ids))})", ids)}
    if len(found) != len(set(ids)):
        conn.close()
        raise ValueError("Daire bulunamadı")
    return ids


@route("POST", "/api/dues/charge", need="admin")
def r_dues_charge(h, qs, user):
    """Extra charge (ek ödeme, gecikme tazminatı, onarım payı) for one or many flats."""
    b = h.body()
    conn = db()
    ids = flat_ids_param(conn, b)
    amount = money(b.get("amount"))
    if amount <= 0:
        conn.close()
        raise ValueError("Tutar sıfırdan büyük olmalı")
    due = opt_date(b, "due_date") or datetime.now().strftime("%Y-%m-%d")
    kind = b.get("kind") if b.get("kind") in ("ek", "gecikme", "aidat") else "ek"
    note = txt(b, "note", 200)
    if not note and kind == "ek":
        conn.close()
        raise ValueError("Ek borcun açıklamasını yazın")
    for fid in ids:
        conn.execute("INSERT INTO charges (flat_id, period, kind, amount, due_date, note, ts, user) VALUES (?,?,?,?,?,?,?,?)",
                     (fid, due[:7], kind, amount, due, note, time.time(), user["name"]))
    conn.commit()
    conn.close()
    audit(user, "Borç yazıldı", f"{len(ids)} daire · {amount} TL · {note or kind}")
    h.json(200, {"ok": True, "count": len(ids)})


@route("POST", "/api/dues/pay", need="admin")
def r_dues_pay(h, qs, user):
    """Record payments. amount empty/None = each flat's whole current balance (bulk 'paid in full')."""
    b = h.body()
    conn = db()
    ids = flat_ids_param(conn, b)
    date = opt_date(b, "date") or datetime.now().strftime("%Y-%m-%d")
    method = b.get("method") if b.get("method") in PAY_METHODS else "havale"
    note = txt(b, "note", 200)
    fixed = None if b.get("amount") in (None, "") else money(b.get("amount"))
    st = dues_state(conn, ids)
    n, total = 0, 0.0
    for fid in ids:
        amt = fixed if fixed is not None else st[fid]["balance"]
        if amt <= 0:
            continue
        conn.execute("INSERT INTO payments (flat_id, date, amount, method, note, user, ts) VALUES (?,?,?,?,?,?,?)",
                     (fid, date, amt, method, note, user["name"], time.time()))
        n += 1
        total += amt
    conn.commit()
    conn.close()
    audit(user, "Tahsilat", f"{n} daire · {round(total, 2)} TL · {PAY_METHODS[method]}")
    h.json(200, {"ok": True, "count": n, "total": round(total, 2)})


@route("POST", "/api/dues/delete", need="admin")
def r_dues_delete(h, qs, user):
    b = h.body()
    table = {"charge": "charges", "payment": "payments"}.get(b.get("type"))
    if not table:
        raise ValueError("Kayıt türü geçersiz")
    conn = db()
    r = conn.execute(f"SELECT flat_id, amount FROM {table} WHERE id=?", (b.get("id"),)).fetchone()
    if r:
        conn.execute(f"DELETE FROM {table} WHERE id=?", (b.get("id"),))
        conn.commit()
    conn.close()
    if not r:
        return h.json(404, {"error": "Kayıt bulunamadı"})
    audit(user, "Aidat kaydı silindi", f"{'borç' if table == 'charges' else 'ödeme'} · daire {r[0]} · {r[1]} TL")
    h.json(200, {"ok": True})


@route("GET", "/api/dues.csv", need="admin")
def r_dues_csv(h, qs, user):
    conn = db()
    flats = registry(conn)
    st = dues_state(conn)
    conn.close()
    rows = [[f["blok"], f["daire"], ", ".join(p["name"] for p in f["people"] if not p["move_out"] and p["kind"] != "hane"),
             st[f["id"]]["charged"], st[f["id"]]["paid"], st[f["id"]]["balance"], st[f["id"]]["overdue"],
             st[f["id"]]["overdue_count"], st[f["id"]]["oldest_due"] or "", st[f["id"]]["late_fee"]] for f in flats]
    audit(user, "Aidat listesini indirdi", f"{len(rows)} daire")
    h.download(csv_bytes(["blok", "daire", "malik_kiraci", "toplam_borc", "toplam_odeme", "bakiye", "geciken", "geciken_kalem",
                          "en_eski_vade", "gecikme_tazminati_hesaplanan"], rows), f"adalya-aidat-{stamp()}.csv", "text/csv; charset=utf-8")


# ---------------------------------------------------------------- resident camera access (live picture only, never recordings)

def cam_channels():
    with lock:
        return {d["channel"]: d for d in devices if d.get("type") == "camera" and d.get("channel") and not d.get("hidden")}


def allowed_channels(flat_id):
    acc = get_setting("cam_access", CAM_DEFAULT)
    if not acc["enabled"] or not flat_id:
        return []
    conn = db()
    f = conn.execute("SELECT blok FROM flats WHERE id=?", (flat_id,)).fetchone()
    o = conn.execute("SELECT channels FROM flat_cams WHERE flat_id=?", (flat_id,)).fetchone()
    conn.close()
    if not f:
        return []
    chans = json.loads(o[0]) if o else acc["all"] + acc["blok"].get(f[0] or "", [])
    have = cam_channels()
    return sorted({int(c) for c in chans if int(c) in have})


def suggest_cam_access(bloks):
    """Default proposal from camera names: '<X> Blok …' cameras go to blok X; nothing is shared with all residents."""
    out = {b: [] for b in bloks}
    for ch, d in cam_channels().items():
        n = fold(d["name"])
        for b in bloks:
            if fold(b) + "blok" in n:
                out[b].append(ch)
    return out


@route("GET", "/api/cam-access", need="admin")
def r_cam_access(h, qs, user):
    conn = db()
    bloks = sorted({r[0] for r in conn.execute("SELECT DISTINCT blok FROM flats") if r[0]})
    over = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT flat_id, channels FROM flat_cams")}
    conn.close()
    h.json(200, {"access": get_setting("cam_access", CAM_DEFAULT), "bloks": bloks, "overrides": {str(k): v for k, v in over.items()},
                 "cameras": [{"channel": c, "name": d["name"]} for c, d in sorted(cam_channels().items())],
                 "suggest": suggest_cam_access(bloks)})


def chan_list(v):
    if not isinstance(v, list):
        raise ValueError("Kanal listesi geçersiz")
    return sorted({int(c) for c in v if str(c).isdigit() and 1 <= int(c) <= 64})


@route("POST", "/api/cam-access", need="admin")
def r_cam_access_save(h, qs, user):
    b = h.body()
    acc = {"enabled": bool(b.get("enabled")), "all": chan_list(b.get("all", [])),
           "blok": {str(k)[:10]: chan_list(v) for k, v in (b.get("blok") or {}).items()}}
    put_setting("cam_access", acc)
    audit(user, "Sakin kamera erişimi", f"{'açık' if acc['enabled'] else 'kapalı'} · tüm sakinler: {acc['all']} · " +
          ", ".join(f"{k}: {v}" for k, v in acc["blok"].items()))
    h.json(200, {"ok": True})


@route("POST", "/api/cam-access/flat", need="admin")
def r_cam_access_flat(h, qs, user):
    b = h.body()
    conn = db()
    need_flat(conn, b.get("flat_id"))
    if b.get("channels") is None:
        conn.execute("DELETE FROM flat_cams WHERE flat_id=?", (b["flat_id"],))
        detail = "varsayılan"
    else:
        ch = chan_list(b["channels"])
        conn.execute("INSERT OR REPLACE INTO flat_cams (flat_id, channels) VALUES (?,?)", (b["flat_id"], json.dumps(ch)))
        detail = str(ch)
    conn.commit()
    conn.close()
    audit(user, "Daire kamera erişimi", f"daire {b['flat_id']} · {detail}")
    h.json(200, {"ok": True})


@route("GET", "/api/my/cameras", need="user")
def r_my_cameras(h, qs, user):
    """For residents: the status payload limited to the cameras their flat may watch (no IPs, MACs or other devices)."""
    if user["role"] != "resident":
        return h.json(400, {"error": "Bu istek site sakinleri içindir"})
    allowed = set(allowed_channels(user.get("flat_id")))
    st = status_payload()
    cams = [{"name": d["name"], "ip": f"ch{d['channel']}", "type": "camera", "hidden": False, "channel": d["channel"],
             "x": d["x"], "y": d["y"], "up": d["up"], "since": d["since"], "rtt": None, "mac": "unknown",
             "drops_today": d["drops_today"], "uptime": d["uptime"]}
            for d in st["devices"] if d["type"] == "camera" and d.get("channel") in allowed and not d["hidden"]]
    n = st["nvr"]
    h.json(200, {"now": st["now"], "started": st["started"], "demo": st["demo"], "devices": cams, "events": [],
                 "nvr": {"connected": n["connected"]}, "has_plan": st["has_plan"], "plan_ver": st["plan_ver"]})


resident_views = set()


def resident_can_watch(user, ch):
    if user["role"] != "resident":
        return True
    if ch not in allowed_channels(user.get("flat_id")):
        return False
    key = (user["username"], ch, datetime.now().strftime("%Y-%m-%d"))
    if key not in resident_views:
        resident_views.add(key)
        audit(user, "Sakin kamera izledi", f"kanal {ch} · {channel_names().get(ch, '')}")
    return True


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
            m = re.search(r"adalya_demo_role=(admin|staff|resident)", self.headers.get("Cookie", ""))
            role = m.group(1) if m else "admin"
            return {**DEMO_USER, "role": role, "name": "Demo " + ROLES[role].lower(), "flat_id": 1 if role == "resident" else None}
        s = sessions.get(self.cookie_token())
        if s and s["exp"] > time.time():
            rec = cfg["users"].get(s["username"])
            if not rec:
                return None
            s["exp"] = time.time() + 12 * 3600
            return {"username": s["username"], "name": rec["name"], "role": rec["role"], "flat_id": rec.get("flat_id")}
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
        if fn.need != "none" and not user:
            return self.error(401, "Giriş gerekli")
        if fn.need == "admin" and user["role"] != "admin":
            return self.error(403, "Bu işlem yönetici yetkisi ister")
        if fn.need == "staff" and user["role"] not in ("admin", "staff"):
            return self.error(403, "Bu sayfa site sakinlerine kapalı")
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
        seed_demo_registry()

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
