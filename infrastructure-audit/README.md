# Infrastructure audit centre

This folder is the single place where camera, NVR and network measurements live. Raw data
is preserved per scan; processed datasets, reports, the dashboard and exports are generated
from it. Nothing here is collected by this folder itself — collection stays in
`adalya_audit.py`, and this centre organises what it measured.

It exists so that:
- the **technical view** (raw JSONL, hashes, per-probe results) and the
  **management view** (health score, reports, charts) are built from the **same** data;
- a number in a report can always be traced back to a file and a timestamp;
- previous scans are never overwritten.

## Layout

```text
infrastructure-audit/
├── README.md                  this file
├── raw-data/                  original collector output, copied per scan (never edited)
│   ├── cameras/               (reserved: direct camera responses; not collected today)
│   ├── nvr/                   nvr-<scan>.json from the Dahua API
│   ├── network-devices/       perf-<scan>.json
│   ├── switches/              (reserved: no switch login/SNMP available)
│   ├── discovery/             discover-<scan>.json + .csv
│   └── historical/<scan>/     monitoring JSONL + manifest.json (sizes, SHA-256)
├── processed-data/            derived, named datasets (one JSON each)
│   ├── device-inventory.json
│   ├── network-topology.json
│   ├── performance-metrics.json
│   ├── bandwidth-analysis.json
│   ├── health-summary.json
│   └── history.json           one summary row per scan
├── reports/                   six standalone HTML reports
├── dashboard/                 index.html + css/app.css + js/app.js + js/data.js
├── logs/                      discovery / monitoring / errors / system logs
├── exports/
│   ├── csv/                   inventory, camera bitrates, findings
│   ├── json/                  copy of every processed dataset
│   └── pdf/                   print-report.html (+ optional real PDF)
└── archive/<scan>/            previous processed snapshot before it is replaced
```

Generated content (`raw-data/`, `processed-data/`, `reports/`, `dashboard/`, `exports/`,
`logs/`, `archive/`) is **not committed**: it contains site details and changes every run.
Only this README lives in git; the layout is recreated by the builder.

## Pipeline

```text
adapta_audit.py discover|nvr|perf|monitor     ->  results/audit/*.json, *.jsonl
adalya_dashboard.py build                     ->  infrastructure-audit/*
```

`build` copies the newest collector files into `raw-data/<kind>/<scan-id>/` with a
manifest (size + SHA-256), then derives every dataset and page from **those copies**.

## Commands

```bat
:: 1. collect (read-only, asks the NVR password once)
python adalya_audit.py discover
python adalya_audit.py nvr --user admin
python adalya_audit.py perf --seconds 30
python adalya_audit.py monitor --minutes 30 --user admin

:: or all of it, plus the report and this folder
python adalya_audit.py full --user admin --monitor-min 30 --users 100

:: 2. build the centre
python adalya_dashboard.py build

:: 3. open it
start infrastructure-audit\dashboard\index.html

:: 4. live mode (rebuilds when new collector data appears)
python adalya_dashboard.py serve --port 8090
::    http://127.0.0.1:8090/dashboard/index.html
```

`serve` is a tiny stdlib HTTP server; it watches `results/audit` and rebuilds every 60 s
(`--every 0` disables). Without `serve`, the dashboard is a static snapshot and says so.

## What each dataset contains

| File | Built from | Key fields |
|---|---|---|
| `device-inventory.json` | discovery + NVR channels + perf + monitor | ip, mac, vendor, role, status, channel, response_ms, loss_pct, uptime_pct, stream (codec/res/fps/kbps), source |
| `network-topology.json` | inventory + NVR channel table | nodes, edges; **confirmed** = NVR channel, **inferred** = wiring guess, clearly labelled |
| `performance-metrics.json` | perf + monitor JSONL | loss, min/avg/max, jitter, TCP latencies, per-device time series |
| `bandwidth-analysis.json` | NVR encode config (+ optional measured stream) | per-channel kbps and GB/day, configured total, measured value if available |
| `health-summary.json` | findings engine | score/grade, alerts with severity + confidence + action, counts, capacity model, recommendations |
| `history.json` | one row per build | score, counts, total kbps, per-device uptime/loss snapshot |

### Severity and confidence

- **Severity:** critical / high / medium / info. Drives ordering and the health penalty.
- **Confidence:** `confirmed` = directly measured (NVR says a channel is offline, ICMP loss,
  disk percentage); `suspected` = inferred from a single sweep or from the design.
