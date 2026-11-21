# Infrastructure audit

A read-only diagnostic system for the site's cameras, NVR and network. It exists so that
decisions about the panel are based on measurements from the box, not assumptions.

It builds on the existing tools instead of duplicating them:

| Layer | Reuses |
|---|---|
| ping, ARP, TCP, HTTP, SNMP | `adalya_test.py` |
| Dahua NVR client | `nvr.py` |
| Device inventory | `devices.json` |
| Inventory schema | same fields the panel already uses |

## Safety

- **Read-only.** Nothing is configured, reset or written on a camera, NVR, switch or router.
- **One login attempt**, only on the NVR, only when you type the password. No camera logins,
  no brute force, no credential guessing.
- The NVR password is held in memory and never written to a file, CSV, JSON or log.
- Scans are rate-limited: short ICMP timeouts, one TCP connect per port, no port floods.
- Every probe is independent: an unreachable device is recorded, not fatal.

## Quick start

```bat
python adalya_audit.py full --user admin --monitor-min 30 --users 100
```

This runs discovery → NVR inspection → performance → 30 minutes of monitoring → report.
The NVR password is asked once and reused. Everything lands in `results/audit/`.

Individual stages:

| Command | What it does |
|---|---|
| `python adalya_audit.py discover` | ICMP/ARP sweep of the local /24, port probe (80/443/554/37777/8000), compares with `devices.json` |
| `python adalya_audit.py nvr --user admin` | ~14 read-only Dahua API probes: model, firmware, serial, network, channels, encode, storage, logs, camera states |
| `python adalya_audit.py perf --seconds 8` | loss %, min/avg/max/jitter per device, TCP 80/554 latency, NVR HTTP latency |
| `python adalya_audit.py perf --stream-ch 3` | additionally measures one NVR sub-stream (~8 s of traffic) |
| `python adalya_audit.py monitor --minutes 60 --user admin` | samples every device every 5 s + NVR state/disk every 60 s; JSONL history |
| `python adalya_audit.py report --users 100` | builds the HTML report, JSON summary and inventory CSV from the newest files |

### Useful flags

- `--subnet 192.168.0.0/24` — scan a different network than the local one.
- `--only 106,108,12` — perf/monitor only those addresses.
- `--dns` — reverse-DNS every answering host (slower).
- `--no-monitor` — full run without the monitoring stage.
- `--log-hours 24` — NVR log window for the error summary.

## Outputs (`results/audit/`)

These are the collector's working files. The **organised, analysed centre** is built from
them into `infrastructure-audit/` (raw preservation, processed datasets, six reports,
dashboard, exports, history). See [infrastructure-audit/README.md](infrastructure-audit/README.md).

| File | Content |
|---|---|
| `discover-*.json` / `.csv` | every answering host: IP, MAC, vendor guess, role, open ports, inventory match; missing / unknown / duplicate MAC / wrong MAC / DHCP-pool lists |
| `nvr-*.json` | device, network config, per-channel name/online/encode/remote IP, storage, log type counts, unavailable probes |
| `perf-*.json` | per-target loss, RTT stats, jitter, TCP latency, optional measured stream bitrate |
| `monitor-*.jsonl` | timestamped samples (`kind: device` and `kind: nvr`) — the historical baseline |
| `monitor-summary-*.csv` | uptime %, loss, min/avg/max, transitions per device |
| `report-*.html` | the human report: summary, findings, inventory, channels, latency, monitoring, capacity model, recommendations, method |
| `report-*.json` | the same content machine-readable, ready to import into the panel DB |
| `inventory-*.csv` | flat inventory export |

Every record carries a timestamp and an IP/device key, so the JSONL can be imported into
SQLite later (`audit_samples`, `audit_runs`, `audit_findings`) without reshaping.

## Build the centre

```bat
python adalya_dashboard.py build          :: results/audit -> infrastructure-audit/
python adalya_dashboard.py serve          :: http://127.0.0.1:8090/dashboard/index.html
```

`full` does the build automatically as its last stage. Layout, datasets, reports, dashboard
and export details are documented in [infrastructure-audit/README.md](infrastructure-audit/README.md).

## What it can and cannot measure

**Can:** liveness, MAC/IP mapping, open management ports, latency, loss, jitter, NVR
channel state, configured codec/resolution/FPS/bitrate per channel, disk capacity/health
state, NVR log error counts, NVR network config, serial/firmware, measured sub-stream bitrate.

**Cannot (from the wired side):** Wi-Fi RSSI per camera, airtime/channel utilization,
switch port counters (the Ruijie has no known login and no SNMP), DHCP leases (the ISP
router has no local API), camera firmware if the camera itself is not reachable/logged in.
Those come from the router client list, the NVR UI and the site walk — the report says so.

## Capacity model

The report's capacity section is a **model with printed assumptions**, not a measurement:
100 users, 20 % peak concurrency, one status poll every 3 s, 3 KB payload, 4 staff live
streams at 1.5 Mbps, camera bitrate from the NVR's configured main stream. It shows API
requests/s and bandwidth separately from video, exactly because those are different
problems. Change the knobs with `--users` (and edit the constants in
`audit_report.capacity_model` if your assumptions differ).

## Extending

- New NVR probe: add a path in `audit_nvr.AUDIT_PATHS` and parse it in `collect()`.
- New network probe: add a function in `audit_net.py`; the sweep/perf/monitor loops are
  independent.
- New finding: add a rule in `audit_report.analyze()`; severity drives ordering and the
  recommendations.
- The HTML is a single string in `render_html()`; no template engine, no external assets,
  so it opens from a USB stick.

## On-site order (fits the checklist)

1. `check` / `discover` — establish what is alive (5 min).
2. `channels` — which NVR channels have a picture (5 min).
3. `nvr` — deep NVR facts (5 min).
4. `events` — person/vehicle payloads (10 min, see `TEST-PLAN.md` 3c).
5. Fix cameras; then `perf` and `monitor` as the **before/after baseline** (30–60 min).
6. `report` — same evening, with the numbers from the box.
