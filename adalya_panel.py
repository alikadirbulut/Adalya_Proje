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
import shutil
import socket
import sqlite3
import tempfile
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

VERSION = "3.3"
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
CREATE TABLE IF NOT EXISTS flats (id INTEGER PRIMARY KEY, blok TEXT, kapi TEXT, daire TEXT, arsa_payi REAL, notes TEXT, kat TEXT);
CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY, flat_id INTEGER, name TEXT, phone TEXT, email TEXT, kind TEXT,
    move_in TEXT, move_out TEXT, emergency TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS vehicles (id INTEGER PRIMARY KEY, flat_id INTEGER, plate TEXT, plate_key TEXT, model TEXT,
    color TEXT, parking TEXT, sticker TEXT);
CREATE TABLE IF NOT EXISTS visitors (id INTEGER PRIMARY KEY, ts_in REAL, ts_out REAL, name TEXT, flat_id INTEGER,
    plate TEXT, purpose TEXT, user TEXT);
CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, ts REAL, flat_id INTEGER, user TEXT, text TEXT,
    status TEXT, reply TEXT, closed_by TEXT, closed_at REAL, category TEXT, photo TEXT);
CREATE TABLE IF NOT EXISTS charges (id INTEGER PRIMARY KEY, flat_id INTEGER, period TEXT, kind TEXT, amount REAL,
    due_date TEXT, note TEXT, ts REAL, user TEXT);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, flat_id INTEGER, date TEXT, amount REAL, method TEXT,
    note TEXT, user TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS flat_cams (flat_id INTEGER PRIMARY KEY, channels TEXT);
CREATE TABLE IF NOT EXISTS calendar_tasks (id INTEGER PRIMARY KEY, date TEXT, title TEXT, kind TEXT, note TEXT,
    done INTEGER DEFAULT 0, user TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS duty_done (duty_key TEXT, year INTEGER, ts REAL, user TEXT, PRIMARY KEY (duty_key, year));
CREATE TABLE IF NOT EXISTS contracts (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, provider TEXT, start_date TEXT,
    end_date TEXT, amount REAL, note TEXT, doc_id INTEGER, user TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, stored TEXT, size INTEGER,
    note TEXT, user TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY, ts REAL, title TEXT, body TEXT, kind TEXT,
    pinned INTEGER DEFAULT 0, user TEXT);
CREATE TABLE IF NOT EXISTS announcement_reads (ann_id INTEGER, username TEXT, ts REAL, PRIMARY KEY (ann_id, username));
CREATE TABLE IF NOT EXISTS guest_passes (id INTEGER PRIMARY KEY, flat_id INTEGER, name TEXT, plate TEXT, note TEXT,
    valid_from TEXT, valid_to TEXT, created_by TEXT, ts REAL, used_ts REAL, used_by TEXT);
CREATE TABLE IF NOT EXISTS packages (id INTEGER PRIMARY KEY, flat_id INTEGER, courier TEXT, note TEXT,
    ts_in REAL, ts_out REAL, user TEXT);
CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY, no INTEGER, year INTEGER, payment_id INTEGER UNIQUE,
    flat_id INTEGER, ts REAL, user TEXT);
CREATE TABLE IF NOT EXISTS budgets (id INTEGER PRIMARY KEY, year INTEGER UNIQUE, status TEXT DEFAULT 'taslak',
    note TEXT, notified_at REAL, objection_until TEXT, created_by TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS budget_lines (id INTEGER PRIMARY KEY, budget_id INTEGER, kind TEXT, label TEXT,
    amount REAL, note TEXT);
CREATE TABLE IF NOT EXISTS cash (id INTEGER PRIMARY KEY, date TEXT, kind TEXT, category TEXT, label TEXT,
    amount REAL, note TEXT, user TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, ts REAL, flat_id INTEGER, channel TEXT, subject TEXT,
    message TEXT, status TEXT DEFAULT 'bekliyor', sent_ts REAL, sent_by TEXT, user TEXT);
CREATE TABLE IF NOT EXISTS registrations (id INTEGER PRIMARY KEY, ts REAL, ref TEXT, name TEXT, phone TEXT, email TEXT,
    blok TEXT, kat TEXT, daire TEXT, kind TEXT, vehicles TEXT, device_count INTEGER, note TEXT, status TEXT DEFAULT 'bekliyor',
    flat_id INTEGER, decided_by TEXT, decided_at REAL, reject_reason TEXT, ip TEXT);
CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY, username TEXT, token_hash TEXT UNIQUE, label TEXT,
    ua TEXT, ip TEXT, first_ts REAL, last_ts REAL);
CREATE TABLE IF NOT EXISTS request_messages (id INTEGER PRIMARY KEY, request_id INTEGER, ts REAL, user TEXT,
    role TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, ts REAL, username TEXT, kind TEXT, title TEXT,
    body TEXT, link TEXT, read_ts REAL);
"""
USERNAME_RE = re.compile(r"^[a-z0-9._-]{2,32}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SITE_DEFAULT = {
    "site_name": "Adalya sitesi", "support_contact": "",
    "memorial": {"name": "Esin Noğan", "birth": "", "death": "", "death_time": "", "profession": "", "note": "",
                 "bio": "", "birth_place": "", "roots": "", "parents": "", "education": "", "raised": "", "note_ali": "",
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
login_fail = {}
pending_logins = {}
LOGIN_MAX, LOGIN_LOCK = 5, 30
IP_MAX, IP_LOCK = 25, 300


def login_blocked(ip, username):
    now = time.time()
    for key in (f"u:{ip}|{username}", f"ip:{ip}"):
        rec = login_fail.get(key)
        if rec and rec["until"] > now:
            return int(rec["until"] - now)
    return 0


def login_bad(ip, username):
    now = time.time()
    for key, maxn, lock in ((f"u:{ip}|{username}", LOGIN_MAX, LOGIN_LOCK), (f"ip:{ip}", IP_MAX, IP_LOCK)):
        rec = login_fail.setdefault(key, {"count": 0, "until": 0.0})
        rec["count"] += 1
        if rec["count"] >= maxn:
            rec.update(count=0, until=now + lock)
    if len(login_fail) > 3000:
        for k in [k for k, v in login_fail.items() if v["until"] < now - 3600]:
            login_fail.pop(k, None)


def login_ok(ip, username):
    login_fail.pop(f"u:{ip}|{username}", None)


def _b32(secret):
    import base64
    s = secret.upper().strip()
    return base64.b32decode(s + "=" * ((8 - len(s) % 8) % 8))


def totp_code(secret, at=None, step=30, digits=6):
    import struct
    counter = int((at or time.time()) // step)
    digest = hmac.new(_b32(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)).zfill(digits)


def totp_verify(secret, code):
    code = re.sub(r"\D", "", str(code or ""))
    if len(code) != 6 or not secret:
        return False
    now = time.time()
    return any(hmac.compare_digest(totp_code(secret, now + w), code) for w in (-30, 0, 30))


# ---------------------------------------------------------------- devices (max 2 per account)
# Store apps will send a per-install id in X-Device-Id; the web panel uses a long-lived cookie.
# MAC addresses are deliberately not used: they are invisible to browsers and spoofable.

def device_token(h):
    m = re.search(r"adalya_device=([A-Za-z0-9_-]+)", h.headers.get("Cookie", ""))
    return (h.headers.get("X-Device-Id") or (m.group(1) if m else "")).strip()[:200]


def device_cookie(h, tok):
    return f"adalya_device={tok}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax{h.cookie_secure()}"


def bind_device(h, username):
    """Return (token, created) or raise ValueError('DEVICE_LIMIT')."""
    tok = device_token(h)
    created = False
    if not tok:
        tok = secrets.token_urlsafe(32)
        created = True
    th = hashlib.sha256(tok.encode("utf-8")).hexdigest()
    now = time.time()
    conn = db()
    row = conn.execute("SELECT id FROM devices WHERE token_hash=?", (th,)).fetchone()
    if row:
        conn.execute("UPDATE devices SET last_ts=?, ip=? WHERE id=?", (now, h.client_address[0], row[0]))
        conn.commit()
        conn.close()
        return tok, created
    n = conn.execute("SELECT COUNT(*) FROM devices WHERE username=?", (username,)).fetchone()[0]
    if n >= 2:
        conn.close()
        raise ValueError("DEVICE_LIMIT")
    conn.execute("INSERT INTO devices (username, token_hash, label, ua, ip, first_ts, last_ts) VALUES (?,?,?,?,?,?,?)",
                 (username, th, f"Cihaz {n + 1}", h.headers.get("User-Agent", "")[:200], h.client_address[0], now, now))
    conn.commit()
    conn.close()
    return tok, created


def user_devices(username):
    conn = db()
    rows = conn.execute("SELECT label, ip, last_ts, ua FROM devices WHERE username=? ORDER BY first_ts", (username,)).fetchall()
    conn.close()
    return [{"label": r[0] or "", "ip": r[1] or "", "last_ts": r[2] or 0, "ua": r[3] or ""} for r in rows]


started = time.time()
args = None


# ---------------------------------------------------------------- storage

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS events (ip TEXT, name TEXT, down_at REAL, up_at REAL, seconds REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS notes (ts REAL, user TEXT, device TEXT, text TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS audit (ts REAL, user TEXT, role TEXT, action TEXT, detail TEXT)")
    conn.executescript(REGISTRY_SQL)
    migrate(conn)
    return conn


def migrate(conn):
    """Small forward-only migrations so an existing data/panel.db keeps working."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
    for col in ("category", "photo"):
        if col not in cols:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT")
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(flats)")}
    if "kat" not in fcols:
        conn.execute("ALTER TABLE flats ADD COLUMN kat TEXT")
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        if conn.execute("SELECT 1 FROM requests LIMIT 1").fetchone():
            conn.execute("INSERT INTO request_messages (request_id, ts, user, role, text) "
                         "SELECT r.id, r.ts, r.user, 'resident', r.text FROM requests r "
                         "WHERE NOT EXISTS (SELECT 1 FROM request_messages m WHERE m.request_id=r.id)")
            conn.execute("INSERT INTO request_messages (request_id, ts, user, role, text) "
                         "SELECT r.id, COALESCE(r.closed_at, r.ts), COALESCE(r.closed_by, 'Yönetim'), 'admin', r.reply FROM requests r "
                         "WHERE r.reply IS NOT NULL AND r.reply <> '' "
                         "AND NOT EXISTS (SELECT 1 FROM request_messages m WHERE m.request_id=r.id AND m.role='admin')")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

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
        user = {**user, "cams": len(allowed_channels(user.get("flat_id"))), "unread": unread_announcements(user["username"])}
    if user and user["role"] == "admin":
        user = {**user, "pending_regs": pending_registrations(), "open_requests": open_requests()}
    if user:
        user = {**user, "notifications": unread_notifications(user["username"])}
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
    b = h.body()
    username = str(b.get("username", "")).strip().lower()
    pw = str(b.get("password", ""))
    ip = h.client_address[0]
    wait = login_blocked(ip, username)
    if wait:
        return h.json(429, {"error": f"Çok fazla hatalı deneme. {wait} sn sonra tekrar deneyin."})
    rec = cfg["users"].get(username)
    if not rec or not check_pw(pw, rec.get("pw")):
        login_bad(ip, username)
        return h.json(401, {"error": "Kullanıcı adı veya şifre hatalı"})
    login_ok(ip, username)
    if rec.get("status") == "askida":
        return h.json(403, {"error": "Hesabınız askıya alınmış. Site yönetimine başvurun."})
    if rec.get("status") == "davetli":
        return h.json(401, {"error": "Hesabınız henüz etkin değil. Davet bağlantınızla şifre belirleyin."})
    if rec.get("totp_on") and rec.get("totp_secret"):
        if len(pending_logins) > 500:
            for k in [k for k, v in pending_logins.items() if v["exp"] < now]:
                pending_logins.pop(k, None)
        pend = secrets.token_urlsafe(24)
        pending_logins[pend] = {"username": username, "exp": now + 300}
        return h.json(200, {"twofa": True, "pending": pend})
    u = {"username": username, "name": rec["name"], "role": rec["role"], "flat_id": rec.get("flat_id")}
    cookies = None
    if not args.demo and rec.get("role") != "admin":
        try:
            tok, created = bind_device(h, username)
            if created:
                cookies = [device_cookie(h, tok)]
        except ValueError:
            return h.json(409, {"error": "Bu hesap en fazla 2 cihaza bağlanabilir. Yönetimden bir cihazı kaldırmasını isteyin.",
                                "device_limit": True})
    h.start_session(u, cookies)
    audit(u, "Giriş", "")


@route("POST", "/api/login/2fa", need="none")
def r_login_2fa(h, qs, user):
    b = h.body()
    pend = str(b.get("pending") or "")
    rec_p = pending_logins.get(pend)
    if not rec_p or rec_p["exp"] < time.time():
        pending_logins.pop(pend, None)
        raise ValueError("Doğrulama süresi doldu; yeniden giriş yapın")
    username = rec_p["username"]
    rec = cfg["users"].get(username)
    if not rec or not totp_verify(rec.get("totp_secret"), b.get("code")):
        return h.json(401, {"error": "Kod hatalı"})
    pending_logins.pop(pend, None)
    u = {"username": username, "name": rec["name"], "role": rec["role"], "flat_id": rec.get("flat_id")}
    cookies = None
    if not args.demo and rec.get("role") != "admin":
        try:
            tok, created = bind_device(h, username)
            if created:
                cookies = [device_cookie(h, tok)]
        except ValueError:
            return h.json(409, {"error": "Bu hesap en fazla 2 cihaza bağlanabilir.", "device_limit": True})
    h.start_session(u, cookies)
    audit(u, "Giriş", "iki adımlı doğrulama")


@route("POST", "/api/me/2fa/setup", need="admin")
def r_2fa_setup(h, qs, user):
    import base64
    rec = cfg["users"].get(user["username"])
    if not rec:
        raise ValueError("Kullanıcı bulunamadı")
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    rec["totp_secret"] = secret
    rec["totp_on"] = False
    save_cfg()
    issuer = site["site_name"]
    uri = (f"otpauth://totp/{quote(issuer)}:{quote(user['username'])}?secret={secret}"
           f"&issuer={quote(issuer)}&digits=6&period=30")
    h.json(200, {"ok": True, "secret": secret, "uri": uri})


@route("POST", "/api/me/2fa/enable", need="admin")
def r_2fa_enable(h, qs, user):
    rec = cfg["users"].get(user["username"])
    if not rec or not rec.get("totp_secret"):
        raise ValueError("Önce kurulumu başlatın")
    if not totp_verify(rec["totp_secret"], h.body().get("code")):
        raise ValueError("Kod hatalı; telefonunuzdaki güncel kodu girin")
    rec["totp_on"] = True
    save_cfg()
    audit(user, "Güvenlik", "İki adımlı doğrulama açıldı")
    h.json(200, {"ok": True})


@route("POST", "/api/me/2fa/disable", need="admin")
def r_2fa_disable(h, qs, user):
    rec = cfg["users"].get(user["username"])
    if not rec:
        raise ValueError("Kullanıcı bulunamadı")
    if not check_pw(h.body().get("password", ""), rec.get("pw")):
        raise ValueError("Şifre hatalı")
    rec.pop("totp_secret", None)
    rec.pop("totp_on", None)
    save_cfg()
    audit(user, "Güvenlik", "İki adımlı doğrulama kapatıldı")
    h.json(200, {"ok": True})


@route("GET", "/api/users", need="admin")
def r_users(h, qs, user):
    h.json(200, {"users": [{"username": k, "name": v["name"], "role": v["role"], "flat_id": v.get("flat_id"),
                            "status": v.get("status", "active"), "phone": v.get("phone", ""),
                            "invite": bool(v.get("invite")), "devices": user_devices(k)}
                           for k, v in sorted(cfg["users"].items(), key=lambda kv: (kv[1]["role"] != "admin", kv[1]["name"]))]})


@route("POST", "/api/users/status", need="admin")
def r_user_status(h, qs, user):
    b = h.body()
    username = str(b.get("username") or "").strip().lower()
    rec = cfg["users"].get(username)
    if not rec:
        raise ValueError("Kullanıcı bulunamadı")
    status = b.get("status")
    if status not in ("active", "askida", "davetli"):
        raise ValueError("Durum geçersiz")
    if username == user["username"] and status != "active":
        raise ValueError("Kendi hesabınızı askıya alamazsınız")
    rec["status"] = status
    if status != "davetli":
        rec.pop("invite", None)
    save_cfg()
    if status != "active":
        for tok in [t for t, s in sessions.items() if s["username"] == username]:
            sessions.pop(tok, None)
    audit(user, "Kullanıcı durumu", f"{username}: {status}")
    h.json(200, {"ok": True})


@route("POST", "/api/users/devices/reset", need="admin")
def r_user_devices_reset(h, qs, user):
    username = str(h.body().get("username") or "").strip().lower()
    if username not in cfg["users"]:
        raise ValueError("Kullanıcı bulunamadı")
    conn = db()
    conn.execute("DELETE FROM devices WHERE username=?", (username,))
    conn.commit()
    conn.close()
    audit(user, "Cihaz sıfırlama", username)
    h.json(200, {"ok": True})