- Health score = 100 − weighted findings (confirmed full weight, suspected half). It is a
  summary, not a measurement, and the dashboard says so.

## Reports

| Report | Answers |
|---|---|
| `infrastructure-overview.html` | What is the overall state, what are the findings, how fresh is the data? |
| `camera-health-report.html` | Which cameras are online, how fast, how lossy, what stream settings? |
| `nvr-storage-report.html` | Recorder identity, channel health, disk, retention, log errors |
| `network-performance-report.html` | Latency, loss, jitter, topology |
| `bottleneck-analysis.html` | Ranked bottleneck candidates, evidence, confidence, what would confirm each |
| `scalability-assessment.html` | Can this support 100 users? Measured vs modelled, assumptions printed |

Every metric block follows the same seven questions: what was measured, what it means,
current result, healthy/concerning, likely cause, impact, recommended action — plus the
evidence path.

## Dashboard

- Opens from disk (`file://`) as a static snapshot: no server needed.
- Tabs: Overview, Cameras, NVR, Network, History, Reports, Data.
- Camera filters: all / online / offline / unstable / unknown + search.
- Charts are inline SVG: uptime bars, latency trends, health history, bandwidth history.
- Topology: confirmed channel edges solid, inferred wiring dashed; no invented links.
- History: compare any two scans; per-device uptime and loss deltas.
- Data tab: links to raw manifest and every export.
- Live mode: `serve` refreshes `live.json`; the page reloads when the scan id changes and
  shows "live" only when the build is younger than 2 minutes.

## Exports

- `exports/csv/device-inventory.csv`, `camera-bitrates.csv`, `findings.csv` — spreadsheets.
- `exports/json/*.json` — the same datasets for other tools.
- `exports/pdf/print-report.html` — open and **Print → Save as PDF**. `python adalya_dashboard.py export --pdf`
  tries Edge/Chrome headless to produce `infrastructure-<scan>.pdf`; if no browser is found,
  the print sheet is the fallback (no extra dependency).

## History and retention

- Every `build` gets a scan id (`YYYYmmdd-HHMMSS`) and copies raw inputs under
  `raw-data/historical/<scan>/`.
- The previous processed snapshot is copied to `archive/<old-scan>/` before being replaced.
- `history.json` keeps the last 100 scans; the dashboard can compare any two.
- Deleting old scans is a manual decision: remove `raw-data/*/<scan>/` and the matching
  `archive/<scan>/` if disk matters. Nothing auto-deletes.

## Interpreting the numbers

- `response_ms` — mean ICMP RTT from the last perf run; sub-millisecond values are 0.5.
- `loss_pct` — share of probes without a reply; ≥5 % is flagged.
- `uptime_pct` — share of successful monitor samples, only when `monitor` ran.
- `kbps` — the NVR's **configured** main-stream bitrate, not measured traffic. Measured
  values appear only when `perf --stream-ch N` ran.
- `response_ms`/`uptime` are unavailable when discovery/monitoring did not run: shown as
  "—", never invented.
- NVR CPU/RAM/SMART are not exposed by this firmware; they are marked unavailable.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard says "static snapshot" | Opened from disk; that is expected. Use `serve` for live refresh. |
| "No findings" and zeros | No collector data yet: run `adapta_audit.py` stages, then `build`. |
| Camera rows say "unknown" | Discovery saw the host but no NVR channel/ perf/monitor data matched it. |
| NVR tab empty | `nvr` stage not run, or the NVR login failed. |
| Build uses old data | `build` takes the newest files in `results/audit`; check timestamps in the Data tab. |
| PDF export fails | Headless browser unavailable/locked; open `exports/pdf/print-report.html` and print. |
| Duplicate scan ids | Two builds in the same second; rerun `build` a second later. |

## Security and privacy

- Read-only: no device is configured, reset or written to.
- The NVR password is typed interactively, held in memory, and never stored in any file
  here. Raw files contain network addresses, channel names and measurements — treat them
  as site-confidential; the folder is excluded from git.
- Camera streams and recordings are not copied into this folder.

## Extending

- New measurement: add a probe to `adapta_audit.py` or `audit_net.py`, write it to
  `results/audit`, then teach `audit_store.py` to ingest it (`mapping` in `ingest()`).
- New finding: add a rule to `audit_report.analyze()`; it flows into alerts, health, reports
  and the dashboard automatically.
- New report: add a page in `audit_store._write_reports()`.
- Database integration: `processed-data/*.json` map 1:1 to future SQLite tables
  (`audit_runs`, `devices`, `samples`, `findings`); the JSON key names are the column names.
