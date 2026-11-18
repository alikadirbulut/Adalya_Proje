"""Dahua NVR client for Adalya Panel. Read-only, Python standard library only.

Uses the Dahua HTTP API (CGI). Every call makes at most one authenticated attempt;
if the NVR refuses the login, the client locks itself so it can never trigger the
NVR's own account lockout by retrying.
"""

import base64
import hashlib
import http.client
import os
import random
import re
from datetime import datetime, timedelta
from urllib.parse import quote


class NvrError(Exception):
    pass


class NvrAuthError(Exception):
    pass


TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def q(v):
    return quote(str(v), safe="")


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def parse_kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def group_items(kv, prefix="items"):
    rows = {}
    pat = re.compile(re.escape(prefix) + r"\[(\d+)\]\.(.+)")
    for k, v in kv.items():
        m = pat.match(k)
        if m:
            rows.setdefault(int(m.group(1)), {})[m.group(2)] = v
    return [rows[i] for i in sorted(rows)]


def _challenge(header):
    return dict(re.findall(r'(\w+)="?([^",]+)"?', header))


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


class Nvr:
    demo = False

    def __init__(self, host, user, password, port=80, timeout=10):
        self.host, self.user, self.password, self.port, self.timeout = host, user, password, port, timeout
        self.locked = False

    def _auth(self, path, header):
        if not header.lower().startswith("digest"):
            return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        a = _challenge(header)
        realm, nonce, qop = a.get("realm", ""), a.get("nonce", ""), a.get("qop", "")
        ha1 = md5(f"{self.user}:{realm}:{self.password}")
        ha2 = md5(f"GET:{path}")
        if "auth" in qop:
            cnonce, nc = os.urandom(8).hex(), "00000001"
            resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
            h = (f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", uri="{path}", '
                 f'qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"')
        else:
            resp = md5(f"{ha1}:{nonce}:{ha2}")
            h = f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", uri="{path}", response="{resp}"'
        if "opaque" in a:
            h += f', opaque="{a["opaque"]}"'
        return h

    def open(self, path, timeout=None):
        if self.locked:
            raise NvrAuthError("NVR girişi daha önce reddedildi. Ayarlar sayfasından yeniden bağlanın.")
        t = timeout or self.timeout
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=t)
            conn.request("GET", path)
            r = conn.getresponse()
            if r.status != 401:
                return conn, r
            hdr = r.getheader("WWW-Authenticate", "")
            r.read()
            conn.close()
            conn = http.client.HTTPConnection(self.host, self.port, timeout=t)
            conn.request("GET", path, headers={"Authorization": self._auth(path, hdr)})
            r = conn.getresponse()
        except OSError as e:
            raise NvrError(f"NVR'ye ulaşılamıyor ({self.host}): {e}")
        if r.status == 401:
            r.read()
            conn.close()
            self.locked = True
            raise NvrAuthError("NVR kullanıcı adını veya şifreyi reddetti. Hesabın kilitlenmemesi için bağlantı durduruldu.")
        return conn, r

    def get(self, path, timeout=None):
        conn, r = self.open(path, timeout)
        try:
            body = r.read()
        finally:
            conn.close()
        if r.status != 200:
            raise NvrError(f"NVR HTTP {r.status} döndü ({path.split('?')[0]})")
        return body

    def kv(self, path):
        return parse_kv(self.get(path).decode("utf-8", "replace"))

    def info(self):
        out = {"type": self.kv("/cgi-bin/magicBox.cgi?action=getDeviceType").get("type", "")}
        for key, path, field in (("firmware", "/cgi-bin/magicBox.cgi?action=getSoftwareVersion", "version"),
                                 ("time", "/cgi-bin/global.cgi?action=getCurrentTime", "result")):
            try:
                out[key] = self.kv(path).get(field, "")
            except NvrError:
                out[key] = ""
        channels = {}
        try:
            for k, v in self.kv("/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle").items():
                m = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name$", k)
                if m:
                    channels[int(m.group(1)) + 1] = v
        except NvrError:
            pass
        out["channels"] = channels
        out.update(self.disks())
        return out

    def disks(self):
        try:
            kv = self.kv("/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo")
        except NvrError:
            return {"disk_pct": None, "disk_total_gb": None, "disk_states": []}
        total = sum(_to_int(v) for k, v in kv.items() if k.endswith("TotalBytes"))
        used = sum(_to_int(v) for k, v in kv.items() if k.endswith("UsedBytes"))
        return {"disk_pct": round(100 * used / total) if total else None,
                "disk_total_gb": round(total / 1e9) if total else None,
                "disk_states": sorted({v for k, v in kv.items() if k.endswith(".State")})}

    def snapshot(self, ch):
        conn, r = self.open(f"/cgi-bin/snapshot.cgi?channel={int(ch)}", timeout=8)
        try:
            body = r.read()
        finally:
            conn.close()
        if r.status != 200:
            raise NvrError(f"Görüntü alınamadı (kanal {ch}, HTTP {r.status})")
        return r.getheader("Content-Type", "image/jpeg"), body

    def records(self, ch, start, end, limit=500):
        obj = self.kv("/cgi-bin/mediaFileFind.cgi?action=factory.create").get("result")
        if not obj:
            raise NvrError("NVR kayıt aramasını başlatamadı")
        try:
            try:
                self.get(f"/cgi-bin/mediaFileFind.cgi?action=findFile&object={obj}&condition.Channel={int(ch)}"
                         f"&condition.StartTime={q(start)}&condition.EndTime={q(end)}&condition.Types[0]=dav")
            except NvrError:
                return []
            files = []
            while len(files) < limit:
                items = group_items(self.kv(f"/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={obj}&count=100"))
                for it in items:
                    files.append({
                        "start": it.get("StartTime", ""), "end": it.get("EndTime", ""),
                        "size": _to_int(it.get("Length")), "path": it.get("FilePath", ""),
                        "type": it.get("Type", ""),
                        "events": [v for k, v in it.items() if k.startswith("Events[")],
                    })
                if len(items) < 100:
                    break
            return files
        finally:
            for action in ("close", "destroy"):
                try:
                    self.get(f"/cgi-bin/mediaFileFind.cgi?action={action}&object={obj}")
                except NvrError:
                    pass

    def stream_file(self, path):
        conn, r = self.open("/cgi-bin/RPC_Loadfile" + quote(path, safe="/"), timeout=60)
        if r.status != 200:
            r.read()
            conn.close()
            raise NvrError(f"Kayıt dosyası indirilemedi (HTTP {r.status})")

        def chunks():
            try:
                while True:
                    c = r.read(65536)
                    if not c:
                        break
                    yield c
            finally:
                conn.close()
        return r.getheader("Content-Length"), chunks()

    def logs(self, start, end, limit=2000):
        tok = self.kv(f"/cgi-bin/log.cgi?action=startFind&condition.StartTime={q(start)}"
                      f"&condition.EndTime={q(end)}").get("token")
        if not tok:
            raise NvrError("NVR log aramasını başlatamadı")
        rows = []
        try:
            while len(rows) < limit:
                items = group_items(self.kv(f"/cgi-bin/log.cgi?action=doFind&token={tok}&count=100"))
                for it in items:
                    detail = "; ".join(f"{k}={v}" for k, v in it.items() if k not in ("Time", "Type", "User"))
                    rows.append({"time": it.get("Time", ""), "type": it.get("Type", ""),
                                 "user": it.get("User", ""), "detail": detail[:500]})
                if len(items) < 100:
                    break
        finally:
            try:
                self.get(f"/cgi-bin/log.cgi?action=stopFind&token={tok}")
            except NvrError:
                pass
        return rows


