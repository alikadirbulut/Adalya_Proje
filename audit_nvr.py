"""Deep, read-only NVR inspection for the Adalya audit toolkit.

Builds on the Dahua client in nvr.py: one login attempt, no configuration writes.
Every probe is independent: if one endpoint is unsupported on this firmware it is
recorded as an error and the rest of the audit continues.

Nothing here stores or logs the NVR password. Requests are authenticated with an
Authorization header, so passwords never appear in URLs or files.
"""

import re
import time
from datetime import datetime, timedelta

from nvr import Nvr, NvrAuthError, NvrError, parse_kv

AUDIT_PATHS = {
    "system_info": "/cgi-bin/magicBox.cgi?action=getSystemInfo",
    "device_type": "/cgi-bin/magicBox.cgi?action=getDeviceType",
    "firmware": "/cgi-bin/magicBox.cgi?action=getSoftwareVersion",
    "serial": "/cgi-bin/magicBox.cgi?action=getSerialNo",
    "machine_name": "/cgi-bin/magicBox.cgi?action=getMachineName",
    "time": "/cgi-bin/global.cgi?action=getCurrentTime",
    "network": "/cgi-bin/configManager.cgi?action=getConfig&name=Network",
    "encode": "/cgi-bin/configManager.cgi?action=getConfig&name=Encode",
    "channel_title": "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle",
    "remote_device": "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice",
    "record": "/cgi-bin/configManager.cgi?action=getConfig&name=Record",
    "camera_state": "/cgi-bin/LogicDeviceManager.cgi?action=getCameraState&uniqueChannels[0]=-1",
    "storage": "/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo",
    "video_input": "/cgi-bin/devVideoInput.cgi?action=getCollect",
}


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _group(kv, prefix):
    """Collect table[0].X=1 style keys into [{'x': '1'}, ...] rows."""
    pat = re.compile(re.escape(prefix) + r"\[(\d+)\]\.(.+)")
    rows = {}
    for k, v in kv.items():
        m = pat.match(k)
        if m:
            rows.setdefault(int(m.group(1)), {})[m.group(2)] = v
    return [rows[i] for i in sorted(rows)]


def parse_encode(kv):
    """table.Encode[ch].MainFormat[0].Video.Width=... -> per-channel summary."""
    out = {}
    pat = re.compile(r"table\.Encode\[(\d+)\]\.(MainFormat|SubFormat)\[(\d+)\]\.(.+)")
    for k, v in kv.items():
        m = pat.match(k)
        if not m:
            continue
        ch, kind = int(m.group(1)) + 1, m.group(2).lower()
        field = m.group(4)
        out.setdefault(ch, {}).setdefault(kind, {})[field] = v
    for per in out.values():
        for kind, fields in per.items():
            want = {
                "codec": fields.get("Video.Compression") or fields.get("Video.Codec"),
                "width": _to_int(fields.get("Video.Width")),
                "height": _to_int(fields.get("Video.Height")),
                "fps": _to_int(fields.get("Video.FrameRate")),
                "bitrate_kbps": _to_int(fields.get("Video.BitRate")),
                "bitrate_control": fields.get("Video.BitRateControl", ""),
                "quality": fields.get("Video.Quality", ""),
                "enabled": fields.get("Video.Enable", ""),
            }
            fields.clear()
            fields.update({k: v for k, v in want.items() if v not in (None, "")})
    return out


def parse_storage(kv):
    disks = {}
    pat = re.compile(r"device\[(\d+)\]\.Detail\[(\d+)\]\.(\w+)")
    for k, v in kv.items():
        m = pat.match(k)
        if m:
            disks.setdefault((int(m.group(1)), int(m.group(2))), {})[m.group(3)] = v
    out = []
    for key in sorted(disks):
        d = disks[key]
        total, used = _to_int(d.get("TotalBytes")), _to_int(d.get("UsedBytes"))
        out.append({
            "index": key[1], "path": d.get("Path", ""), "type": d.get("Type", ""),
            "state": d.get("State", ""), "health": d.get("Health", ""),
            "total_gb": round(total / 1e9) if total else None,
            "used_gb": round(used / 1e9) if used else None,
            "used_pct": round(100 * used / total) if total else None,
        })
    total = sum(_to_int(v) for k, v in kv.items() if k.endswith("TotalBytes"))
    used = sum(_to_int(v) for k, v in kv.items() if k.endswith("UsedBytes"))
    return {"disks": out, "total_gb": round(total / 1e9) if total else None,
            "used_gb": round(used / 1e9) if used else None,
            "used_pct": round(100 * used / total) if total else None,
            "states": sorted({d["state"] for d in out if d["state"]})}


def parse_network(kv):
    out = {}
    for k, v in kv.items():
        low = k.lower()
        for field, name in (("ipaddress", "ip"), ("subnetmask", "mask"), ("defaultgateway", "gateway"),
                            ("dhcpenable", "dhcp"), ("macaddress", "mac")):
            if low.endswith("." + field):
                out.setdefault(name, v)
        m = re.search(r"dnsservers\[(\d+)\]$", low)
        if m:
            out.setdefault("dns", []).append(v)
    return out