@route("POST", "/api/users/invite", need="admin")
def r_user_invite(h, qs, user):
    username = str(h.body().get("username") or "").strip().lower()
    rec = cfg["users"].get(username)
    if not rec:
        raise ValueError("Kullanıcı bulunamadı")
    if rec.get("role") != "resident":
        raise ValueError("Davet yalnızca site sakinleri için")
    rec["status"] = "davetli"
    rec["invite"] = secrets.token_urlsafe(24)
    rec["invite_exp"] = time.time() + 14 * 86400
    rec.pop("pw", None)
    save_cfg()
    audit(user, "Davet", username)
    h.json(200, {"ok": True, "url": f"/davet?token={rec['invite']}"})


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
    rec.setdefault("status", "active")
    if b.get("phone"):
        rec["phone"] = txt(b, "phone", 20)
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
    conn = db()
    conn.execute("DELETE FROM devices WHERE username=?", (username,))
    conn.commit()
    conn.close()
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
    h.json(200, {"ok": True}, {"Set-Cookie": f"adalya_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{h.cookie_secure()}"})


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


@route("GET", "/api/search", need="staff")
def r_search(h, qs, user):
    q = str(qs.get("q") or "").strip()[:60]
    if len(q) < 2:
        return h.json(200, {"query": q, "results": []})
    conn = db()
    like = f"%{q}%"
    out = []
    for r in conn.execute("SELECT id, blok, daire FROM flats WHERE blok LIKE ? OR daire LIKE ? OR kapi LIKE ? "
                          "ORDER BY blok, CAST(daire AS INTEGER) LIMIT 4", (like, like, like)):
        out.append({"type": "flat", "title": flat_label({"blok": r[1], "daire": r[2]}), "sub": "Daire", "href": "#/sakinler"})
    for r in conn.execute("SELECT p.name, p.phone, p.kind, f.blok, f.daire FROM people p JOIN flats f ON f.id=p.flat_id "
                          "WHERE p.name LIKE ? OR p.phone LIKE ? ORDER BY p.name LIMIT 5", (like, like)):
        out.append({"type": "person", "title": r[0],
                    "sub": KINDS.get(r[2], r[2] or "") + " · " + flat_label({"blok": r[3], "daire": r[4]}), "href": "#/sakinler"})
    pk = plate_key(q)
    if len(pk) >= 2:
        for r in conn.execute("SELECT v.plate, v.model, v.color, f.blok, f.daire FROM vehicles v JOIN flats f ON f.id=v.flat_id "
                              "WHERE v.plate_key LIKE ? LIMIT 5", (f"%{pk}%",)):
            out.append({"type": "vehicle", "title": r[0], "sub": " · ".join(x for x in [r[1], r[2],
                        flat_label({"blok": r[3], "daire": r[4]})] if x), "href": "#/ziyaret"})
    if user["role"] == "admin":
        for r in conn.execute("SELECT id, name FROM documents WHERE name LIKE ? ORDER BY ts DESC LIMIT 4", (like,)):
            out.append({"type": "document", "title": r[1], "sub": "Belge", "href": f"/api/documents/file?id={r[0]}"})
        for r in conn.execute("SELECT id, name, end_date FROM contracts WHERE name LIKE ? OR provider LIKE ? LIMIT 4", (like, like)):
            out.append({"type": "contract", "title": r[1], "sub": "Sözleşme" + (f" · {r[2]}" if r[2] else ""), "href": "#/sozlesmeler"})
        for r in conn.execute("SELECT id, title FROM announcements WHERE title LIKE ? ORDER BY ts DESC LIMIT 4", (like,)):
            out.append({"type": "announcement", "title": r[1], "sub": "Duyuru", "href": "#/duyurular"})
    conn.close()
    h.json(200, {"query": q, "results": out[:20]})


@route("GET", "/api/audit", need="admin")
def r_audit(h, qs, user):
    conn = db()
    sql, args = "SELECT ts, user, role, action, detail FROM audit WHERE 1=1", []
    if qs.get("user"):
        sql += " AND user LIKE ?"; args.append(f"%{qs['user']}%")
    if qs.get("action"):
        sql += " AND action LIKE ?"; args.append(f"%{qs['action']}%")
    if qs.get("q"):
        sql += " AND detail LIKE ?"; args.append(f"%{qs['q']}%")
    if qs.get("from"):
        sql += " AND ts >= ?"; args.append(dparam(qs, "from", "1970-01-01").timestamp())
    if qs.get("to"):
        sql += " AND ts < ?"; args.append((dparam(qs, "to", datetime.now().strftime("%Y-%m-%d")) + timedelta(days=1)).timestamp())
    sql += " ORDER BY ts DESC LIMIT 500"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    h.json(200, {"audit": [{"ts": r[0], "user": r[1], "role": r[2], "action": r[3], "detail": r[4]} for r in rows]})


@route("GET", "/api/audit.csv", need="admin")
def r_audit_csv(h, qs, user):
    conn = db()
    sql, args = "SELECT ts, user, role, action, detail FROM audit WHERE 1=1", []
    if qs.get("user"):
        sql += " AND user LIKE ?"; args.append(f"%{qs['user']}%")
    if qs.get("action"):
        sql += " AND action LIKE ?"; args.append(f"%{qs['action']}%")
    if qs.get("q"):
        sql += " AND detail LIKE ?"; args.append(f"%{qs['q']}%")
    if qs.get("from"):
        sql += " AND ts >= ?"; args.append(dparam(qs, "from", "1970-01-01").timestamp())
    if qs.get("to"):
        sql += " AND ts < ?"; args.append((dparam(qs, "to", datetime.now().strftime("%Y-%m-%d")) + timedelta(days=1)).timestamp())
    sql += " ORDER BY ts DESC LIMIT 20000"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    data = csv_bytes(["Zaman", "Kişi", "Rol", "İşlem", "Ayrıntı"],
                     [[fmt_ts(r[0]), r[1], ROLES.get(r[2], r[2]), r[3], r[4]] for r in rows])
    audit(user, "Dışa aktarım", f"İşlem kaydı CSV ({len(rows)} satır)")
    h.download(data, f"adalya-islem-kaydi-{stamp()}.csv", "text/csv; charset=utf-8")


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
    safe = re.sub(r"[^\w-]+", "-", channel_names().get(ch, f"kanal{ch}")).strip("-")  # kept outside the f-string for Python < 3.12
    name = f"{safe}_kanal{ch}_{os.path.basename(path)}"
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
        for k, limit in (("name", 80), ("profession", 80), ("note", 4000), ("created_by", 120),
                         ("birth_place", 80), ("roots", 160), ("parents", 160), ("education", 200),
                         ("raised", 80), ("bio", 4000), ("note_ali", 2000)):
            if k in src:
                m[k] = str(src[k])[:limit]
        if "death_time" in src:
            v = str(src["death_time"])
            if v and not re.match(r"^\d{2}:\d{2}$", v):
                raise ValueError("Saat biçimi SS:DD olmalı")
            m["death_time"] = v
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
    if args.demo:
        return h.json(400, {"error": "Demo modunda fotoğraf değiştirilmez"})
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


@route("GET", "/media/onboarding.mp4", need="none")
def r_onboarding_video(h, qs, user):
    path = os.path.join(HERE, "media", "onboarding.mp4")
    if not os.path.isfile(path):
        return h.json(404, {"error": "Video yok"})
    size = os.path.getsize(path)
    start, end, code = 0, size - 1, 200
    rh = h.headers.get("Range", "")
    if rh.startswith("bytes="):
        try:
            a, b = rh[6:].split(",")[0].split("-", 1)
            if a == "":
                start = max(0, size - int(b))
            else:
                start = int(a)
                end = int(b) if b else size - 1
            end = min(end, size - 1)
            start = min(start, end)
            code = 206
        except (ValueError, TypeError):
            return h.json(416, {"error": "Aralık geçersiz"})
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start + 1)
    extra = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400"}
    if code == 206:
        extra["Content-Range"] = f"bytes {start}-{end}/{size}"
    h.raw(code, data, "video/mp4", extra)


@route("GET", "/media/onboarding-poster.jpg", need="none")
def r_onboarding_poster(h, qs, user):
    path = os.path.join(HERE, "media", "onboarding-poster.jpg")
    if not os.path.isfile(path):
        return h.json(404, {"error": "Afiş yok"})
    with open(path, "rb") as f:
        h.raw(200, f.read(), "image/jpeg")


def plan_file():
    for ext, ctype in (("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg"), ("webp", "image/webp")):
        p = os.path.join(HERE, f"site-plan.{ext}")
        if os.path.isfile(p):
            return p, ctype
    return None, None


