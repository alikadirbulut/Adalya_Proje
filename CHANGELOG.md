# Changelog

All notable changes to Adalya Paneli. Dates are 2026; the panel is a personal, local-only project.

## Audit toolkit — 21 November 2026
- **`adalya_audit.py`**: read-only infrastructure audit in five stages — `discover` (ICMP/ARP/port sweep compared with `devices.json`), `nvr` (~14 Dahua API probes: model, firmware, serial, network, per-channel encode/state, storage, logs), `perf` (loss, RTT, jitter, TCP/HTTP latency, optional measured sub-stream bitrate), `monitor` (JSONL history with NVR state/disk sampling) and `report` (self-contained HTML + JSON + inventory CSV).
- **Analysis, not just collection:** offline cameras, link errors in the NVR log, packet loss/high latency/jitter, disk pressure, duplicate MACs, wrong MACs, DHCP-pool devices, missing/unknown inventory, and a 100-user capacity model with printed assumptions (API vs video separated).
- Modules: `audit_net.py` (network probes, monitoring), `audit_nvr.py` (deep NVR read), `audit_report.py` (findings, capacity, HTML). It reuses `adapta_test.py` primitives and `nvr.py` instead of duplicating them.
- Safety: read-only, single NVR login attempt, password never written to disk, rate-limited probes, per-probe fault tolerance. Documented in `AUDIT.md`.

## 3.5 — 21 November 2026
- **NVR load governor.** On the real box the panel's snapshot polling made cameras drop off the NVR (its monitor showed "network disconnected"). Every snapshot makes the NVR open a fresh sub-stream to that camera; an 8 fps enlarged view, a 16-tile wall and a 2 s Vardiya rebuild were too much for the weak Wi-Fi cameras.
  - Server: **one snapshot in flight** at a time, at most **4 per second** in total (`--snap-rate`), a 0.25 s reuse cache, and a channel that answers 400/timeout is **benched** for 30 s → 2 min → 10 min instead of being retried on every refresh.
  - Live RTSP: default cap **2** simultaneous streams (`--live-max`); `-rw_timeout` stops a stalled pull from holding its slot.
  - Vardiya wall: no rebuild-everything-every-2-s; one tile per 600 ms, paused while the tab is hidden or the enlarged view is open, failed tiles skipped for 30 s.
  - Canlı izleme grid: channels that fail are skipped for 30 s on the client too.
  - `--demo` now resets `config.json` as well, so a second demo/test run starts from the same state.
- **`python adalya_test.py channels`**: asks the NVR for one channel at a time (2 s apart by default), prints the NVR's channel names and which channels really return a picture, and saves `results/channels-*.csv`. Also fixes the stale `.23` default host in the `nvr` command.
- **`python adalya_test.py events`**: listens to the NVR's smart-event push (motion, tripwire, human/vehicle) for a few minutes and saves the raw payloads (`results/events-*.jsonl`). This is the capture that decides the person/vehicle feature: if `ObjectType` says `Human`/`Vehicle`, the panel can show real object events.
- 210 API checks still pass, plus a governor self-test (serialisation, budget, cache, cool-down).

## 3.4 — 21 November 2026
- **Live RTSP video** in the enlarged camera view: the panel opens the NVR sub-stream (`/cam/realmonitor?channel=N&subtype=1`) with **ffmpeg** and streams MJPEG to the browser (8 fps, 960 px wide, ~1 s delay). Max **4 simultaneous** live streams; resident camera permissions are enforced; it falls back to snapshots if ffmpeg/NVR fails.
- NVR address corrected to **192.168.0.254** (DHI-NVR4232-4KS3, 32 channels; `.23` is the old address). Live video needs ffmpeg installed on the panel PC; snapshots work without it.
- 210 API checks.

## 3.3 — 21 November 2026
- **Camera frames doubled** (not more): enlarged view up to **8 snapshots/s** (NVR-limited, one request at a time), grid scheduler ~2.8 requests/s with 2 in flight, Vardiya wall every 2 s and its enlarged view every 450 ms.
- Still near-live snapshots, not true video; the measured rate is shown next to the camera name.

## 3.2 — 19 November 2026
- **Proje → Nasıl kullanılır** (`#/nasil`, old `#/proje` redirects).
- **45-second onboarding video** (`media/onboarding.mp4`, 1280×720, silent) served with HTTP Range support, plus poster.
- Page rewritten for residents: why the panel matters, first-five-minutes steps, role cards, Esin Noğan's profile with biography and Ali's note; technical troubleshooting story removed.
- Memorial card re-laid out for desktop and phone.
- 209 API checks.

## 3.1 — 19 November 2026
- **Black-and-white photo logo** as app icons: 192/512, maskable 512, Apple touch, favicon, login, sidebar, `/kayit`.
- **Mobile shell for manager and Güvenlik**: fixed top bar with logo and bell, off-canvas menu with backdrop, full-width forms, 16 px inputs, safe-area padding, 44 px targets.
- iOS standalone meta tags (`apple-mobile-web-app-*`), manifest id/theme, service-worker cache `adalya-v4`.

## 3.0 — 19 November 2026
- **Request conversations**: `request_messages`, resident follow-ups, management replies, **Açık / İşlemde / Kapalı**; closed requests stop accepting replies.
- **Notification centre**: bell, phone tab and badge; new announcements and **Mesaj gönder → Uygulama bildirimi** with ticked recipients.
- Residents can **add/edit/remove up to 2 vehicles** in Dairem; admins stay unlimited.
- **Esin Noğan biography** on Proje and in Ayarlar (18 Haziran 2026, 04:30).
- `/demo?role=…&to=…` role links for testing.

## 2.9 — 19 November 2026
- Vardiya camera wall selectable **4 / 6 / 8 / 10**, click a tile for a large live view.
- Menu group headers aggregate their sub-item notifications, even collapsed.

## 2.8 — 18 November 2026
- Daily-Turkish menu (Genel, Kamera ve güvenlik, Site sakinleri, Ödemeler, Site işleri, Sistem, Bilgi).
- Public **`/kayit`** registration with admin approval, 14-day invite links, max 2 accounts per flat, 2 devices per account.
- **Kullanıcılar ve cihazlar**, per-flat page, vardiya quick guest entry; arsa payı hidden from screens.
- 187 API checks.

## 2.7 — 18 November 2026
- Reminder queue with automatic monthly preparation and send log.
- Login throttle per user+IP and per IP, TOTP two-step login, `Secure` cookies behind HTTPS.
- PWA manifest, SVG icon and service worker.

## 2.0–2.6 — 18 November 2026
- UI refresh: task-first **Bugün**, **Vardiya ekranı**, phone-first **Dairem**, daily-Turkish labels.
- Phase C: KMK calendar, contracts/policies, document archive.
- Announcements with read receipts, guest passes, parcels, light/dark theme, highlighted menu.
- Request categories/photos, global search, audit filters/CSV, daily backups with restore.
- Receipts, payment link, bank statement import.
- İşletme projesi (KMK 37) and gelir-gider.
- 175 API checks.

## 1.0 — 18 November 2026
- First working panel: monitoring, drop history, map, recordings, logs, exports, download log, maintenance log, accounts, memorial, demo mode, site test toolkit.