class DemoNvr:
    """Simulated NVR so every page can be tried away from the site."""
    demo = True
    locked = False

    def __init__(self, names):
        self.host, self.user, self.names = "demo", "demo", names

    def info(self):
        return {"type": "DHI-NVR4232-4KS3 (demo)", "firmware": "4.006.0000000.6.R",
                "time": datetime.now().strftime(TIME_FMT), "channels": self.names, **self.disks()}

    def disks(self):
        return {"disk_pct": 62, "disk_total_gb": 4000, "disk_states": ["Success"]}

    def snapshot(self, ch):
        name = self.names.get(int(ch), f"Kanal {ch}")
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        rnd = random.Random(int(ch))
        shapes = "".join(
            f'<rect x="{rnd.randint(0, 560)}" y="{rnd.randint(120, 300)}" width="{rnd.randint(40, 160)}" '
            f'height="{rnd.randint(30, 90)}" fill="#{rnd.randint(0x40, 0x70):02x}{rnd.randint(0x40, 0x70):02x}{rnd.randint(0x40, 0x70):02x}"/>'
            for _ in range(6))
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" width="640" height="360">'
               f'<rect width="640" height="360" fill="#2f3337"/><rect y="230" width="640" height="130" fill="#3a3f38"/>{shapes}'
               f'<rect x="0" y="0" width="640" height="34" fill="#000" opacity="0.45"/>'
               f'<text x="12" y="23" font-family="Segoe UI,sans-serif" font-size="16" fill="#fff">{name}</text>'
               f'<text x="628" y="23" font-family="Consolas,monospace" font-size="15" fill="#fff" text-anchor="end">{now}</text>'
               f'<text x="320" y="200" font-family="Segoe UI,sans-serif" font-size="22" fill="#fff" opacity="0.6" text-anchor="middle">Demo görüntü · kanal {ch}</text>'
               f'</svg>')
        return "image/svg+xml", svg.encode("utf-8")

    def records(self, ch, start, end, limit=500):
        s, e = datetime.strptime(start, TIME_FMT), datetime.strptime(end, TIME_FMT)
        e = min(e, datetime.now())
        t = s.replace(minute=0, second=0)
        files = []
        rnd = random.Random(f"{ch}{start}")
        while t < e and len(files) < limit:
            seg_end = min(t + timedelta(hours=1), e)
            if seg_end > s:
                files.append({"start": max(t, s).strftime(TIME_FMT), "end": seg_end.strftime(TIME_FMT),
                              "size": rnd.randint(400, 900) * 1_000_000,
                              "path": f"/mnt/dvr/demo/ch{ch}/{t:%Y%m%d%H}.dav", "type": "dav",
                              "events": ["Motion"] if rnd.random() < 0.3 else []})
            t += timedelta(hours=1)
        return files

    def stream_file(self, path):
        data = (f"ADALYA PANEL DEMO KAYDI\n{path}\nBu bir demo dosyasıdır; gerçek NVR'de .dav video dosyası iner.\n").encode("utf-8")
        return str(len(data)), iter([data])

    def logs(self, start, end, limit=2000):
        s, e = datetime.strptime(start, TIME_FMT), datetime.strptime(end, TIME_FMT)
        e = min(e, datetime.now())
        rnd = random.Random(start)
        kinds = [("Login", "admin", "Address=192.168.0.18"), ("VideoLoss", "System", "Channel=9"),
                 ("VideoLoss", "System", "Channel=11"), ("Reboot", "System", ""), ("HDD", "System", "State=Success"),
                 ("Logout", "admin", ""), ("NetAbort", "System", "Channel=2")]
        rows = []
        t = s
        while t < e and len(rows) < limit:
            t += timedelta(minutes=rnd.randint(10, 90))
            if t >= e:
                break
            k = rnd.choice(kinds)
            rows.append({"time": t.strftime(TIME_FMT), "type": k[0], "user": k[1], "detail": k[2]})
        return rows