@route("GET", "/manifest.webmanifest", need="none")
def r_manifest(h, qs, user):
    data = {"name": site["site_name"], "short_name": "Adalya", "id": "/", "start_url": "/", "scope": "/",
            "display": "standalone", "display_override": ["standalone", "minimal-ui"],
            "background_color": "#f7f6f3", "theme_color": "#1d2f28", "lang": "tr", "dir": "ltr",
            "categories": ["productivity", "utilities"],
            "description": "Adalya Sitesi yönetim paneli",
            "icons": [{"src": "/icon-192.png?v=2", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-512.png?v=2", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-maskable-512.png?v=2", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    h.raw(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/manifest+json")


@route("GET", "/icon-192.png", need="none")
@route("GET", "/icon-512.png", need="none")
@route("GET", "/apple-touch-icon.png", need="none")
@route("GET", "/icon-maskable-512.png", need="none")
def r_icon_png(h, qs, user):
    name = os.path.basename(h.path.split("?")[0])
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return h.json(404, {"error": "Simge bulunamadı"})
    with open(path, "rb") as fh:
        data = fh.read()
    h.raw(200, data, "image/png")


@route("GET", "/sw.js", need="none")
def r_sw(h, qs, user):
    js = """
const CACHE = "adalya-v4";
self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/", "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png", "/apple-touch-icon.png", "/manifest.webmanifest"])).catch(() => {}));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.pathname.startsWith("/api/") || u.pathname === "/sw.js") return;
  e.respondWith(fetch(e.request).then((r) => {
    const copy = r.clone();
    caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
    return r;
  }).catch(() => caches.match(e.request).then((m) => m || caches.match("/"))));
});
""".strip()
    h.raw(200, js.encode("utf-8"), "application/javascript; charset=utf-8")


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


# ---------------------------------------------------------------- management: calendar, contracts, documents
# The manager's working tools for the KMK 35-39 duties. They help, they are not legal advice:
# the yönetim planı and a lawyer have the final word. Contracts and documents are admin-only
# (they can carry personal data), every change and every download is written to the audit log.

TASK_KINDS = {"gorev": "Görev", "toplanti": "Toplantı", "bakim": "Bakım", "hukuki": "Hukuki", "diger": "Diğer"}
CONTRACT_KINDS = {"sigorta": "Sigorta poliçesi", "asansor": "Asansör bakım", "temizlik": "Temizlik",
                  "guvenlik": "Güvenlik", "peyzaj": "Peyzaj / bahçe", "bakim": "Genel bakım", "diger": "Diğer"}
DOC_KINDS = {"sozlesme": "Sözleşme", "police": "Poliçe", "karar": "Karar defteri", "plan": "Plan / proje",
             "makbuz": "Makbuz", "diger": "Diğer"}

# Fixed yearly duties: key, month, day, title, law, detail. Keys are stable: completion is stored per key and year.
LEGAL_DUTIES = [
    ("isletme", 12, 1, "İşletme projesini (yıllık bütçe) hazırlayın", "KMK 37",
     "Gelir-gider tahmini ve daire başına avanç. Sahiplerine yazılı bildirin; 7 gün içinde itiraz edilmezse kesinleşir."),
    ("davet", 1, 16, "Genel kurul davetiyelerini gönderin", "KMK 35",
     "Toplantıdan en az 15 gün önce, gündemle birlikte imza karşılığı veya taahhütlü posta."),
    ("kurul", 1, 31, "Olağan genel kurulu yapın", "KMK 35",
     "Ocak ayı içinde olağan genel kurul; karar yeter sayısı yönetim planına göre hesaplanır."),
    ("hesap", 1, 31, "Yıllık hesapları kurula sunun", "KMK 39",
     "Ocak ayında (veya yönetim planındaki tarihte) yıllık hesap raporunu sunun."),
    ("defter", 1, 31, "Karar defterini noterde kapatın", "KMK 36",
     "Yıl sonundan sonra en geç bir ay içinde noter kapanışı; kararları tarih sırasıyla ve tüm belgelerle saklayın."),
    ("mutabakat", 12, 31, "Yıl sonu kasa ve banka mutabakatı", "KMK 35 / 39",
     "Kasa, banka ve aidat kayıtlarını karşılaştırın; belgeleri arşivleyin."),
]
# Duties that come back every month (completion is stored per month: key-AA).
MONTHLY_DUTIES = [
    ("asansor", "Asansör aylık bakımı", "Yönetim planı / pratik",
     "Aylık bakımı yaptırın ve kaydını saklayın. Kırmızı etiket varsa asansörü kullanımdan kaldırın."),
]
# One-click task templates for the practical yearly duties that have no fixed date.
TASK_TEMPLATES = [
    ("asansor-yillik", "Asansör yıllık A tipi muayene", "bakim", "03-15"),
    ("sigorta", "Sigorta poliçelerini yenile", "hukuki", "09-01"),
    ("isg", "İSG risk değerlendirmesi ve çalışan sağlık taraması", "hukuki", "02-01"),
    ("yangin", "Yangın söndürme cihazlarının yıllık kontrolü", "bakim", "04-01"),
]

DOC_MAX = 10_000_000
DOC_EXT = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "jpg": "image/jpeg", "jpeg": "image/jpeg",
           "png": "image/png", "webp": "image/webp", "doc": "application/msword",
           "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "xls": "application/vnd.ms-excel",
           "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
DOC_MAGIC = {"pdf": (b"%PDF",), "jpg": (b"\xff\xd8",), "jpeg": (b"\xff\xd8",), "png": (b"\x89PNG",),
             "webp": (b"RIFF",), "docx": (b"PK",), "xlsx": (b"PK",), "doc": (b"\xd0\xcf\x11\xe0",),
             "xls": (b"\xd0\xcf\x11\xe0",)}


def doc_dir():
    return os.path.join(DATA, "belgeler")


def dparse(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def day_diff(s):
    d = dparse(s)
    return None if d is None else (d - datetime.now().date()).days


def duty_occurrences(year):
    conn = db()
    done = {r[0] for r in conn.execute("SELECT duty_key FROM duty_done WHERE year=?", (year,))}
    conn.close()
    out = []
    for key, m, d, title, law, detail in LEGAL_DUTIES:
        out.append({"key": key, "date": f"{year}-{m:02d}-{d:02d}", "title": title, "law": law,
                    "detail": detail, "monthly": False, "done": key in done})
    for key, title, law, detail in MONTHLY_DUTIES:
        for m in range(1, 13):
            k = f"{key}-{m:02d}"
            out.append({"key": k, "date": f"{year}-{m:02d}-01", "title": title, "law": law,
                        "detail": detail, "monthly": True, "done": k in done})
    return out


def task_rows(conn, year):
    rows = conn.execute("SELECT id, date, title, kind, note, done, user, ts FROM calendar_tasks "
                        "WHERE substr(date,1,4)=? ORDER BY date, id", (str(year),)).fetchall()
    return [{"id": r[0], "date": r[1], "title": r[2], "kind": r[3], "note": r[4] or "",
             "done": bool(r[5]), "user": r[6] or "", "ts": r[7]} for r in rows]


def contract_rows(conn):
    docs = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM documents")}
    rows = conn.execute("SELECT id, name, kind, provider, start_date, end_date, amount, note, doc_id, user, ts "
                        "FROM contracts ORDER BY end_date IS NULL, end_date, name").fetchall()
    out = []
    for r in rows:
        days = day_diff(r[5])
        if days is None:
            status = "none"
        elif days < 0:
            status = "expired"
        elif days <= 30:
            status = "soon"
        else:
            status = "valid"
        out.append({"id": r[0], "name": r[1], "kind": r[2], "provider": r[3] or "", "start_date": r[4] or "",
                    "end_date": r[5] or "", "amount": r[6] or 0, "note": r[7] or "", "doc_id": r[8],
                    "doc_name": docs.get(r[8], ""), "user": r[9] or "", "ts": r[10],
                    "status": status, "days_left": days})
    return out


def document_rows(conn):
    rows = conn.execute("SELECT id, name, kind, stored, size, note, user, ts FROM documents ORDER BY ts DESC").fetchall()
    return [{"id": r[0], "name": r[1], "kind": r[2], "size": r[4] or 0, "note": r[5] or "",
             "user": r[6] or "", "ts": r[7], "filename": r[1] + (os.path.splitext(r[3] or "")[1])} for r in rows]


def alerts_payload():
    """What the manager should not miss: legal dates, open tasks and contracts near expiry."""
    today = datetime.now().date()
    year = today.year
    duties = []
    for d in duty_occurrences(year):
        if d["done"]:
            continue
        left = (dparse(d["date"]) - today).days
        if -30 <= left <= 21:
            duties.append({**d, "days_left": left})
    duties.sort(key=lambda x: x["date"])
    conn = db()
    tasks = []
    for t in task_rows(conn, year):
        left = (dparse(t["date"]) - today).days
        if not t["done"] and -30 <= left <= 30:
            tasks.append({**t, "days_left": left})
    tasks.sort(key=lambda x: x["date"])
    contracts = [c for c in contract_rows(conn) if c["status"] in ("soon", "expired")]
    conn.close()
    return {"today": today.isoformat(), "duties": duties[:4], "tasks": tasks[:4], "contracts": contracts[:4],
            "counts": {"duties": len(duties), "tasks": len(tasks), "contracts": len(contracts)}}


@route("GET", "/api/alerts", need="admin")
def r_alerts(h, qs, user):
    h.json(200, alerts_payload())


@route("GET", "/api/calendar", need="admin")
def r_calendar(h, qs, user):
    try:
        year = int(qs.get("year") or datetime.now().year)
    except (TypeError, ValueError):
        raise ValueError("Yıl geçersiz")
    if not 2000 <= year <= 2100:
        raise ValueError("Yıl geçersiz")
    conn = db()
    tasks = task_rows(conn, year)
    conn.close()
    h.json(200, {"year": year, "today": datetime.now().date().isoformat(), "duties": duty_occurrences(year),
                 "tasks": tasks, "task_kinds": TASK_KINDS, "templates": [
                     {"key": k, "title": t, "kind": kd, "date": f"{year}-{md}"} for k, t, kd, md in TASK_TEMPLATES]})


@route("POST", "/api/calendar/task", need="admin")
def r_task_save(h, qs, user):
    b = h.body()
    date = opt_date(b, "date")
    if not date:
        raise ValueError("Tarih gerekli")
    title = txt(b, "title", 120)
    if not title:
        raise ValueError("Başlık gerekli")
    kind = b.get("kind") if b.get("kind") in TASK_KINDS else "gorev"
    note = txt(b, "note", 2000)
    try:
        tid = int(b.get("id") or 0)
    except (TypeError, ValueError):
        tid = 0
    conn = db()
    if tid:
        cur = conn.execute("UPDATE calendar_tasks SET date=?, title=?, kind=?, note=? WHERE id=?",
                           (date, title, kind, note, tid))
        if not cur.rowcount:
            conn.close()
            raise ValueError("Görev bulunamadı")
        action = "Takvim görevi düzenlendi"
    else:
        tid = conn.execute("INSERT INTO calendar_tasks (date, title, kind, note, done, user, ts) VALUES (?,?,?,?,0,?,?)",
                           (date, title, kind, note, user["name"], time.time())).lastrowid
        action = "Takvim görevi eklendi"
    conn.commit()
    conn.close()
    audit(user, "Takvim", f"{action}: {date} {title}")
    h.json(200, {"ok": True, "id": tid})


@route("POST", "/api/calendar/task/done", need="admin")
def r_task_done(h, qs, user):
    b = h.body()
    conn = db()
    r = conn.execute("SELECT title FROM calendar_tasks WHERE id=?", (b.get("id"),)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Görev bulunamadı")
    done = 1 if b.get("done") else 0
    conn.execute("UPDATE calendar_tasks SET done=? WHERE id=?", (done, b.get("id")))
    conn.commit()
    conn.close()
    audit(user, "Takvim", f"Görev {'tamamlandı' if done else 'yeniden açıldı'}: {r[0]}")
    h.json(200, {"ok": True})


@route("POST", "/api/calendar/task/delete", need="admin")
def r_task_delete(h, qs, user):
    return delete_row(h, user, "calendar_tasks", "title", "Takvim görevi silindi")


@route("POST", "/api/calendar/duty", need="admin")
def r_duty_done(h, qs, user):
    b = h.body()
    key = txt(b, "key", 40)
    try:
        year = int(b.get("year"))
    except (TypeError, ValueError):
        raise ValueError("Yıl geçersiz")
    if not key or not 2000 <= year <= 2100:
        raise ValueError("Kayıt geçersiz")
    valid = {d["key"] for d in duty_occurrences(year)}
    if key not in valid:
        raise ValueError("Bu tarih takvimde yok")
    conn = db()
    if b.get("done"):
        conn.execute("INSERT OR REPLACE INTO duty_done (duty_key, year, ts, user) VALUES (?,?,?,?)",
                     (key, year, time.time(), user["name"]))
    else:
        conn.execute("DELETE FROM duty_done WHERE duty_key=? AND year=?", (key, year))
    conn.commit()
    conn.close()
    audit(user, "Takvim", f"{'Tamamlandı' if b.get('done') else 'Geri alındı'}: {key} ({year})")
    h.json(200, {"ok": True})


@route("GET", "/api/contracts", need="admin")
def r_contracts(h, qs, user):
    conn = db()
    data = {"contracts": contract_rows(conn), "kinds": CONTRACT_KINDS,
            "documents": [{"id": d["id"], "name": d["name"]} for d in document_rows(conn)]}
    conn.close()
    h.json(200, data)


@route("POST", "/api/contracts/save", need="admin")
def r_contract_save(h, qs, user):
    b = h.body()
    name = txt(b, "name", 120)
    if not name:
        raise ValueError("Sözleşme adı gerekli")
    kind = b.get("kind") if b.get("kind") in CONTRACT_KINDS else "diger"
    provider = txt(b, "provider", 120)
    start = opt_date(b, "start_date")
    end = opt_date(b, "end_date")
    if start and end and start > end:
        raise ValueError("Bitiş tarihi başlangıçtan önce olamaz")
    amount = money(b.get("amount") or 0, "Tutar") if str(b.get("amount") or "").strip() else 0
    note = txt(b, "note", 2000)
    doc_id = None
    if b.get("doc_id"):
        try:
            doc_id = int(b["doc_id"])
        except (TypeError, ValueError):
            raise ValueError("Belge seçimi geçersiz")
    try:
        cid = int(b.get("id") or 0)
    except (TypeError, ValueError):
        cid = 0
    conn = db()
    if doc_id and not conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
        conn.close()
        raise ValueError("Belge bulunamadı")
    if cid:
        cur = conn.execute("UPDATE contracts SET name=?, kind=?, provider=?, start_date=?, end_date=?, amount=?, note=?, doc_id=? WHERE id=?",
                           (name, kind, provider, start, end, amount, note, doc_id, cid))
        if not cur.rowcount:
            conn.close()
            raise ValueError("Sözleşme bulunamadı")
        action = "Sözleşme düzenlendi"
    else:
        cid = conn.execute("INSERT INTO contracts (name, kind, provider, start_date, end_date, amount, note, doc_id, user, ts) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (name, kind, provider, start, end, amount, note, doc_id, user["name"], time.time())).lastrowid
        action = "Sözleşme eklendi"
    conn.commit()
    conn.close()
    audit(user, "Sözleşme", f"{action}: {name}" + (f" · bitiş {end}" if end else ""))
    h.json(200, {"ok": True, "id": cid})


@route("POST", "/api/contracts/delete", need="admin")
def r_contract_delete(h, qs, user):
    return delete_row(h, user, "contracts", "name", "Sözleşme silindi")


def decode_doc(b):
    """A document arrives as base64 in JSON (same idea as the memorial photo: no multipart parser)."""
    import base64
    raw = str(b.get("data") or "")
    m = re.match(r"^data:([\w/+.-]+);base64,(.*)$", raw, re.S)
    if m:
        raw = m.group(2)
    try:
        data = base64.b64decode(raw, validate=False)
    except (ValueError, TypeError):
        raise ValueError("Dosya okunamadı")
    if not data:
        raise ValueError("Dosya boş")
    if len(data) > DOC_MAX:
        raise ValueError("Dosya en fazla 10 MB olabilir")
    fname = os.path.basename(str(b.get("filename") or "").replace("\\", "/")).strip()[:120]
    ext = os.path.splitext(fname)[1].lstrip(".").lower()
    if ext not in DOC_EXT:
        raise ValueError("Bu dosya türü desteklenmiyor (PDF, Word, Excel, JPG, PNG, WEBP, TXT)")
    magic = DOC_MAGIC.get(ext)
    if magic and not data.startswith(magic):
        raise ValueError("Dosya içeriği uzantısıyla uyuşmuyor")
    return data, fname or f"belge.{ext}", ext


@route("GET", "/api/documents", need="admin")
def r_documents(h, qs, user):
    conn = db()
    data = {"documents": document_rows(conn), "kinds": DOC_KINDS, "max_mb": DOC_MAX // 1_000_000}
    conn.close()
    h.json(200, data)


@route("POST", "/api/documents", need="admin")
def r_document_add(h, qs, user):
    b = h.body(max_bytes=16_000_000)
    data, fname, ext = decode_doc(b)
    name = txt(b, "name", 120) or os.path.splitext(fname)[0][:120]
    kind = b.get("kind") if b.get("kind") in DOC_KINDS else "diger"
    note = txt(b, "note", 1000)
    os.makedirs(doc_dir(), exist_ok=True)
    conn = db()
    did = conn.execute("INSERT INTO documents (name, kind, stored, size, note, user, ts) VALUES (?,?,?,?,?,?,?)",
                       (name, kind, "", 0, note, user["name"], time.time())).lastrowid
    stored = f"{did}-{secrets.token_hex(6)}.{ext}"
    with open(os.path.join(doc_dir(), stored), "wb") as f:
        f.write(data)
    conn.execute("UPDATE documents SET stored=?, size=? WHERE id=?", (stored, len(data), did))
    conn.commit()
    conn.close()
    audit(user, "Belge", f"Belge yüklendi: {name} ({fname}, {len(data)} bayt)")
    h.json(200, {"ok": True, "id": did})


@route("GET", "/api/documents/file", need="admin")
def r_document_file(h, qs, user):
    conn = db()
    r = conn.execute("SELECT name, kind, stored, size FROM documents WHERE id=?", (qs.get("id"),)).fetchone()
    conn.close()
    if not r:
        raise ValueError("Belge bulunamadı")
    p = os.path.join(doc_dir(), os.path.basename(r[2] or ""))
    if not r[2] or not os.path.isfile(p):
        raise ValueError("Belge dosyası diskte yok")
    ext = os.path.splitext(r[2])[1].lstrip(".").lower()
    with open(p, "rb") as f:
        data = f.read()
    audit(user, "Belge indirme", r[0])
    h.download(data, f"{r[0]}.{ext}", DOC_EXT.get(ext, "application/octet-stream"))


@route("POST", "/api/documents/delete", need="admin")
def r_document_delete(h, qs, user):
    did = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT name, stored FROM documents WHERE id=?", (did,)).fetchone()
    if not r:
        conn.close()
        return h.json(404, {"error": "Belge bulunamadı"})
    conn.execute("DELETE FROM documents WHERE id=?", (did,))
    conn.execute("UPDATE contracts SET doc_id=NULL WHERE doc_id=?", (did,))
    conn.commit()
    conn.close()
    try:
        if r[1]:
            os.remove(os.path.join(doc_dir(), os.path.basename(r[1])))
    except OSError:
        pass
    audit(user, "Belge", f"Belge silindi: {r[0]}")
    h.json(200, {"ok": True})


# ---------------------------------------------------------------- backups
# data/ is the only copy of accounts, residents, history and documents. A daily zip next to it
# (data/yedekler/) plus a tested restore path is the difference between an outage and a disaster.

BACKUP_DEFAULT = {"auto": True, "hour": 3, "keep": 14}


def backup_dir():
    return os.path.join(DATA, "yedekler")


def backup_list():
    d = backup_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for n in os.listdir(d):
        if not n.endswith(".zip"):
            continue
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": n, "size": st.st_size, "ts": st.st_mtime})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def create_backup(tag="manuel"):
    os.makedirs(backup_dir(), exist_ok=True)
    name = f"adalya-yedek-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}.zip"
    path = os.path.join(backup_dir(), name)
    tmpdb = os.path.join(backup_dir(), ".tmp-backup.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmpdb)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmpdb, "panel.db")
            for f in (CFG_PATH, SITE_PATH, DEVICES):
                if os.path.isfile(f):
                    z.write(f, os.path.basename(f))
            for sub in ("belgeler", "talepler"):
                d = os.path.join(DATA, sub)
                if os.path.isdir(d):
                    for n in os.listdir(d):
                        p = os.path.join(d, n)
                        if os.path.isfile(p):
                            z.write(p, f"{sub}/{n}")
    finally:
        try:
            os.remove(tmpdb)
        except OSError:
            pass
    keep = int(get_setting("backup", BACKUP_DEFAULT).get("keep") or 14)
    for old in backup_list()[keep:]:
        try:
            os.remove(os.path.join(backup_dir(), old["name"]))
        except OSError:
            pass
    return name


@route("GET", "/api/backups", need="admin")
def r_backups(h, qs, user):
    h.json(200, {"backups": backup_list(), "settings": get_setting("backup", BACKUP_DEFAULT),
                 "dir": "data/yedekler"})


@route("POST", "/api/backups", need="admin")
def r_backup_create(h, qs, user):
    name = create_backup("manuel")
    audit(user, "Yedek", f"Yedek alındı: {name}")
    h.json(200, {"ok": True, "name": name})


@route("POST", "/api/backups/settings", need="admin")
def r_backup_settings(h, qs, user):
    b = h.body()
    cur = get_setting("backup", BACKUP_DEFAULT)
    if "auto" in b:
        cur["auto"] = bool(b["auto"])
    if "hour" in b:
        try:
            cur["hour"] = max(0, min(23, int(b["hour"])))
        except (TypeError, ValueError):
            raise ValueError("Saat 0–23 olmalı")
    if "keep" in b:
        try:
            cur["keep"] = max(3, min(60, int(b["keep"])))
        except (TypeError, ValueError):
            raise ValueError("Saklama sayısı 3–60 olmalı")
    put_setting("backup", cur)
    audit(user, "Yedek", f"Ayarlar: otomatik={cur['auto']} saat={cur['hour']} adet={cur['keep']}")
    h.json(200, {"ok": True, "settings": cur})


@route("GET", "/api/backups/download", need="admin")
def r_backup_download(h, qs, user):
    name = os.path.basename(str(qs.get("name") or ""))
    p = os.path.join(backup_dir(), name)
    if not name.endswith(".zip") or not os.path.isfile(p):
        raise ValueError("Yedek bulunamadı")
    with open(p, "rb") as f:
        data = f.read()
    audit(user, "Yedek indirme", name)
    h.download(data, name, "application/zip")


@route("POST", "/api/backups/restore", need="admin")
def r_backup_restore(h, qs, user):
    b = h.body()
    name = os.path.basename(str(b.get("name") or ""))
    src_zip = os.path.join(backup_dir(), name)
    if not name.endswith(".zip") or not os.path.isfile(src_zip):
        raise ValueError("Yedek bulunamadı")
    if str(b.get("confirm") or "").strip().upper() != "GERI YUKLE":
        raise ValueError("Onay için GERI YUKLE yazın")
    safety = create_backup("geri-yukleme-oncesi")
    tmp_db = os.path.join(DATA, "restore-tmp.db")
    try:
        with zipfile.ZipFile(src_zip) as z:
            names = set(z.namelist())
            if "panel.db" not in names:
                raise ValueError("Yedekte panel.db yok")
            with z.open("panel.db") as f, open(tmp_db, "wb") as out:
                shutil.copyfileobj(f, out)
            for fname, target in (("config.json", CFG_PATH), ("site.json", SITE_PATH), ("devices.json", DEVICES)):
                if fname in names:
                    p = target + ".tmp"
                    with open(p, "wb") as out:
                        out.write(z.read(fname))
                    os.replace(p, target)
            for member in names:
                if member.startswith(("belgeler/", "talepler/")) and ".." not in member:
                    dest = os.path.join(DATA, *member.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(member) as f, open(dest, "wb") as out:
                        shutil.copyfileobj(f, out)
        # Restore INTO the live database (SQLite backup API): replacing the file fails on Windows
        # while the poller or a request still holds a connection.
        busy = None
        for _ in range(6):
            try:
                uri = "file:" + quote(tmp_db.replace(os.sep, "/"), safe="/:") + "?mode=ro"
                src = sqlite3.connect(uri, uri=True)
                dst = sqlite3.connect(DB_PATH, timeout=15)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                busy = None
                break
            except sqlite3.OperationalError as e:
                busy = e
                time.sleep(0.4)
        if busy is not None:
            raise ValueError("Veritabanı meşgul; birkaç saniye sonra tekrar deneyin")
    except PermissionError as e:
        raise ValueError(f"Geri yükleme dosya hatası: {e}")
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass
    load_cfg()
    load_site()
    load_devices()
    audit(user, "Yedek", f"Geri yüklendi: {name} (güvenlik yedeği: {safety})")
    h.json(200, {"ok": True, "safety": safety, "note": "Panel sayfalarını yenileyin."})


def backup_worker():
    while True:
        try:
            st = get_setting("backup", BACKUP_DEFAULT)
            if st.get("auto"):
                hour = int(st.get("hour") or 3)
                last = max([b["ts"] for b in backup_list()] or [0])
                if datetime.now().hour >= hour and time.time() - last > 20 * 3600:
                    name = create_backup("otomatik")
                    audit({"name": "Sistem", "role": "system"}, "Yedek", f"Otomatik yedek: {name}")
        except Exception:
            pass
        time.sleep(600)


# ---------------------------------------------------------------- announcements, guest passes, packages
# Announcements and guest passes are personal-data light but still KVKK-relevant: the gate sees
# only name, plate and flat; read receipts are usernames, never exposed to residents.

ANN_KINDS = {"duyuru": "Duyuru", "ariza": "Arıza / kesinti", "toplanti": "Toplantı", "guvenlik": "Güvenlik"}


def user_name(username):
    rec = cfg.get("users", {}).get(username)
    return rec.get("name", username) if isinstance(rec, dict) else username


def unread_announcements(username):
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM announcements a WHERE NOT EXISTS "
                     "(SELECT 1 FROM announcement_reads r WHERE r.ann_id=a.id AND r.username=?)", (username,)).fetchone()[0]
    conn.close()
    return n


def pending_registrations():
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM registrations WHERE status='bekliyor'").fetchone()[0]
    conn.close()
    return n


def unread_notifications(username):
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM notifications WHERE username=? AND read_ts IS NULL", (username,)).fetchone()[0]
    conn.close()
    return n


def notify(username, kind, title, body, link=""):
    conn = db()
    conn.execute("INSERT INTO notifications (ts, username, kind, title, body, link) VALUES (?,?,?,?,?,?)",
                 (time.time(), username, kind, title, body, link))
    conn.commit()
    conn.close()


def open_requests():
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM requests WHERE status='acik'").fetchone()[0]
    conn.close()
    return n


def pass_state(p, now_str):
    if p.get("used_ts"):
        return "used"
    if (p.get("valid_to") or "") < now_str:
        return "expired"
    if (p.get("valid_from") or "") > now_str:
        return "upcoming"
    return "active"


@route("GET", "/api/announcements", need="user")
def r_announcements(h, qs, user):
    conn = db()
    rows = conn.execute("SELECT id, ts, title, body, kind, pinned, user FROM announcements "
                        "ORDER BY pinned DESC, ts DESC").fetchall()
    reads = {}
    for aid, uname in conn.execute("SELECT ann_id, username FROM announcement_reads"):
        reads.setdefault(aid, []).append(uname)
    mine = {r[0] for r in conn.execute("SELECT ann_id FROM announcement_reads WHERE username=?", (user["username"],))}
    conn.close()
    is_admin = user["role"] == "admin"
    out = []
    for r in rows:
        d = {"id": r[0], "ts": r[1], "title": r[2], "body": r[3], "kind": r[4], "pinned": bool(r[5]),
             "user": r[6] or "", "read": r[0] in mine, "read_count": len(reads.get(r[0], []))}
        if is_admin:
            d["readers"] = [user_name(u) for u in sorted(reads.get(r[0], []))]
        out.append(d)
    h.json(200, {"announcements": out, "kinds": ANN_KINDS})


@route("POST", "/api/announcements", need="admin")
def r_announcement_save(h, qs, user):
    b = h.body()
    title, body = txt(b, "title", 140), txt(b, "body", 4000)
    if not title:
        raise ValueError("Başlık gerekli")
    if not body:
        raise ValueError("Duyuru metni gerekli")
    kind = b.get("kind") if b.get("kind") in ANN_KINDS else "duyuru"
    pinned = 1 if b.get("pinned") else 0
    try:
        aid = int(b.get("id") or 0)
    except (TypeError, ValueError):
        aid = 0
    conn = db()
    if aid:
        cur = conn.execute("UPDATE announcements SET title=?, body=?, kind=?, pinned=? WHERE id=?",
                           (title, body, kind, pinned, aid))
        if not cur.rowcount:
            conn.close()
            raise ValueError("Duyuru bulunamadı")
    else:
        aid = conn.execute("INSERT INTO announcements (ts, title, body, kind, pinned, user) VALUES (?,?,?,?,?,?)",
                           (time.time(), title, body, kind, pinned, user["name"])).lastrowid
    conn.commit()
    conn.close()
    if not b.get("id"):
        for uname, rec in cfg["users"].items():
            if rec.get("role") == "resident" and rec.get("status", "active") == "active":
                notify(uname, "duyuru", f"Yeni duyuru: {title}", body[:200], "#/duyurular")
    audit(user, "Duyuru", f"{'Düzenlendi' if aid and b.get('id') else 'Yayınlandı'}: {title}")
    h.json(200, {"ok": True, "id": aid})


@route("POST", "/api/announcements/delete", need="admin")
def r_announcement_delete(h, qs, user):
    aid = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT title FROM announcements WHERE id=?", (aid,)).fetchone()
    if not r:
        conn.close()
        return h.json(404, {"error": "Duyuru bulunamadı"})
    conn.execute("DELETE FROM announcements WHERE id=?", (aid,))
    conn.execute("DELETE FROM announcement_reads WHERE ann_id=?", (aid,))
    conn.commit()
    conn.close()
    audit(user, "Duyuru", f"Silindi: {r[0]}")
    h.json(200, {"ok": True})


@route("POST", "/api/announcements/read", need="user")
def r_announcement_read(h, qs, user):
    aid = h.body().get("id")
    conn = db()
    if not conn.execute("SELECT 1 FROM announcements WHERE id=?", (aid,)).fetchone():
        conn.close()
        raise ValueError("Duyuru bulunamadı")
    conn.execute("INSERT OR IGNORE INTO announcement_reads (ann_id, username, ts) VALUES (?,?,?)",
                 (aid, user["username"], time.time()))
    conn.commit()
    conn.close()
    h.json(200, {"ok": True})


@route("GET", "/api/notifications", need="user")
def r_notifications(h, qs, user):
    conn = db()
    rows = conn.execute("SELECT id, ts, kind, title, body, link, read_ts FROM notifications "
                        "WHERE username=? ORDER BY ts DESC LIMIT 100", (user["username"],)).fetchall()
    conn.close()
    h.json(200, {"notifications": [{"id": r[0], "ts": r[1], "kind": r[2], "title": r[3], "body": r[4] or "",
                                     "link": r[5] or "", "read": bool(r[6])} for r in rows],
                 "unread": sum(1 for r in rows if not r[6])})


@route("POST", "/api/notifications/read", need="user")
def r_notifications_read(h, qs, user):
    b = h.body()
    conn = db()
    if b.get("all"):
        conn.execute("UPDATE notifications SET read_ts=? WHERE username=? AND read_ts IS NULL", (time.time(), user["username"]))
    elif b.get("id"):
        conn.execute("UPDATE notifications SET read_ts=? WHERE id=? AND username=?", (time.time(), b["id"], user["username"]))
    conn.commit()
    conn.close()
    h.json(200, {"ok": True})


@route("POST", "/api/notifications/app", need="admin")
def r_notify_app(h, qs, user):
    b = h.body()
    try:
        flat_ids = {int(x) for x in (b.get("flat_ids") or [])}
    except (TypeError, ValueError):
        raise ValueError("Alıcı listesi geçersiz")
    if not flat_ids:
        raise ValueError("En az bir alıcı seçin")
    title, body = txt(b, "title", 140), txt(b, "body", 2000)
    if not title or not body:
        raise ValueError("Başlık ve mesaj gerekli")
    n = 0
    for uname, rec in cfg["users"].items():
        if rec.get("role") == "resident" and rec.get("status", "active") == "active" and rec.get("flat_id") in flat_ids:
            notify(uname, "mesaj", title, body, "#/bildirimler")
            n += 1
    audit(user, "Uygulama bildirimi", f"{n} hesap · {title}")
    h.json(200, {"ok": True, "count": n})


def guest_pass_rows(conn, where, args):
    rows = conn.execute("SELECT g.id, g.flat_id, g.name, g.plate, g.note, g.valid_from, g.valid_to, g.created_by, "
                        "g.ts, g.used_ts, g.used_by, f.blok, f.daire FROM guest_passes g "
                        "LEFT JOIN flats f ON f.id=g.flat_id " + where +
                        " ORDER BY (g.used_ts IS NOT NULL), g.valid_from LIMIT 100", args).fetchall()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = []
    for r in rows:
        p = {"id": r[0], "flat_id": r[1], "name": r[2], "plate": r[3] or "", "note": r[4] or "",
             "valid_from": r[5], "valid_to": r[6], "created_by": r[7] or "", "ts": r[8],
             "used_ts": r[9], "used_by": r[10] or "",
             "flat": flat_label({"blok": r[11], "daire": r[12]}) if r[12] else ""}
        p["status"] = pass_state(p, now_str)
        out.append(p)
    return out


@route("GET", "/api/guest-passes", need="user")
def r_guest_passes(h, qs, user):
    conn = db()
    if user["role"] == "resident":
        if not user.get("flat_id"):
            conn.close()
            return h.json(200, {"passes": []})
        passes = guest_pass_rows(conn, "WHERE g.flat_id=?", (user["flat_id"],))
    else:
        day = dparam(qs, "date", datetime.now().strftime("%Y-%m-%d")).strftime("%Y-%m-%d")
        passes = guest_pass_rows(conn, "WHERE substr(g.valid_from,1,10)<=? AND substr(g.valid_to,1,10)>=?", (day, day))
    conn.close()
    h.json(200, {"passes": passes})


@route("POST", "/api/guest-passes", need="user")
def r_guest_pass_add(h, qs, user):
    b = h.body()
    name = txt(b, "name", 80)
    if not name:
        raise ValueError("Ziyaretçi adı gerekli")
    if user["role"] == "resident":
        flat_id = user.get("flat_id")
        if not flat_id:
            raise ValueError("Hesabınız bir daireye bağlı değil")
    else:
        try:
            flat_id = int(b.get("flat_id"))
        except (TypeError, ValueError):
            raise ValueError("Daire seçin")
    date = opt_date(b, "date") or datetime.now().strftime("%Y-%m-%d")
    frm, to = str(b.get("from") or "09:00"), str(b.get("to") or "23:59")
    for v, lbl in ((frm, "Başlangıç"), (to, "Bitiş")):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError(f"{lbl} saati SS:DD olmalı")
    if to <= frm:
        raise ValueError("Bitiş saati başlangıçtan sonra olmalı")
    plate = re.sub(r"\s+", " ", str(b.get("plate") or "")).strip().upper()[:12]
    note = txt(b, "note", 200)
    conn = db()
    gid = conn.execute("INSERT INTO guest_passes (flat_id, name, plate, note, valid_from, valid_to, created_by, ts) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (flat_id, name, plate, note, f"{date} {frm}", f"{date} {to}", user["name"], time.time())).lastrowid
    conn.commit()
    conn.close()
    audit(user, "Ziyaretçi izni", f"{name} · {date} {frm}-{to}")
    h.json(200, {"ok": True, "id": gid})


@route("POST", "/api/guest-passes/use", need="staff")
def r_guest_pass_use(h, qs, user):
    gid = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT name, used_ts FROM guest_passes WHERE id=?", (gid,)).fetchone()
    if not r:
        conn.close()
        raise ValueError("İzin bulunamadı")
    if r[1]:
        conn.close()
        raise ValueError("Bu izin zaten kullanıldı")
    conn.execute("UPDATE guest_passes SET used_ts=?, used_by=? WHERE id=?", (time.time(), user["name"], gid))
    conn.commit()
    conn.close()
    audit(user, "Ziyaretçi izni", f"Giriş kaydedildi: {r[0]}")
    h.json(200, {"ok": True})


@route("POST", "/api/guest-passes/delete", need="user")
def r_guest_pass_delete(h, qs, user):
    gid = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT name, flat_id FROM guest_passes WHERE id=?", (gid,)).fetchone()
    if not r:
        conn.close()
        return h.json(404, {"error": "İzin bulunamadı"})
    if user["role"] == "resident" and r[1] != user.get("flat_id"):
        conn.close()
        return h.json(403, {"error": "Bu izin size ait değil"})
    conn.execute("DELETE FROM guest_passes WHERE id=?", (gid,))
    conn.commit()
    conn.close()
    audit(user, "Ziyaretçi izni", f"Silindi: {r[0]}")
    h.json(200, {"ok": True})


def package_rows(conn, where, args):
    rows = conn.execute("SELECT p.id, p.flat_id, p.courier, p.note, p.ts_in, p.ts_out, p.user, f.blok, f.daire "
                        "FROM packages p LEFT JOIN flats f ON f.id=p.flat_id " + where +
                        " ORDER BY (p.ts_out IS NOT NULL), p.ts_in DESC LIMIT 100", args).fetchall()
    return [{"id": r[0], "flat_id": r[1], "courier": r[2] or "", "note": r[3] or "", "ts_in": r[4],
             "ts_out": r[5], "user": r[6] or "",
             "flat": flat_label({"blok": r[7], "daire": r[8]}) if r[8] else ""} for r in rows]


@route("GET", "/api/packages", need="user")
def r_packages(h, qs, user):
    conn = db()
    if user["role"] == "resident":
        if not user.get("flat_id"):
            conn.close()
            return h.json(200, {"packages": []})
        rows = package_rows(conn, "WHERE p.flat_id=?", (user["flat_id"],))
    else:
        rows = package_rows(conn, "WHERE p.ts_in >= ?", (time.time() - 86400,))
    conn.close()
    h.json(200, {"packages": rows})


@route("POST", "/api/packages", need="staff")
def r_package_add(h, qs, user):
    b = h.body()
    try:
        flat_id = int(b.get("flat_id"))
    except (TypeError, ValueError):
        raise ValueError("Daire seçin")
    courier = txt(b, "courier", 60)
    note = txt(b, "note", 200)
    if not courier:
        raise ValueError("Kargo / gönderi adı gerekli")
    conn = db()
    ok = conn.execute("SELECT 1 FROM flats WHERE id=?", (flat_id,)).fetchone()
    if not ok:
        conn.close()
        raise ValueError("Daire bulunamadı")
    pid = conn.execute("INSERT INTO packages (flat_id, courier, note, ts_in, user) VALUES (?,?,?,?,?)",
                       (flat_id, courier, note, time.time(), user["name"])).lastrowid
    conn.commit()
    conn.close()
    audit(user, "Kargo", f"{courier} · daire {flat_id}")
    h.json(200, {"ok": True, "id": pid})


@route("POST", "/api/packages/out", need="staff")
def r_package_out(h, qs, user):
    pid = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT courier FROM packages WHERE id=? AND ts_out IS NULL", (pid,)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Kargo kaydı bulunamadı veya teslim edilmiş")
    conn.execute("UPDATE packages SET ts_out=? WHERE id=?", (time.time(), pid))
    conn.commit()
    conn.close()
    audit(user, "Kargo", f"Teslim: {r[0]}")
    h.json(200, {"ok": True})


# ---------------------------------------------------------------- residents registry (KVKK: personal data)

KINDS = {"malik": "Malik", "kiraci": "Kiracı", "hane": "Hane üyesi"}
TR_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iIsSgGuUoOcC")


def fold(s):
    return re.sub(r"[^a-z0-9]", "", str(s).translate(TR_FOLD).lower())


def fold_words(s):
    """Lowercase, Turkish letters folded, punctuation to spaces (keeps word boundaries)."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]", " ", str(s).translate(TR_FOLD).lower())).strip()


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
    q = "SELECT id, blok, kapi, daire, arsa_payi, notes, kat FROM flats"
    rows = conn.execute(q + (" WHERE id=?" if flat_id else ""), (flat_id,) if flat_id else ()).fetchall()
    flats = {r[0]: {"id": r[0], "blok": r[1], "kapi": r[2], "daire": r[3], "arsa_payi": r[4], "notes": r[5] or "",
                    "kat": r[6] or "", "people": [], "vehicles": []} for r in rows}
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
    vals = (blok, txt(b, "kapi", 10), daire, ap, txt(b, "notes", 500), txt(b, "kat", 10))
    if fid:
        conn.execute("UPDATE flats SET blok=?, kapi=?, daire=?, arsa_payi=?, notes=?, kat=? WHERE id=?", (*vals, fid))
    else:
        fid = conn.execute("INSERT INTO flats (blok, kapi, daire, arsa_payi, notes, kat) VALUES (?,?,?,?,?,?)", vals).lastrowid
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
    reqs = conn.execute("SELECT id, ts, text, status, reply FROM requests WHERE flat_id=? ORDER BY ts DESC LIMIT 30",
                        (user["flat_id"],)).fetchall()
    msgs = {}
    if reqs:
        ids = [r[0] for r in reqs]
        for m in conn.execute("SELECT request_id, ts, user, role, text FROM request_messages WHERE request_id IN ("
                              + ",".join("?" * len(ids)) + ") ORDER BY ts, id", ids):
            msgs.setdefault(m[0], []).append({"ts": m[1], "user": m[2], "role": m[3], "text": m[4]})
    conn.close()
    f = flats[0] if flats else None
    if f:
        for p in f["people"]:
            p.pop("notes", None)
        f.pop("notes", None)
    conn = db()
    dues = {"state": dues_state(conn, [user["flat_id"]])[user["flat_id"]], "ledger": ledger(conn, user["flat_id"])} if f else None
    if dues:
        rc = receipt_map(conn)
        for row in dues["ledger"]:
            if row.get("type") == "payment":
                v = rc.get(row["id"])
                row["receipt"] = receipt_serial(*v) if v else ""
    conn.close()
    ds = get_setting("dues", DUES_DEFAULT)
    if dues:
        dues.update(iban=ds["iban"], account_name=ds["account_name"], late_pct=ds["late_pct"], due_day=ds["due_day"],
                    pay_url=ds.get("pay_url", ""))
    h.json(200, {"flat": f, "kinds": KINDS, "dues": dues, "methods": PAY_METHODS,
                 "requests": [{"id": r[0], "ts": r[1], "text": r[2], "status": r[3], "reply": r[4],
                               "messages": msgs.get(r[0], [])} for r in reqs]})


REQ_CATEGORIES = {"bakim": "Bakım / onarım", "ariza": "Arıza", "temizlik": "Temizlik",
                  "gurultu": "Gürültü", "arac": "Araç / otopark", "diger": "Diğer"}


def request_dir():
    return os.path.join(DATA, "talepler")


def decode_photo(b):
    """Resident request photo: base64 data URL, JPG/PNG/WEBP, max 5 MB."""
    import base64
    raw = str(b.get("photo") or "")
    if not raw:
        return None
    m = re.match(r"^data:image/(jpeg|png|webp);base64,(.+)$", raw, re.S)
    if not m:
        raise ValueError("Fotoğraf JPG, PNG veya WEBP olmalı")
    try:
        data = base64.b64decode(m.group(2), validate=False)
    except (ValueError, TypeError):
        raise ValueError("Fotoğraf okunamadı")
    if len(data) > 5_000_000:
        raise ValueError("Fotoğraf en fazla 5 MB olabilir")
    magic = {"jpeg": b"\xff\xd8", "png": b"\x89PNG", "webp": b"RIFF"}[m.group(1)]
    if not data.startswith(magic):
        raise ValueError("Dosya bir fotoğraf değil")
    return data, {"jpeg": "jpg", "png": "png", "webp": "webp"}[m.group(1)]


@route("POST", "/api/my/request", need="user")
def r_my_request(h, qs, user):
    if user["role"] != "resident" or not user.get("flat_id"):
        return h.json(400, {"error": "Bu işlem site sakinleri içindir"})
    b = h.body(max_bytes=8_000_000)
    t = txt(b, "text", 2000)
    if not t:
        raise ValueError("Talebinizi yazın")
    cat = b.get("category") if b.get("category") in REQ_CATEGORIES else "diger"
    photo = decode_photo(b)
    conn = db()
    rid = conn.execute("INSERT INTO requests (ts, flat_id, user, text, status, category) VALUES (?,?,?,?, 'acik', ?)",
                       (time.time(), user["flat_id"], user["name"], t, cat)).lastrowid
    conn.execute("INSERT INTO request_messages (request_id, ts, user, role, text) VALUES (?,?,?, 'resident', ?)",
                 (rid, time.time(), user["name"], t))
    if photo:
        data, ext = photo
        os.makedirs(request_dir(), exist_ok=True)
        name = f"{rid}-{secrets.token_hex(4)}.{ext}"
        with open(os.path.join(request_dir(), name), "wb") as f:
            f.write(data)
        conn.execute("UPDATE requests SET photo=? WHERE id=?", (name, rid))
    conn.commit()
    conn.close()
    audit(user, "Talep", f"{REQ_CATEGORIES[cat]} · {t[:60]}")
    h.json(200, {"ok": True, "id": rid})


@route("GET", "/api/requests/photo", need="user")
def r_request_photo(h, qs, user):
    conn = db()
    r = conn.execute("SELECT flat_id, photo FROM requests WHERE id=?", (qs.get("id"),)).fetchone()
    conn.close()
    if not r or not r[1]:
        raise ValueError("Fotoğraf yok")
    if user["role"] == "staff":
        return h.json(403, {"error": "Bu fotoğraf güvenlik rolüne kapalı"})
    if user["role"] == "resident" and r[0] != user.get("flat_id"):
        return h.json(403, {"error": "Bu talep size ait değil"})
    p = os.path.join(request_dir(), os.path.basename(r[1]))
    if not os.path.isfile(p):
        raise ValueError("Fotoğraf dosyası yok")
    ext = os.path.splitext(p)[1].lstrip(".").lower()
    ctype = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "application/octet-stream")
    with open(p, "rb") as f:
        h.raw(200, f.read(), ctype)


@route("GET", "/api/requests", need="admin")
def r_requests(h, qs, user):
    conn = db()
    rows = conn.execute("SELECT r.id, r.ts, r.user, r.text, r.status, r.reply, r.closed_by, r.closed_at, f.blok, f.daire, "
                        "r.category, r.photo, r.flat_id FROM requests r LEFT JOIN flats f ON f.id=r.flat_id "
                        "ORDER BY r.status='kapali', r.ts DESC LIMIT 300").fetchall()
    msgs = {}
    if rows:
        ids = [r[0] for r in rows]
        for m in conn.execute("SELECT request_id, ts, user, role, text FROM request_messages WHERE request_id IN ("
                              + ",".join("?" * len(ids)) + ") ORDER BY ts, id", ids):
            msgs.setdefault(m[0], []).append({"ts": m[1], "user": m[2], "role": m[3], "text": m[4]})
    conn.close()
    h.json(200, {"requests": [{"id": r[0], "ts": r[1], "user": r[2], "text": r[3], "status": r[4], "reply": r[5],
                               "closed_by": r[6], "closed_at": r[7],
                               "flat": flat_label({"blok": r[8], "daire": r[9]}) if r[9] else "",
                               "category": r[10] or "diger", "has_photo": bool(r[11]), "flat_id": r[12],
                               "messages": msgs.get(r[0], [])} for r in rows],
                 "categories": REQ_CATEGORIES})


@route("POST", "/api/requests/close", need="admin")
def r_request_close(h, qs, user):
    b = h.body()
    reply = txt(b, "reply", 2000)
    conn = db()
    conn.execute("UPDATE requests SET status='kapali', reply=?, closed_by=?, closed_at=? WHERE id=?",
                 (reply, user["name"], time.time(), b.get("id")))
    if reply:
        conn.execute("INSERT INTO request_messages (request_id, ts, user, role, text) VALUES (?,?,?, 'admin', ?)",
                     (b.get("id"), time.time(), user["name"], reply))
    conn.commit()
    conn.close()
    audit(user, "Talep kapatıldı", f"#{b.get('id')}")
    h.json(200, {"ok": True})


@route("POST", "/api/requests/reply", need="admin")
def r_request_reply(h, qs, user):
    b = h.body()
    rid, text = b.get("id"), txt(b, "text", 2000)
    if not text:
        raise ValueError("Mesaj yazın")
    conn = db()
    r = conn.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Talep bulunamadı")
    conn.execute("INSERT INTO request_messages (request_id, ts, user, role, text) VALUES (?,?,?, 'admin', ?)",
                 (rid, time.time(), user["name"], text))
    status = b.get("status") if b.get("status") in ("acik", "islemde", "kapali") else ("islemde" if r[0] == "acik" else r[0])
    conn.execute("UPDATE requests SET status=?, reply=?, closed_by=?, closed_at=? WHERE id=?",
                 (status, text, user["name"] if status == "kapali" else None,
                  time.time() if status == "kapali" else None, rid))
    conn.commit()
    conn.close()
    audit(user, "Talep yanıtı", f"#{rid} · {status}")
    h.json(200, {"ok": True, "status": status})


@route("POST", "/api/requests/status", need="admin")
def r_request_status(h, qs, user):
    b = h.body()
    status = b.get("status")
    if status not in ("acik", "islemde", "kapali"):
        raise ValueError("Durum geçersiz")
    conn = db()
    r = conn.execute("SELECT status FROM requests WHERE id=?", (b.get("id"),)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Talep bulunamadı")
    conn.execute("UPDATE requests SET status=?, closed_by=?, closed_at=? WHERE id=?",
                 (status, user["name"] if status == "kapali" else None,
                  time.time() if status == "kapali" else None, b.get("id")))
    conn.commit()
    conn.close()
    audit(user, "Talep durumu", f"#{b.get('id')} · {status}")
    h.json(200, {"ok": True})


@route("POST", "/api/my/vehicle", need="user")
def r_my_vehicle(h, qs, user):
    if user["role"] != "resident" or not user.get("flat_id"):
        return h.json(400, {"error": "Bu işlem site sakinleri içindir"})
    b = h.body()
    plate = re.sub(r"\s+", " ", txt(b, "plate", 15).translate(TR_FOLD).upper())
    if len(plate_key(plate)) < 4:
        raise ValueError("Plaka geçersiz")
    conn = db()
    dup = conn.execute("SELECT flat_id FROM vehicles WHERE plate_key=?", (plate_key(plate),)).fetchone()
    if dup and dup[0] != user["flat_id"]:
        conn.close()
        raise ValueError("Bu plaka başka bir dairede kayıtlı")
    vid = b.get("id")
    if vid:
        if not conn.execute("SELECT 1 FROM vehicles WHERE id=? AND flat_id=?", (vid, user["flat_id"])).fetchone():
            conn.close()
            raise ValueError("Araç bulunamadı")
        conn.execute("UPDATE vehicles SET plate=?, plate_key=?, model=?, color=?, parking=? WHERE id=?",
                     (plate, plate_key(plate), txt(b, "model", 60), txt(b, "color", 30), txt(b, "parking", 20), vid))
        action = "Araç güncellendi"
    else:
        n = conn.execute("SELECT COUNT(*) FROM vehicles WHERE flat_id=?", (user["flat_id"],)).fetchone()[0]
        if n >= 2:
            conn.close()
            raise ValueError("En fazla 2 araç ekleyebilirsiniz")
        conn.execute("INSERT INTO vehicles (flat_id, plate, plate_key, model, color, parking, sticker) VALUES (?,?,?,?,?,?,'')",
                     (user["flat_id"], plate, plate_key(plate), txt(b, "model", 60), txt(b, "color", 30), txt(b, "parking", 20)))
        action = "Araç eklendi"
    conn.commit()
    conn.close()
    audit(user, action, plate)
    h.json(200, {"ok": True})


@route("POST", "/api/my/vehicle/delete", need="user")
def r_my_vehicle_delete(h, qs, user):
    if user["role"] != "resident" or not user.get("flat_id"):
        return h.json(400, {"error": "Bu işlem site sakinleri içindir"})
    vid = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT plate FROM vehicles WHERE id=? AND flat_id=?", (vid, user["flat_id"])).fetchone()
    if not r:
        conn.close()
        raise ValueError("Araç bulunamadı")
    conn.execute("DELETE FROM vehicles WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    audit(user, "Araç silindi", r[0])
    h.json(200, {"ok": True})


@route("POST", "/api/my/request/reply", need="user")
def r_my_request_reply(h, qs, user):
    if user["role"] != "resident" or not user.get("flat_id"):
        return h.json(400, {"error": "Bu işlem site sakinleri içindir"})
    b = h.body()
    rid, text = b.get("id"), txt(b, "text", 2000)
    if not text:
        raise ValueError("Mesaj yazın")
    conn = db()
    r = conn.execute("SELECT status FROM requests WHERE id=? AND flat_id=?", (rid, user["flat_id"])).fetchone()
    if not r:
        conn.close()
        raise ValueError("Talep bulunamadı")
    if r[0] == "kapali":
        conn.close()
        raise ValueError("Bu talep kapatılmış; yeni bir talep açın")
    conn.execute("INSERT INTO request_messages (request_id, ts, user, role, text) VALUES (?,?,?, 'resident', ?)",
                 (rid, time.time(), user["name"], text))
    conn.execute("UPDATE requests SET status=COALESCE(NULLIF(status,''),'acik') WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    audit(user, "Talep mesajı", f"#{rid}")
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
    req_text = "Aracımın plakası değişti: yeni plaka 07 YEN 101."
    rid = conn.execute("INSERT INTO requests (ts, flat_id, user, text, status) VALUES (?,?,?,?, 'acik')",
                       (now - 86400, 1, "Demo site sakini", req_text)).lastrowid
    conn.execute("INSERT INTO request_messages (request_id, ts, user, role, text) VALUES (?,?,?, 'resident', ?)",
                 (rid, now - 86400, "Demo site sakini", req_text))
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


def seed_demo_management():
    """Fictional contracts, tasks and one document, demo mode only (data-demo/, recreated at start)."""
    today = datetime.now().date()
    day = lambda n: (today + timedelta(days=n)).isoformat()
    now = time.time()
    bdir = doc_dir()
    os.makedirs(bdir, exist_ok=True)
    text = ("ADALYA SİTESİ (DEMO) - Asansör bakım sözleşmesi özeti\n\n"
            "Bu dosya demo modunda üretilmiştir; gerçek bir sözleşme değildir.\n"
            "Kapsam: aylık bakım, yıllık A tipi muayene, acil servis.\n")
    conn = db()
    did = conn.execute("INSERT INTO documents (name, kind, stored, size, note, user, ts) VALUES (?,?,?,?,?,?,?)",
                       ("Asansör bakım sözleşmesi (demo)", "sozlesme", "", 0, "Demo belgesi", "Demo", now)).lastrowid
    stored = f"{did}-demo.txt"
    with open(os.path.join(bdir, stored), "w", encoding="utf-8") as f:
        f.write(text)
    conn.execute("UPDATE documents SET stored=?, size=? WHERE id=?", (stored, len(text.encode("utf-8")), did))
    rows = [
        ("Asansör bakım sözleşmesi", "asansor", "Demo Asansör A.Ş.", day(-200), day(160), 18000, "Aylık bakım + yıllık A tipi muayene", did),
        ("Site sigorta poliçesi", "sigorta", "Demo Sigorta", day(-345), day(20), 42500, "Yangın, deprem ve sorumluluk", None),
        ("Temizlik hizmeti sözleşmesi", "temizlik", "Demo Temizlik Ltd.", day(-371), day(-6), 96000, "Ortak alanlar, haftada 6 gün", None),
    ]
    for r in rows:
        conn.execute("INSERT INTO contracts (name, kind, provider, start_date, end_date, amount, note, doc_id, user, ts) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (*r, "Demo", now))
    for date, title, kind, note, done in [
        (day(5), "Asansör yıllık A tipi muayene randevusu", "bakim", "Yetkili firmadan teyit bekleniyor.", 0),
        (day(-3), "Kombi bakım teklifi karşılaştır", "gorev", "", 0),
        (day(12), "Genel kurul gündemini hazırla", "toplanti", "", 0),
        (day(-40), "Karar defteri noter kapanışı", "hukuki", "Tamamlandı.", 1),
    ]:
        conn.execute("INSERT INTO calendar_tasks (date, title, kind, note, done, user, ts) VALUES (?,?,?,?,?,?,?)",
                     (date, title, kind, note, done, "Demo", now))
    for key in ("defter", "isletme"):
        conn.execute("INSERT OR REPLACE INTO duty_done (duty_key, year, ts, user) VALUES (?,?,?,?)",
                     (key, today.year, now, "Demo"))
    conn.commit()
    conn.close()


def seed_demo_comms():
    """Fictional announcements, guest passes and packages for demo mode."""
    today = datetime.now().date()
    iso = lambda n: (today + timedelta(days=n)).isoformat()
    now = time.time()
    conn = db()
    anns = [
        (now - 2 * 86400, "Su deposu temizliği",
         "22 Kasım Cumartesi 09:00–12:00 arasında su kesintisi olacak. Depolar temizlendikten sonra suyu birkaç dakika akıtmanız önerilir.",
         "ariza", 1),
        (now - 5 * 86400, "Olağan genel kurul",
         "31 Ocak 2027 Cumartesi 19:00 · site yemekhanesi. Gündem: 2026 hesap raporu, 2027 işletme projesi, asansör bakım sözleşmesi.",
         "toplanti", 0),
        (now - 9 * 86400, "Otopark kuralları",
         "Misafir araçlar için girişte plaka bildirimi yapılması rica olunur. Yangın yolunu kapatmayınız.",
         "duyuru", 0),
    ]
    ids = []
    for ts, title, body, kind, pinned in anns:
        ids.append(conn.execute("INSERT INTO announcements (ts, title, body, kind, pinned, user) "
                                "VALUES (?,?,?,?,?,'Demo yönetim')", (ts, title, body, kind, pinned)).lastrowid)
    conn.execute("INSERT OR IGNORE INTO announcement_reads (ann_id, username, ts) VALUES (?,?,?)", (ids[0], "demo", now))
    for flat_id, name, plate, note, frm, to in [
        (1, "Mehmet Yılmaz", "07 ABC 123", "Aile ziyareti", "18:00", "23:00"),
        (2, "Kombi servisi", "34 SRV 12", "B Blok 3", "09:00", "17:00"),
    ]:
        conn.execute("INSERT INTO guest_passes (flat_id, name, plate, note, valid_from, valid_to, created_by, ts) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (flat_id, name, plate, note, f"{iso(0)} {frm}", f"{iso(0)} {to}", "Demo sakin", now))
    for flat_id, courier, note, back in [(1, "Kargo (demo)", "2 paket", 3600), (3, "Market", "1 poşet", 7200)]:
        conn.execute("INSERT INTO packages (flat_id, courier, note, ts_in, user) VALUES (?,?,?,?,?)",
                     (flat_id, courier, note, now - back, "Demo güvenlik"))
    year = today.year
    bid = conn.execute("INSERT INTO budgets (year, status, note, created_by, ts) VALUES (?,'taslak',?,'Demo yönetim',?)",
                       (year, f"{year} yılı işletme projesi", now)).lastrowid
    for kind, label, amount in [("gelir", "Ortak alan kira geliri", 24000), ("gider", "Asansör bakım sözleşmesi", 84000),
                                ("gider", "Temizlik hizmeti", 192000), ("gider", "Elektrik ve su", 96000),
                                ("gider", "Sigorta", 42500)]:
        conn.execute("INSERT INTO budget_lines (budget_id, kind, label, amount, note) VALUES (?,?,?,?,'')",
                     (bid, kind, label, amount))
    for back, kind, cat, label, amount in [(13, "gider", "elektrik", "Elektrik faturası", 8200),
                                           (9, "gider", "temizlik", "Temizlik hizmeti", 16000),
                                           (4, "gelir", "kira", "Ortak alan kira geliri", 2000)]:
        conn.execute("INSERT INTO cash (date, kind, category, label, amount, note, user, ts) VALUES (?,?,?,?,?,'','Demo yönetim',?)",
                     ((today + timedelta(days=-back)).isoformat(), kind, cat, label, amount, now))
    for kind, title, body, link in [
        ("duyuru", "Yeni duyuru: Su deposu temizliği", "22 Kasım 09:00–12:00 arası su kesintisi olacak.", "#/duyurular"),
        ("mesaj", "Aidat hatırlatma", "Kasım aidatı için son ödeme 10 Kasım.", "#/bildirimler"),
    ]:
        conn.execute("INSERT INTO notifications (ts, username, kind, title, body, link) VALUES (?,?,?,?,?,?)",
                     (now - 3600, "demo", kind, title, body, link))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- işletme projesi (KMK 37) and income/expense
# KMK 37: the yearly estimate is notified to owners; it becomes final unless objected within 7 days.
# The panel is the working copy; the notarised book and the yönetim planı stay official.

BUDGET_STATUS = {"taslak": "Taslak", "bildirildi": "Bildirildi", "kesin": "Kesinleşti"}
CASH_CATS = {"kira": "Kira geliri", "bagis": "Bağış", "faiz": "Faiz", "aidat": "Aidat",
             "temizlik": "Temizlik", "asansor": "Asansör", "elektrik": "Elektrik", "su": "Su",
             "personel": "Personel", "bakim": "Bakım / onarım", "vergi": "Vergi", "diger": "Diğer"}


def budget_payload(conn, year):
    b = conn.execute("SELECT id, year, status, note, notified_at, objection_until, created_by, ts FROM budgets WHERE year=?",
                     (year,)).fetchone()
    flats = [{"id": r[0], "label": flat_label({"blok": r[1], "daire": r[2]}), "arsa": r[3] or 0}
             for r in conn.execute("SELECT id, blok, daire, arsa_payi FROM flats ORDER BY blok, CAST(daire AS INTEGER)")]
    if not b:
        return {"year": year, "budget": None, "flats": flats, "statuses": BUDGET_STATUS}
    lines = [{"id": r[0], "kind": r[1], "label": r[2], "amount": r[3], "note": r[4] or ""}
             for r in conn.execute("SELECT id, kind, label, amount, note FROM budget_lines WHERE budget_id=? ORDER BY kind DESC, id",
                                   (b[0],))]
    gelir = round(sum(l["amount"] for l in lines if l["kind"] == "gelir"), 2)
    gider = round(sum(l["amount"] for l in lines if l["kind"] == "gider"), 2)
    net = round(gider - gelir, 2)
    tot = sum(f["arsa"] for f in flats)
    for f in flats:
        f["share"] = round(net * f["arsa"] / tot, 2) if tot else (round(net / len(flats), 2) if flats else 0)
    return {"year": year, "flats": flats, "statuses": BUDGET_STATUS,
            "budget": {"id": b[0], "status": b[2], "note": b[3] or "", "notified_at": b[4],
                       "objection_until": b[5] or "", "created_by": b[6] or "", "ts": b[7],
                       "lines": lines, "gelir": gelir, "gider": gider, "net": net}}


@route("GET", "/api/budget", need="admin")
def r_budget(h, qs, user):
    try:
        year = int(qs.get("year") or datetime.now().year)
    except (TypeError, ValueError):
        raise ValueError("Yıl geçersiz")
    if not 2000 <= year <= 2100:
        raise ValueError("Yıl geçersiz")
    conn = db()
    data = budget_payload(conn, year)
    conn.close()
    h.json(200, data)


@route("POST", "/api/budget", need="admin")
def r_budget_create(h, qs, user):
    b = h.body()
    try:
        year = int(b.get("year"))
    except (TypeError, ValueError):
        raise ValueError("Yıl gerekli")
    if not 2000 <= year <= 2100:
        raise ValueError("Yıl geçersiz")
    conn = db()
    if conn.execute("SELECT 1 FROM budgets WHERE year=?", (year,)).fetchone():
        conn.close()
        raise ValueError(f"{year} için işletme projesi zaten var")
    bid = conn.execute("INSERT INTO budgets (year, status, note, created_by, ts) VALUES (?,'taslak',?,?,?)",
                       (year, txt(b, "note", 400), user["name"], time.time())).lastrowid
    conn.commit()
    conn.close()
    audit(user, "İşletme projesi", f"{year} oluşturuldu")
    h.json(200, {"ok": True, "id": bid})


@route("POST", "/api/budget/line", need="admin")
def r_budget_line(h, qs, user):
    b = h.body()
    try:
        bid = int(b.get("budget_id"))
    except (TypeError, ValueError):
        raise ValueError("Proje seçin")
    kind = b.get("kind") if b.get("kind") in ("gelir", "gider") else None
    if not kind:
        raise ValueError("Tür gelir veya gider olmalı")
    label = txt(b, "label", 120)
    if not label:
        raise ValueError("Açıklama gerekli")
    amount = money(b.get("amount"), "Tutar")
    if amount <= 0:
        raise ValueError("Tutar 0'dan büyük olmalı")
    conn = db()
    st = conn.execute("SELECT status FROM budgets WHERE id=?", (bid,)).fetchone()
    if not st:
        conn.close()
        raise ValueError("Proje bulunamadı")
    if st[0] != "taslak":
        conn.close()
        raise ValueError("Bildirilmiş proje değiştirilemez")
    lid = conn.execute("INSERT INTO budget_lines (budget_id, kind, label, amount, note) VALUES (?,?,?,?,?)",
                       (bid, kind, label, amount, txt(b, "note", 200))).lastrowid
    conn.commit()
    conn.close()
    audit(user, "İşletme projesi", f"{kind}: {label} · {amount} TL")
    h.json(200, {"ok": True, "id": lid})


@route("POST", "/api/budget/line/delete", need="admin")
def r_budget_line_delete(h, qs, user):
    lid = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT budget_id, label FROM budget_lines WHERE id=?", (lid,)).fetchone()
    if not r:
        conn.close()
        return h.json(404, {"error": "Satır bulunamadı"})
    st = conn.execute("SELECT status FROM budgets WHERE id=?", (r[0],)).fetchone()
    if st and st[0] != "taslak":
        conn.close()
        raise ValueError("Bildirilmiş proje değiştirilemez")
    conn.execute("DELETE FROM budget_lines WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    audit(user, "İşletme projesi", f"Satır silindi: {r[1]}")
    h.json(200, {"ok": True})


@route("POST", "/api/budget/status", need="admin")
def r_budget_status(h, qs, user):
    b = h.body()
    try:
        bid = int(b.get("id"))
    except (TypeError, ValueError):
        raise ValueError("Proje seçin")
    action = b.get("action")
    conn = db()
    r = conn.execute("SELECT year, status, objection_until FROM budgets WHERE id=?", (bid,)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Proje bulunamadı")
    year, st, until = r
    today = datetime.now().strftime("%Y-%m-%d")
    if action == "notify":
        if st != "taslak":
            conn.close()
            raise ValueError("Bu proje zaten bildirilmiş")
        until = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        conn.execute("UPDATE budgets SET status='bildirildi', notified_at=?, objection_until=? WHERE id=?",
                     (time.time(), until, bid))
        msg = f"{year} bildirildi · itiraz sonu {until}"
    elif action == "final":
        if st == "kesin":
            conn.close()
            raise ValueError("Proje zaten kesinleşti")
        if st == "taslak":
            conn.close()
            raise ValueError("Önce projeyi bildirin")
        if until and until >= today and not b.get("force"):
            conn.close()
            raise ValueError(f"7 günlük itiraz süresi {until} tarihinde bitiyor. Erken kesinleştirmek için onaylayın.")
        conn.execute("UPDATE budgets SET status='kesin' WHERE id=?", (bid,))
        msg = f"{year} kesinleşti"
    else:
        conn.close()
        raise ValueError("İşlem geçersiz")
    conn.commit()
    conn.close()
    audit(user, "İşletme projesi", msg)
    h.json(200, {"ok": True})


@route("GET", "/api/cash", need="admin")
def r_cash(h, qs, user):
    try:
        year = int(qs.get("year") or datetime.now().year)
    except (TypeError, ValueError):
        raise ValueError("Yıl geçersiz")
    conn = db()
    rows = conn.execute("SELECT id, date, kind, category, label, amount, note, user, ts FROM cash "
                        "WHERE substr(date,1,4)=? ORDER BY date DESC, id DESC", (str(year),)).fetchall()
    conn.close()
    entries = [{"id": r[0], "date": r[1], "kind": r[2], "category": r[3], "label": r[4] or "",
                "amount": r[5], "note": r[6] or "", "user": r[7] or "", "ts": r[8]} for r in rows]
    gelir = round(sum(e["amount"] for e in entries if e["kind"] == "gelir"), 2)
    gider = round(sum(e["amount"] for e in entries if e["kind"] == "gider"), 2)
    h.json(200, {"year": year, "entries": entries, "gelir": gelir, "gider": gider,
                 "net": round(gelir - gider, 2), "categories": CASH_CATS})


@route("POST", "/api/cash", need="admin")
def r_cash_add(h, qs, user):
    b = h.body()
    kind = b.get("kind") if b.get("kind") in ("gelir", "gider") else None
    if not kind:
        raise ValueError("Tür gelir veya gider olmalı")
    label = txt(b, "label", 120)
    if not label:
        raise ValueError("Açıklama gerekli")
    amount = money(b.get("amount"), "Tutar")
    if amount <= 0:
        raise ValueError("Tutar 0'dan büyük olmalı")
    date = opt_date(b, "date") or datetime.now().strftime("%Y-%m-%d")
    cat = b.get("category") if b.get("category") in CASH_CATS else "diger"
    conn = db()
    cid = conn.execute("INSERT INTO cash (date, kind, category, label, amount, note, user, ts) VALUES (?,?,?,?,?,?,?,?)",
                       (date, kind, cat, label, amount, txt(b, "note", 200), user["name"], time.time())).lastrowid
    conn.commit()
    conn.close()
    audit(user, "Gelir-gider", f"{kind}: {label} · {amount} TL")
    h.json(200, {"ok": True, "id": cid})


@route("POST", "/api/cash/delete", need="admin")
def r_cash_delete(h, qs, user):
    cid = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT label, amount FROM cash WHERE id=?", (cid,)).fetchone()
    if not r:
        conn.close()
        return h.json(404, {"error": "Kayıt bulunamadı"})
    conn.execute("DELETE FROM cash WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    audit(user, "Gelir-gider", f"Silindi: {r[0]} · {r[1]} TL")
    h.json(200, {"ok": True})


@route("GET", "/api/cash.csv", need="admin")
def r_cash_csv(h, qs, user):
    try:
        year = int(qs.get("year") or datetime.now().year)
    except (TypeError, ValueError):
        raise ValueError("Yıl geçersiz")
    conn = db()
    rows = conn.execute("SELECT date, kind, category, label, amount, note, user FROM cash WHERE substr(date,1,4)=? "
                        "ORDER BY date", (str(year),)).fetchall()
    conn.close()
    data = csv_bytes(["Tarih", "Tür", "Kategori", "Açıklama", "Tutar", "Not", "Kaydeden"],
                     [[r[0], CASH_CATS.get(r[2], r[2]), ("Gelir" if r[1] == "gelir" else "Gider"), r[3], r[4], r[5], r[6]]
                      for r in rows])
    audit(user, "Dışa aktarım", f"Gelir-gider CSV {year} ({len(rows)} satır)")
    h.download(data, f"adalya-gelir-gider-{year}.csv", "text/csv; charset=utf-8")


# ---------------------------------------------------------------- reminder queue (aidat)
REMINDER_DEFAULT = {"enabled": False, "day": 10}


def fmt_money(v):
    return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def prepare_reminders(conn, ids, actor):
    st = dues_state(conn, ids)
    ds = get_setting("dues", DUES_DEFAULT)
    n = 0
    for fid in ids:
        s = st.get(fid) or {}
        if s.get("overdue", 0) <= 0:
            continue
        if conn.execute("SELECT 1 FROM reminders WHERE flat_id=? AND status='bekliyor' AND ts > ?",
                        (fid, time.time() - 7 * 86400)).fetchone():
            continue
        row = conn.execute(
            "SELECT f.blok, f.daire, p.name, p.phone FROM flats f "
            "LEFT JOIN people p ON p.flat_id=f.id AND COALESCE(p.move_out,'')='' "
            "WHERE f.id=? ORDER BY CASE p.kind WHEN 'kiraci' THEN 0 WHEN 'malik' THEN 1 ELSE 2 END LIMIT 1", (fid,)).fetchone()
        flat = flat_label({"blok": row[0], "daire": row[1]}) if row else f"Daire {fid}"
        name = (row[2] if row and row[2] else "Sakin")
        phone = (row[3] if row and row[3] else "")
        oldest = s.get("oldest_due") or ""
        msg = (f"Sayın {name}, {flat} için {fmt_money(s['overdue'])} TL gecikmiş aidat bulunuyor. "
               f"En eski vade: {oldest}. Ödeme: {ds.get('account_name') or site['site_name'] + ' Yönetimi'}"
               f" · IBAN {ds.get('iban') or '—'}. — {site['site_name']}")
        conn.execute("INSERT INTO reminders (ts, flat_id, channel, subject, message, status, user) VALUES (?,?,?,?,?,'bekliyor',?)",
                     (time.time(), fid, "whatsapp", "Aidat hatırlatma", msg, actor["name"]))
        n += 1
    return n


@route("GET", "/api/reminders", need="admin")
def r_reminders(h, qs, user):
    conn = db()
    rows = conn.execute("SELECT r.id, r.ts, r.flat_id, r.channel, r.subject, r.message, r.status, r.sent_ts, r.sent_by, "
                        "f.blok, f.daire FROM reminders r LEFT JOIN flats f ON f.id=r.flat_id "
                        "ORDER BY (r.status='gonderildi'), r.ts DESC LIMIT 200").fetchall()
    phones = {}
    for r in rows:
        if r[2] in phones:
            continue
        t = conn.execute("SELECT phone FROM people WHERE flat_id=? AND COALESCE(move_out,'')='' "
                         "ORDER BY CASE kind WHEN 'kiraci' THEN 0 WHEN 'malik' THEN 1 ELSE 2 END LIMIT 1", (r[2],)).fetchone()
        phones[r[2]] = (t[0] if t and t[0] else "")
    conn.close()
    out = [{"id": r[0], "ts": r[1], "flat_id": r[2], "channel": r[3], "subject": r[4], "message": r[5],
            "status": r[6], "sent_ts": r[7], "sent_by": r[8] or "",
            "flat": flat_label({"blok": r[9], "daire": r[10]}) if r[10] else "", "phone": phones.get(r[2], "")}
           for r in rows]
    h.json(200, {"reminders": out, "settings": get_setting("reminder", REMINDER_DEFAULT),
                 "pending": sum(1 for x in out if x["status"] == "bekliyor")})


@route("POST", "/api/reminders/prepare", need="admin")
def r_rem_prepare(h, qs, user):
    b = h.body()
    conn = db()
    ids = b.get("flat_ids")
    if not isinstance(ids, list) or not ids:
        ids = [r[0] for r in conn.execute("SELECT id FROM flats")]
    else:
        ids = [int(i) for i in ids[:1000]]
    n = prepare_reminders(conn, ids, user)
    conn.commit()
    conn.close()
    audit(user, "Hatırlatma", f"{n} hatırlatma kuyruğa alındı")
    h.json(200, {"ok": True, "count": n})


@route("POST", "/api/reminders/sent", need="admin")
def r_rem_sent(h, qs, user):
    b = h.body()
    rid = b.get("id")
    conn = db()
    r = conn.execute("SELECT flat_id FROM reminders WHERE id=?", (rid,)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Hatırlatma bulunamadı")
    conn.execute("UPDATE reminders SET status='gonderildi', sent_ts=?, sent_by=? WHERE id=?",
                 (time.time(), user["name"], rid))
    conn.commit()
    conn.close()
    audit(user, "Hatırlatma", f"Gönderildi olarak işaretlendi (#{rid})")
    h.json(200, {"ok": True})


@route("POST", "/api/reminders/cancel", need="admin")
def r_rem_cancel(h, qs, user):
    rid = h.body().get("id")
    conn = db()
    r = conn.execute("SELECT 1 FROM reminders WHERE id=?", (rid,)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Hatırlatma bulunamadı")
    conn.execute("UPDATE reminders SET status='iptal' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    h.json(200, {"ok": True})


@route("POST", "/api/reminders/settings", need="admin")
def r_rem_settings(h, qs, user):
    b = h.body()
    cur = get_setting("reminder", REMINDER_DEFAULT)
    if "enabled" in b:
        cur["enabled"] = bool(b["enabled"])
    if "day" in b:
        try:
            cur["day"] = max(1, min(28, int(b["day"])))
        except (TypeError, ValueError):
            raise ValueError("Gün 1–28 olmalı")
    put_setting("reminder", cur)
    audit(user, "Hatırlatma", f"Ayarlar: açık={cur['enabled']} gün={cur['day']}")
    h.json(200, {"ok": True, "settings": cur})


def reminder_worker():
    while True:
        try:
            st = get_setting("reminder", REMINDER_DEFAULT)
            if st.get("enabled") and datetime.now().day == int(st.get("day") or 10):
                conn = db()
                recent = conn.execute("SELECT 1 FROM reminders WHERE user='Sistem' AND ts > ?", (time.time() - 20 * 3600,)).fetchone()
                if not recent:
                    ids = [r[0] for r in conn.execute("SELECT id FROM flats")]
                    n = prepare_reminders(conn, ids, {"name": "Sistem", "role": "system"})
                    conn.commit()
                    if n:
                        audit({"name": "Sistem", "role": "system"}, "Hatırlatma", f"Otomatik kuyruk: {n} daire")
                conn.close()
        except Exception:
            pass
        time.sleep(1800)


# ---------------------------------------------------------------- public registration and invitations
REG_HITS = {}
REG_KINDS = {"malik": "Mülk sahibi", "kiraci": "Kiracı"}
PUBLIC_CSS = """
:root{--bg:#f7f6f3;--card:#fff;--line:#e8e5dd;--text:#1b1e23;--muted:#6b7280;--accent:#1d5f8a;--accent-bg:#e9f2f9;--bad:#b3261e;--ok:#2f7d4f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:560px;margin:0 auto;padding:24px 18px 48px}
h1{font-size:24px;margin:0 0 2px}h2{font-size:15px;margin:0 0 10px}
.sub{color:var(--muted);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
label{display:block;font-size:12.5px;color:var(--muted);margin:10px 0 4px}
label:first-of-type{margin-top:0}
input,select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font:inherit;background:var(--card);color:var(--text)}
.row{display:flex;gap:10px}.row>div{flex:1;min-width:0}
.hint{font-size:12.5px;color:var(--muted);margin:6px 0 0}
.check{display:flex;gap:9px;align-items:flex-start;font-size:13.5px;color:var(--text);margin-top:8px}
.check input{width:auto;margin-top:3px}
button{margin-top:14px;width:100%;padding:12px;border:0;border-radius:10px;background:var(--text);color:var(--card);font:inherit;font-weight:600;cursor:pointer}
.err{color:var(--bad);font-size:13px;margin-top:8px;min-height:1em}
.ok{color:var(--ok);font-size:15px;font-weight:600}
.big{font-family:Consolas,monospace;font-size:20px;letter-spacing:.06em;display:inline-block;padding:6px 10px;border:2px solid var(--text);border-radius:8px;margin:8px 0}
.plogo{width:64px;height:64px;border-radius:18px;object-fit:cover;display:block;margin:0 0 12px;border:1px solid var(--line)}
"""


def public_page(title, body):
    return ("<!doctype html><html lang=\"tr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{title}</title><style>{PUBLIC_CSS}</style></head><body><main class=\"wrap\">{body}</main></body></html>")


def norm_phone(s):
    d = re.sub(r"\D", "", str(s or ""))
    if d.startswith("90") and len(d) == 12:
        d = d[2:]
    if d.startswith("0") and len(d) == 11:
        d = d[1:]
    return d


def reg_rate_ok(ip):
    now = time.time()
    hits = [t for t in REG_HITS.get(ip, []) if t > now - 3600]
    if len(hits) >= 5:
        REG_HITS[ip] = hits
        return False
    hits.append(now)
    REG_HITS[ip] = hits
    return True


def unique_username(name):
    base = re.sub(r"[^a-z0-9]+", ".", fold_words(name)).strip(".")[:24] or "sakin"
    if base in cfg["users"]:
        i = 2
        while f"{base}{i}" in cfg["users"]:
            i += 1
        base = f"{base}{i}"
    return base


@route("GET", "/demo", need="none")
def r_demo_role(h, qs, user):
    """Demo-only: switch the viewed role and continue to a page, e.g. /demo?role=resident&to=/%23/dairem."""
    role = qs.get("role", "")
    if role not in ROLES:
        return h.json(400, {"error": "Rol geçersiz"})
    to = qs.get("to", "/")
    if not to.startswith("/"):
        to = "/"
    h.raw(302, b"", "text/plain; charset=utf-8",
          {"Set-Cookie": f"adalya_demo_role={role}; Path=/; SameSite=Strict", "Location": to})


@route("GET", "/kayit", need="none")
def r_kayit_page(h, qs, user):
    import html as html_lib
    nm = html_lib.escape(site["site_name"])
    body = """
<img class="plogo" src="/icon-192.png?v=2" alt="">
<h1>__SITE__</h1><p class="sub">Site sakini kaydı · yönetim onayından sonra etkinleşir</p>
<form id="reg">
  <section class="card"><h2>Kimsiniz?</h2>
    <label for="rgName">Ad soyad</label><input id="rgName" maxlength="80" autocomplete="name">
    <label for="rgPhone">Cep telefonu</label><input id="rgPhone" inputmode="tel" maxlength="20" placeholder="05xx xxx xx xx" autocomplete="tel">
    <label for="rgEmail">E-posta <span style="color:var(--muted)">(isteğe bağlı)</span></label><input id="rgEmail" type="email" maxlength="120" autocomplete="email">
    <div class="row"><div><label for="rgBlok">Blok</label><select id="rgBlok"><option>A</option><option>B</option><option>C</option></select></div>
      <div><label for="rgKat">Kat</label><input id="rgKat" maxlength="10" placeholder="3"></div>
      <div><label for="rgDaire">Daire</label><input id="rgDaire" maxlength="10" inputmode="numeric" placeholder="5"></div></div>
    <label for="rgKind">Durum</label><select id="rgKind"><option value="malik">Mülk sahibi</option><option value="kiraci">Kiracı</option></select>
  </section>
  <section class="card"><h2>Araç</h2>
    <label class="check"><input type="checkbox" id="rgCar"> Aracım var (en fazla 2)</label>
    <div id="carBox" hidden>
      <div class="row"><div><label for="rgP1">Plaka</label><input id="rgP1" maxlength="12" placeholder="07 ABC 123"></div>
        <div><label for="rgM1">Marka model</label><input id="rgM1" maxlength="60"></div></div>
      <div class="row"><div><label for="rgP2">2. plaka (varsa)</label><input id="rgP2" maxlength="12"></div>
        <div><label for="rgM2">Marka model</label><input id="rgM2" maxlength="60"></div></div>
    </div>
  </section>
  <section class="card"><h2>Cihaz</h2>
    <label for="rgDev">Bu hesabı kaç cihazda kullanacaksınız?</label>
    <select id="rgDev"><option value="1">1 cihaz</option><option value="2">2 cihaz (telefon + tablet gibi)</option></select>
    <p class="hint">En fazla 2 cihaz bağlanabilir. Telefon değişirse yönetim eski cihazı kaldırır.</p>
  </section>
  <section class="card"><h2>Onay</h2>
    <label class="check"><input type="checkbox" id="rgConsent"> Ad, telefon, daire ve araç bilgilerimin site yönetimi tarafından üyelik ve güvenlik amacıyla işlenmesini kabul ediyorum (KVKK).</label>
    <input type="text" id="rgHp" name="website" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px" aria-hidden="true">
    <button type="submit">Kayıt isteği gönder</button>
    <div class="err" id="rgErr"></div>
  </section>
</form>
<div class="card" id="done" hidden>
  <div class="ok">Kayıt isteğiniz alındı.</div>
  <p>Başvuru numaranız:</p><div class="big" id="ref"></div>
  <p class="hint">Yönetim onayladıktan sonra size gönderilen davet bağlantısıyla şifrenizi belirleyebilirsiniz.</p>
  <a href="/" style="display:inline-block;margin-top:8px">Panele dön</a>
</div>
<script>
const $ = s => document.querySelector(s);
$("#rgCar").onchange = () => { $("#carBox").hidden = !$("#rgCar").checked; };
$("#reg").onsubmit = async (e) => {
  e.preventDefault();
  $("#rgErr").textContent = "";
  const vehicles = [];
  if ($("#rgCar").checked) {
    if ($("#rgP1").value.trim()) vehicles.push({plate:$("#rgP1").value, model:$("#rgM1").value});
    if ($("#rgP2").value.trim()) vehicles.push({plate:$("#rgP2").value, model:$("#rgM2").value});
  }
  const body = {
    name: $("#rgName").value.trim(), phone: $("#rgPhone").value.trim(), email: $("#rgEmail").value.trim(),
    blok: $("#rgBlok").value, kat: $("#rgKat").value.trim(), daire: $("#rgDaire").value.trim(),
    kind: $("#rgKind").value, vehicles, device_count: +$("#rgDev").value,
    consent: $("#rgConsent").checked, website: $("#rgHp").value
  };
  try {
    const r = await fetch("/api/register", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Kayıt alınamadı");
    $("#reg").hidden = true; $("#done").hidden = false; $("#ref").textContent = d.ref;
  } catch (err) { $("#rgErr").textContent = err.message; }
};
</script>"""
    body = body.replace("__SITE__", nm)
    h.raw(200, public_page(f"Kayıt · {site['site_name']}", body).encode("utf-8"), "text/html; charset=utf-8")


@route("POST", "/api/register", need="none")
def r_register(h, qs, user):
    ip = h.client_address[0]
    if not reg_rate_ok(ip):
        return h.json(429, {"error": "Çok fazla kayıt denemesi. Bir süre sonra tekrar deneyin."})
    b = h.body(max_bytes=200_000)
    if str(b.get("website") or "").strip():
        return h.json(400, {"error": "Kayıt alınamadı"})
    name = txt(b, "name", 80)
    phone = norm_phone(b.get("phone"))
    email = txt(b, "email", 120)
    blok, kat, daire = txt(b, "blok", 5).upper(), txt(b, "kat", 10), txt(b, "daire", 10)
    kind = b.get("kind") if b.get("kind") in REG_KINDS else "malik"
    if len(name) < 3:
        raise ValueError("Ad soyad girin")
    if len(phone) != 10 or not phone.startswith("5"):
        raise ValueError("Geçerli bir cep telefonu girin (05xx xxx xx xx)")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("E-posta geçersiz")
    if not blok or not daire:
        raise ValueError("Blok ve daire gerekli")
    if not b.get("consent"):
        raise ValueError("KVKK onayı gerekli")
    vehicles = []
    for v in (b.get("vehicles") or [])[:2]:
        plate = re.sub(r"\s+", " ", str(v.get("plate") or "")).strip().upper()[:12]
        if plate:
            vehicles.append({"plate": plate, "model": txt(v, "model", 60)})
    try:
        dev = max(1, min(2, int(b.get("device_count") or 1)))
    except (TypeError, ValueError):
        dev = 1
    conn = db()
    rid = conn.execute("INSERT INTO registrations (ts, ref, name, phone, email, blok, kat, daire, kind, vehicles, "
                       "device_count, note, status, ip) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'bekliyor',?)",
                       (time.time(), "", name, phone, email, blok, kat, daire, kind,
                        json.dumps(vehicles, ensure_ascii=False), dev, txt(b, "note", 300), ip)).lastrowid
    ref = f"K-{datetime.now().year}-{rid:04d}"
    conn.execute("UPDATE registrations SET ref=? WHERE id=?", (ref, rid))
    conn.commit()
    conn.close()
    audit({"name": "Kayıt formu", "role": "system"}, "Kayıt isteği", f"{ref} · {name}")
    h.json(200, {"ok": True, "ref": ref})


@route("GET", "/api/registrations", need="admin")
def r_registrations(h, qs, user):
    conn = db()
    rows = conn.execute("SELECT id, ts, ref, name, phone, email, blok, kat, daire, kind, vehicles, device_count, note, "
                        "status, flat_id, decided_by, decided_at, reject_reason FROM registrations "
                        "ORDER BY (status!='bekliyor'), ts DESC LIMIT 300").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            vehicles = json.loads(r[10] or "[]")
        except ValueError:
            vehicles = []
        out.append({"id": r[0], "ts": r[1], "ref": r[2], "name": r[3], "phone": r[4], "email": r[5] or "",
                    "blok": r[6], "kat": r[7] or "", "daire": r[8], "kind": r[9], "vehicles": vehicles,
                    "device_count": r[11] or 1, "note": r[12] or "", "status": r[13], "flat_id": r[14],
                    "decided_by": r[15] or "", "decided_at": r[16], "reject_reason": r[17] or ""})
    h.json(200, {"registrations": out, "kinds": REG_KINDS,
                 "pending": sum(1 for x in out if x["status"] == "bekliyor")})


@route("POST", "/api/registrations/approve", need="admin")
def r_reg_approve(h, qs, user):
    b = h.body()
    rid, force = b.get("id"), bool(b.get("force"))
    conn = db()
    r = conn.execute("SELECT id, name, phone, email, blok, kat, daire, kind, vehicles, status FROM registrations WHERE id=?",
                     (rid,)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Kayıt bulunamadı")
    if r[9] != "bekliyor":
        conn.close()
        raise ValueError("Bu kayıt zaten sonuçlandırılmış")
    name, phone, email, blok, kat, daire, kind = r[1], r[2], r[3], r[4], r[5], r[6], r[7]
    try:
        vehicles = json.loads(r[8] or "[]")
    except ValueError:
        vehicles = []
    flat = conn.execute("SELECT id FROM flats WHERE blok=? AND daire=?", (blok, daire)).fetchone()
    if flat:
        fid = flat[0]
        if kat:
            conn.execute("UPDATE flats SET kat=COALESCE(NULLIF(kat,''),?) WHERE id=?", (kat, fid))
    else:
        fid = conn.execute("INSERT INTO flats (blok, kapi, daire, arsa_payi, notes, kat) VALUES (?,?,?,NULL,'',?)",
                           (blok, "", daire, kat)).lastrowid
    accounts = [u for u, rec in cfg["users"].items() if rec.get("role") == "resident" and rec.get("flat_id") == fid]
    if len(accounts) >= 2 and not force:
        conn.close()
        raise ValueError("Bu dairede 2 hesap var. Yine de eklemek için onaylayın.")
    if not conn.execute("SELECT 1 FROM people WHERE flat_id=? AND name=?", (fid, name)).fetchone():
        conn.execute("INSERT INTO people (flat_id, name, phone, email, kind, move_in, move_out, emergency, notes) "
                     "VALUES (?,?,?,?,?,?,'','','')", (fid, name, phone, email, kind, datetime.now().strftime("%Y-%m-%d")))
    for v in vehicles[:2]:
        pk = plate_key(v.get("plate") or "")
        if len(pk) < 4 or conn.execute("SELECT 1 FROM vehicles WHERE plate_key=?", (pk,)).fetchone():
            continue
        conn.execute("INSERT INTO vehicles (flat_id, plate, plate_key, model, color, parking, sticker) "
                     "VALUES (?,?,?,?,'','','')", (fid, v["plate"], pk, (v.get("model") or "")[:60]))
    conn.commit()
    conn.close()
    username = unique_username(name)
    tok = secrets.token_urlsafe(24)
    cfg["users"][username] = {"name": name, "role": "resident", "flat_id": fid, "status": "davetli", "phone": phone,
                              "invite": tok, "invite_exp": time.time() + 14 * 86400, "created_by": user["name"]}
    save_cfg()
    conn = db()
    conn.execute("UPDATE registrations SET status='onaylandi', flat_id=?, decided_by=?, decided_at=? WHERE id=?",
                 (fid, user["name"], time.time(), rid))
    conn.commit()
    conn.close()
    audit(user, "Kayıt onayı", f"{name} · {flat_label({'blok': blok, 'daire': daire})}")
    h.json(200, {"ok": True, "username": username, "invite_url": f"/davet?token={tok}", "phone": phone,
                 "flat": flat_label({"blok": blok, "daire": daire})})


@route("POST", "/api/registrations/reject", need="admin")
def r_reg_reject(h, qs, user):
    b = h.body()
    rid = b.get("id")
    reason = txt(b, "reason", 300)
    conn = db()
    r = conn.execute("SELECT name, status FROM registrations WHERE id=?", (rid,)).fetchone()
    if not r:
        conn.close()
        raise ValueError("Kayıt bulunamadı")
    if r[1] != "bekliyor":
        conn.close()
        raise ValueError("Bu kayıt zaten sonuçlandırılmış")
    conn.execute("UPDATE registrations SET status='reddedildi', decided_by=?, decided_at=?, reject_reason=? WHERE id=?",
                 (user["name"], time.time(), reason, rid))
    conn.commit()
    conn.close()
    audit(user, "Kayıt reddi", f"{r[0]}{' · ' + reason if reason else ''}")
    h.json(200, {"ok": True})


@route("GET", "/davet", need="none")
def r_davet_page(h, qs, user):
    tok = str(qs.get("token") or "")
    body = f"""
<h1>Davet</h1><p class="sub">Şifrenizi belirleyerek hesabınızı etkinleştirin</p>
<div class="card">
  <label for="dvPw">Yeni şifre (en az 8 karakter)</label><input id="dvPw" type="password" autocomplete="new-password">
  <label for="dvPw2">Şifre tekrar</label><input id="dvPw2" type="password" autocomplete="new-password">
  <button id="dvGo">Etkinleştir</button>
  <div class="err" id="dvErr"></div>
</div>
<script>
const $ = s => document.querySelector(s);
$("#dvGo").onclick = async () => {{
  $("#dvErr").textContent = "";
  if ($("#dvPw").value.length < 8) {{ $("#dvErr").textContent = "Şifre en az 8 karakter olmalı."; return; }}
  if ($("#dvPw").value !== $("#dvPw2").value) {{ $("#dvErr").textContent = "Şifreler aynı değil."; return; }}
  try {{
    const r = await fetch("/api/invite/accept", {{method:"POST", headers:{{"Content-Type":"application/json"}},
      body: JSON.stringify({{token: {json.dumps(tok)}, password: $("#dvPw").value}})}});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Etkinleştirilemedi");
    location.href = "/";
  }} catch (e) {{ $("#dvErr").textContent = e.message; }}
}};
</script>"""
    h.raw(200, public_page(f"Davet · {site['site_name']}", body).encode("utf-8"), "text/html; charset=utf-8")


@route("POST", "/api/invite/accept", need="none")
def r_invite_accept(h, qs, user):
    b = h.body()
    tok, pw = str(b.get("token") or ""), str(b.get("password") or "")
    uname = next((u for u, r in cfg["users"].items() if r.get("invite") == tok), None)
    if not uname:
        raise ValueError("Davet geçersiz veya kullanılmış")
    rec = cfg["users"][uname]
    if rec.get("invite_exp", 0) < time.time():
        raise ValueError("Davet süresi dolmuş; yönetimden yeni davet isteyin")
    if len(pw) < 8:
        raise ValueError("Şifre en az 8 karakter olmalı")
    rec["pw"] = hash_pw(pw)
    rec["status"] = "active"
    rec.pop("invite", None)
    rec.pop("invite_exp", None)
    save_cfg()
    audit({"name": rec["name"], "role": "resident"}, "Davet kabul", uname)
    u = {"username": uname, "name": rec["name"], "role": "resident", "flat_id": rec.get("flat_id")}
    h.start_session(u)


# ---------------------------------------------------------------- settings stored in the database (data/, never committed)

DUES_DEFAULT = {"mode": "esit", "amount": 0.0, "total": 0.0, "due_day": 10, "late_pct": 5.0, "iban": "", "account_name": "", "pay_url": ""}
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
    rc = receipt_map(conn)
    for row in rows:
        if row.get("type") == "payment":
            v = rc.get(row["id"])
            row["receipt"] = receipt_serial(*v) if v else ""
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
    pay_url = txt(b, "pay_url", 500)
    if pay_url and not re.match(r"^https?://", pay_url):
        raise ValueError("Ödeme bağlantısı http:// veya https:// ile başlamalı")
    d["pay_url"] = pay_url
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


def receipt_serial(no, year):
    return f"{year}-{no:05d}"


def receipt_map(conn):
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT payment_id, no, year FROM receipts")}


def ensure_receipt(conn, payment_id, user):
    ex = conn.execute("SELECT no, year FROM receipts WHERE payment_id=?", (payment_id,)).fetchone()
    if ex:
        return ex[0], ex[1]
    p = conn.execute("SELECT flat_id FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not p:
        raise ValueError("Ödeme bulunamadı")
    year = datetime.now().year
    no = conn.execute("SELECT COALESCE(MAX(no),0) FROM receipts WHERE year=?", (year,)).fetchone()[0] + 1
    conn.execute("INSERT INTO receipts (no, year, payment_id, flat_id, ts, user) VALUES (?,?,?,?,?,?)",
                 (no, year, payment_id, p[0], time.time(), user["name"]))
    return no, year


@route("POST", "/api/dues/receipt", need="admin")
def r_dues_receipt(h, qs, user):
    pid = h.body().get("payment_id")
    conn = db()
    no, year = ensure_receipt(conn, pid, user)
    conn.commit()
    conn.close()
    audit(user, "Makbuz", f"Ödeme {pid} · {receipt_serial(no, year)}")
    h.json(200, {"ok": True, "serial": receipt_serial(no, year), "url": f"/makbuz?id={pid}"})


@route("GET", "/makbuz", need="user")
def r_makbuz(h, qs, user):
    import html as html_lib
    pid = qs.get("id")
    conn = db()
    p = conn.execute("SELECT p.id, p.flat_id, p.date, p.amount, p.method, p.note, f.blok, f.daire FROM payments p "
                     "LEFT JOIN flats f ON f.id=p.flat_id WHERE p.id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        raise ValueError("Ödeme bulunamadı")
    if user["role"] == "resident" and p[1] != user.get("flat_id"):
        conn.close()
        return h.json(403, {"error": "Bu makbuz size ait değil"})
    if user["role"] == "staff":
        conn.close()
        return h.json(403, {"error": "Makbuzlar güvenlik rolüne kapalı"})
    rc = conn.execute("SELECT no, year FROM receipts WHERE payment_id=?", (pid,)).fetchone()
    if not rc:
        if user["role"] != "admin":
            conn.close()
            raise ValueError("Bu ödeme için makbuz düzenlenmemiş")
        no, year = ensure_receipt(conn, pid, user)
        conn.commit()
        rc = (no, year)
    conn.close()
    serial = receipt_serial(rc[0], rc[1])
    amount = f"{p[3]:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    try:
        pay_date = datetime.strptime(str(p[2]), "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        pay_date = str(p[2])
    flat = flat_label({"blok": p[6], "daire": p[7]})
    method = PAY_METHODS.get(p[4], p[4])
    note = html_lib.escape(p[5] or "—")
    body = f"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><title>Makbuz {serial}</title>
<style>
body{{font:14px/1.65 "Segoe UI",sans-serif;color:#1b1e23;max-width:640px;margin:48px auto;padding:0 22px}}
.serial{{font-family:Consolas,monospace;font-size:15px;font-weight:700;letter-spacing:.04em}}
h1{{font-size:20px;margin:6px 0 2px}} .sub{{color:#6b7280;font-size:13px}}
table{{width:100%;border-collapse:collapse;margin:20px 0}} td{{padding:8px 0;border-bottom:1px solid #e5e2da}}
td.r{{text-align:right}} .big{{font-size:20px;font-weight:700}}
.note{{color:#6b7280;font-size:12px;margin-top:18px}}
.sig{{margin-top:54px;border-top:1px solid #9a9a9a;width:250px;padding-top:6px;font-size:12px;color:#6b7280}}
.print{{margin-top:26px;padding:9px 16px;border:0;border-radius:8px;background:#1b1e23;color:#fff;font-size:14px;cursor:pointer}}
@media print{{.print{{display:none}} body{{margin:0}}}}
</style></head><body>
<div class="serial">Makbuz {serial}</div>
<h1>{html_lib.escape(site["site_name"])} · Aidat Makbuzu</h1>
<div class="sub">{html_lib.escape(flat)} · {pay_date} tarihli ödeme</div>
<table>
<tr><td>Tutar</td><td class="r big">{amount} TL</td></tr>
<tr><td>Ödeme türü</td><td class="r">{html_lib.escape(method)}</td></tr>
<tr><td>Açıklama</td><td class="r">{note}</td></tr>
<tr><td>Makbuz tarihi</td><td class="r">{datetime.now().strftime('%d.%m.%Y %H:%M')}</td></tr>
</table>
<div class="note">Bu makbuz {html_lib.escape(site["site_name"])} yönetimi tarafından düzenlenmiştir. Kişisel veri içerir; yalnızca ilgili daire ve yönetim saklar.</div>
<div class="sig">Yönetim imza / kaşe</div>
<button class="print" onclick="window.print()">Yazdır / PDF olarak kaydet</button>
</body></html>"""
    h.raw(200, body.encode("utf-8"), "text/html; charset=utf-8")


def read_text_payload(b):
    import base64
    raw = str(b.get("data") or "")
    m = re.match(r"^data:[^;]*;base64,(.*)$", raw, re.S)
    if m:
        try:
            raw = base64.b64decode(m.group(1), validate=False)
        except (ValueError, TypeError):
            raise ValueError("Dosya okunamadı")
    else:
        raw = raw.encode("utf-8")
    for enc in ("utf-8-sig", "cp1254", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Dosya okunamadı")


def norm_bank_date(s):
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now().strftime("%Y-%m-%d")


def parse_bank_csv(text):
    sample = text[:4000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        raise ValueError("Dosya boş")
    names = {"date": ("tarih", "date"), "amount": ("tutar", "amount", "miktar"),
             "desc": ("aciklama", "açıklama", "description", "detay")}
    header = [fold(c) for c in rows[0]]
    idx = {}
    for key, keys in names.items():
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                idx[key] = i
                break
    data = rows[1:] if len(idx) == 3 else rows
    if len(idx) != 3:
        idx = {"date": 0, "amount": 1, "desc": 2}
    out = []
    for r in data:
        if max(idx.values()) >= len(r):
            continue
        try:
            amt = money(r[idx["amount"]], "Tutar")
        except ValueError:
            continue
        if amt <= 0:
            continue
        out.append({"date": norm_bank_date(r[idx["date"]]), "amount": amt,
                    "desc": r[idx["desc"]] if idx["desc"] < len(r) else ""})
    if not out:
        raise ValueError("Tarih/tutar/açıklama satırı bulunamadı")
    return out


def match_flat(conn, desc):
    text = fold_words(desc)
    pk = plate_key(desc)
    if len(pk) >= 5:
        r = conn.execute("SELECT flat_id FROM vehicles WHERE plate_key LIKE ? LIMIT 1", (f"%{pk}%",)).fetchone()
        if r:
            return r[0], "plaka"
    m = re.search(r"\b([abc])\s+blok\s+(\d{1,2})\b", text)
    if m:
        r = conn.execute("SELECT id FROM flats WHERE lower(blok)=? AND daire=?", (m.group(1), m.group(2))).fetchone()
        if r:
            return r[0], "daire"
    m = re.search(r"\bdaire\s*[-:]?\s*(\d{1,2})\b", text)
    if m:
        rs = conn.execute("SELECT id FROM flats WHERE daire=?", (m.group(1),)).fetchall()
        if len(rs) == 1:
            return rs[0][0], "daire"
    for r in conn.execute("SELECT DISTINCT flat_id, name FROM people WHERE COALESCE(move_out,'')=''"):
        parts = [p for p in fold_words(r[1]).split() if len(p) >= 3]
        if parts and all(p in text.split() for p in parts):
            return r[0], "isim"
    return None, ""


@route("POST", "/api/dues/bank/preview", need="admin")
def r_bank_preview(h, qs, user):
    text = read_text_payload(h.body(max_bytes=5_000_000))
    rows = parse_bank_csv(text)
    conn = db()
    flats = {r[0]: flat_label({"blok": r[1], "daire": r[2]}) for r in conn.execute("SELECT id, blok, daire FROM flats")}
    out = []
    for r in rows[:200]:
        fid, why = match_flat(conn, r["desc"])
        out.append({**r, "flat_id": fid, "flat": flats.get(fid, ""), "why": why})
    conn.close()
    h.json(200, {"rows": out, "count": len(out),
                 "flats": [{"id": k, "label": v} for k, v in sorted(flats.items())]})


@route("POST", "/api/dues/bank/apply", need="admin")
def r_bank_apply(h, qs, user):
    rows = h.body().get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise ValueError("Aktarılacak satır yok")
    conn = db()
    n, total = 0, 0.0
    for r in rows[:200]:
        try:
            fid, amt = int(r.get("flat_id")), money(r.get("amount"), "Tutar")
        except (TypeError, ValueError):
            continue
        date = opt_date(r, "date") or datetime.now().strftime("%Y-%m-%d")
        if not fid or amt <= 0 or not conn.execute("SELECT 1 FROM flats WHERE id=?", (fid,)).fetchone():
            continue
        desc = str(r.get("desc") or "")[:120]
        conn.execute("INSERT INTO payments (flat_id, date, amount, method, note, user, ts) VALUES (?,?,?,?,?,?,?)",
                     (fid, date, amt, "havale", ("Banka: " + desc)[:200], user["name"], time.time()))
        n += 1
        total += amt
    conn.commit()
    conn.close()
    audit(user, "Banka içe aktarımı", f"{n} ödeme · {round(total, 2)} TL")
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
        if table == "payments":
            conn.execute("DELETE FROM receipts WHERE payment_id=?", (b.get("id"),))
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
            return {**DEMO_USER, "role": role, "name": "Demo " + ROLES[role].lower(), "flat_id": 1 if role == "resident" else None,
                    "twofa": False}
        s = sessions.get(self.cookie_token())
        if s and s["exp"] > time.time():
            rec = cfg["users"].get(s["username"])
            if not rec:
                return None
            s["exp"] = time.time() + 12 * 3600
            return {"username": s["username"], "name": rec["name"], "role": rec["role"], "flat_id": rec.get("flat_id"),
                    "twofa": bool(rec.get("totp_on"))}
        return None

    def cookie_secure(self):
        return "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""

    def start_session(self, u, extra_cookies=None):
        tok = secrets.token_urlsafe(32)
        sessions[tok] = {**u, "exp": time.time() + 12 * 3600}
        cookies = [f"adalya_session={tok}; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict{self.cookie_secure()}"]
        cookies.extend(extra_cookies or [])
        self.json(200, {"ok": True, "user": u}, {"Set-Cookie": cookies})

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
            if isinstance(v, (list, tuple)):
                for item in v:
                    self.send_header(k, item)
            else:
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


class Server(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR lets a second server bind a port that is already in use (requests then
    land on either one at random). Use an exclusive bind there instead, so a busy port is detected."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def open_server(host, port, fixed):
    """Bind the requested port; if it is the default and busy, try the next ten."""
    for p in [port] if fixed else range(port, port + 10):
        try:
            return Server((host, p), Handler), p
        except OSError:
            if fixed:
                raise SystemExit(f"Port {p} is already in use (is the panel already running?). Close it or choose another with --port.")
    raise SystemExit(f"Ports {port}–{port + 9} are all in use. Choose another with --port.")


def main():
    global args, nvr, DATA, DB_PATH, CFG_PATH, DEVICES, SITE_PATH
    p = argparse.ArgumentParser(description="Adalya Panel")
    p.add_argument("--port", type=int, default=None, help="default 8080 (or the next free port)")
    p.add_argument("--lan", action="store_true", help="listen on all interfaces (site screens)")
    p.add_argument("--demo", action="store_true", help="simulated site and NVR")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between ping rounds")
    p.add_argument("--misses", type=int, default=2, help="missed pings before a device counts as down")
    p.add_argument("--nvr-host")
    p.add_argument("--nvr-user")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    if args.demo:
        # The demo works on fresh copies in data-demo/, so trying it never changes the real
        # devices.json, site.json or memorial photo (and a fresh clone stays clean).
        DATA = os.path.join(HERE, "data-demo")
        DB_PATH, CFG_PATH = os.path.join(DATA, "panel.db"), os.path.join(DATA, "config.json")
        os.makedirs(DATA, exist_ok=True)
        for src, name in ((DEVICES, "devices.json"), (SITE_PATH, "site.json")):
            dst = os.path.join(DATA, name)
            if os.path.exists(src):
                with open(src, "rb") as f, open(dst, "wb") as g:
                    g.write(f.read())
        DEVICES, SITE_PATH = os.path.join(DATA, "devices.json"), os.path.join(DATA, "site.json")
    os.makedirs(DATA, exist_ok=True)
    if args.demo and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    if args.demo:
        for sub in ("belgeler", "talepler", "yedekler"):
            bdir = os.path.join(DATA, sub)
            if os.path.isdir(bdir):
                for n in os.listdir(bdir):
                    try:
                        os.remove(os.path.join(bdir, n))
                    except OSError:
                        pass
    load_cfg()
    load_site()
    load_devices()
    db().close()
    if args.demo:
        seed_demo_registry()
        seed_demo_management()
        seed_demo_comms()

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

    srv, port = open_server("0.0.0.0" if args.lan else "127.0.0.1", args.port or 8080, args.port is not None)
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=backup_worker, daemon=True).start()
    threading.Thread(target=reminder_worker, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"Adalya Panel {VERSION} running at {url}{'  (demo)' if args.demo else ''}")
    if port != 8080 and args.port is None:
        print("(port 8080 was busy, so the next free port is used)")
    if args.lan:
        print(f"Other screens on the site network: http://<this-computer-ip>:{port}")
    print("Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