class AuditNvr(Nvr):
    """Nvr subclass that records every probe and tolerates unsupported endpoints."""

    def __init__(self, host, user, password, port=80, timeout=8):
        super().__init__(host, user, password, port, timeout)
        self.calls = []
        self.raw = {}

    def probe(self, label, path, kv=True):
        t0 = time.time()
        try:
            data = self.get(path)
            ms = round((time.perf_counter() - t0) * 1000)
            self.calls.append({"probe": label, "path": path.split("?")[0], "ok": True,
                               "ms": ms, "bytes": len(data)})
            text = data.decode("utf-8", "replace")
            self.raw[label] = text
            return parse_kv(text) if kv else text
        except (NvrError, NvrAuthError) as e:
            self.calls.append({"probe": label, "path": path.split("?")[0], "ok": False,
                               "ms": round((time.perf_counter() - t0) * 1000), "error": str(e)})
            return {} if kv else ""

    def collect(self, log_hours=24):
        out = {"ts": time.time(), "started": datetime.now().isoformat(timespec="seconds"),
               "host": self.host, "errors": [], "calls": [], "unavailable": []}

        kv = {}
        for label in ("system_info", "device_type", "firmware", "serial", "machine_name", "time",
                      "network", "encode", "channel_title", "remote_device", "record",
                      "camera_state", "storage", "video_input"):
            kv[label] = self.probe(label, AUDIT_PATHS[label], kv=True)
            for c in self.calls[-1:]:
                if not c["ok"]:
                    out["unavailable"].append({"probe": label, "error": c["error"]})

        info = kv["system_info"]
        out["device"] = {
            "type": kv["device_type"].get("type", ""),
            "machine_name": kv["machine_name"].get("result", ""),
            "firmware": kv["firmware"].get("version", ""),
            "serial": kv["serial"].get("result", ""),
            "time": kv["time"].get("result", ""),
            "model": info.get("model", info.get("deviceModel", "")),
            "uptime_s": _to_int(info.get("uptime", info.get("upTime", ""))),
            "system": {k: v for k, v in info.items() if k not in ("model", "uptime", "upTime")},
        }
        out["network"] = parse_network(kv["network"])
        out["storage"] = parse_storage(kv["storage"])
        out["encode"] = parse_encode(kv["encode"])

        titles = {}
        for k, v in kv["channel_title"].items():
            m = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name$", k)
            if m:
                titles[int(m.group(1)) + 1] = v.strip()

        remote = {}
        for i, row in enumerate(_group(kv["remote_device"], "table.RemoteDevice")):
            remote[i + 1] = {
                "ip": row.get("Address", ""), "port": _to_int(row.get("Port")),
                "user": row.get("UserName", ""), "type": row.get("DeviceType", ""),
                "enable": row.get("Enable", ""),
            }

        states = {}
        for row in _group(kv["camera_state"], "camera"):
            ch = _to_int(row.get("Channel") or row.get("UniqueChannel") or 0)
            if ch:
                states[ch] = row.get("State") or row.get("Connect") or ""
        for row in _group(kv["camera_state"], "table.CameraState"):
            ch = _to_int(row.get("Channel") or 0)
            if ch:
                states[ch] = row.get("State") or row.get("Connect") or ""

        video = {}
        for i, row in enumerate(_group(kv["video_input"], "info")):
            video[i + 1] = row

        channels = []
        for ch in sorted(set(list(titles) + list(remote) + list(states) + list(out["encode"]))):
            enc = out["encode"].get(ch, {})
            channels.append({
                "channel": ch,
                "name": titles.get(ch, ""),
                "online": states.get(ch, ""),
                "remote": remote.get(ch, {}),
                "main": enc.get("mainformat", {}),
                "sub": enc.get("subformat", {}),
                "camera": video.get(ch, {}),
            })
        out["channels"] = channels

        counts = {"total": len(channels), "named": sum(1 for c in channels if c["name"]),
                  "online": sum(1 for c in channels if str(c["online"]) in ("1", "True", "true")),
                  "offline": sum(1 for c in channels if str(c["online"]) in ("0", "False", "false"))}
        out["channel_counts"] = counts

        end = datetime.now()
        start = end - timedelta(hours=log_hours)
        try:
            logs = self.logs(start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), limit=1000)
        except (NvrError, NvrAuthError) as e:
            logs = []
            out["unavailable"].append({"probe": "logs", "error": str(e)})
        types = {}
        for row in logs:
            types[row["type"]] = types.get(row["type"], 0) + 1
        out["logs"] = {
            "window_hours": log_hours, "count": len(logs), "types": types,
            "recent_errors": [r for r in logs if r["type"] in ("VideoLoss", "NetAbort", "NetDisconnect", "DiskError")][-20:],
            "recent": logs[-10:],
        }
        out["calls"] = self.calls
        return out
