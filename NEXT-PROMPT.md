# Next session prompt: Adalya Paneli + Adalya CCTV troubleshooting

Copy everything below the line into a new session. Replace `<FOLDER>` with where the `adalya-toolkit` folder lives (on my PC it is in `OneDrive\Masaüstü\Esin`). Open the session **in that folder**, not in a temporary one.

*State as of 18 November 2026, Phase C included (calendar, contracts, documents, UI refresh).*

---

I'm continuing two connected pieces of work for **Adalya Sitesi** (a residential complex in Turkey, about 90 × 90 m, three blocks: A, B, C). The project folder is `<FOLDER>/adalya-toolkit` (a git repo, branch `main`). Please read `README.md`, `ON-SITE-CHECKLIST.md`, `INSTALL.md` and `TEST-PLAN.md` there first.

1. **Network/CCTV troubleshooting** on site. We took the system over after the previous MSP stopped supporting it. Still in progress; the next site visit follows `ON-SITE-CHECKLIST.md`.
2. **Adalya Paneli**, a local web panel (Python standard library only, Turkish UI) to monitor the cameras and manage the site (residents, aidat, messages), **dedicated to the memory of Esin Noğan** (13 Ocak 1995 – 18 Ocak 2026, okul öncesi öğretmeni), built by **Ali Kadir Bulut** (me).

## Rules for this session
- Explain to me in English; the panel UI stays in Turkish.
- The repo is on GitHub (private): https://github.com/alikadirbulut/Adalya_Proje. **Commit when a piece of work is done, but don't push until I say so.** Personal project, all rights reserved (see `LICENSE`); keep it that way unless I decide otherwise.
- When I bring results back from the site, use them to update the troubleshooting status below, `devices.json` and the checklist.
- Nothing in the panel may change devices (router, NVR, cameras): read-only towards devices. The device inventory only edits the panel's own list.
- Never commit `data/`, `data-demo/` or `results/` (accounts, residents, aidat, history, download log). Never put passwords, camera safety codes or IBANs of real people in files.
- **KVKK:** recordings, logs, the residents registry, visitor log and aidat records are personal data. Role-based access enforced on the server, data minimisation for Güvenlik, every export/lookup/change logged. Residents may watch **only the live picture** of cameras the manager allowed for their flat, **never recordings**.
- Legal features (KMK, genel kurul, gecikme tazminatı) are tools that help the manager, **not legal advice**; the yönetim planı and a lawyer have the final word. Say so in the UI.
- One command must keep working on a fresh download: `python adalya_panel.py --demo` (or double-click `demo.bat`). Standard library only, Python 3.9+; no f-string tricks that need 3.12.

## What the panel does today (all built and tested in demo)
- **Roles:** Yönetici (everything), Güvenlik (was "Görevli": cameras, recordings, logs, residents' names/phones/vehicles read-only, plate lookup, visitor log), Site sakini (own flat only). Demo has a role selector at the bottom of the menu.
- **Menu:** eight collapsible groups — Sitem (residents), İzleme (Bugün, Vardiya ekranı, Canlı izleme, Kameralar, Site haritası), Kayıt ve kanıt, Sakinler, İletişim (Duyurular), Yönetim (Takvim, Sözleşmeler, Belgeler, Bakım), Teknik (Ağ cihazları, Yedekler, NVR arayüzü, Ayarlar), Hakkında; state remembered per browser, active section header highlighted. Residents get Dairem + bottom tabs (Dairem, Kameralar/Harita if allowed, Proje) and Proje. **Ara (⌘K/Ctrl+K)** opens global search for admin/staff.
- **UI (18 Nov refresh):** new **Bugün** home (greeting, 4 counters, "Şimdi ilgilenmeniz gerekenler" with one action per row, agenda, 4 quick actions), **Vardiya ekranı** (plate search + 6 live snapshots + today's visitors), phone-first **Dairem** (large balance card, tiles, bottom tabs), design tokens (15 px base, 14 px cards, 44 px targets, plain-language status chips), technical pages moved under Teknik, **manual light/dark theme** (bottom of the menu, follows the system until chosen, `?theme=dark` link for testing). Version 2.3.
- **İzleme:** Canlı izleme (8/16/32 tiles with paging, full screen, snapshot scheduler ≈1.4 req/s, max 2 in flight; enlarged view refreshes continuously up to 4 fps and shows the frame rate), Kameralar, Site haritası (type icons with status colours, readable name pills that avoid overlaps, device card, drag-to-place in edit mode), Ağ cihazları (+ **device inventory editor**: add/edit/remove cameras and devices with the correct order: MAC → router reservation below .150 → NVR channel → panel → map).
- **Kayıt ve kanıt:** Kayıtlar (search/download from NVR), Olaylar ve loglar, Dışa aktar (zip, audit log).
- **Sakinler:** Daireler ve kişiler (flats, people malik/kiracı/hane, vehicles; Excel .xlsx/CSV import with preview; CSV export; aidat column with one-click **Hatırlat** via WhatsApp/e-mail; checkboxes + bulk actions), Aidat ve tahsilat (monthly charges equal or by arsa payı, payments close oldest debt first, overdue, estimated KMK 20 late fee, ledger per flat, bulk paid/charge, IBAN settings, CSV), Ziyaretçi ve araç (plate lookup, visitor log), Mesaj gönder (WhatsApp click-to-send, e-mail, printable letters + signature list; templates incl. aidat reminder, second notice, monthly notice, thank-you, details confirmation, genel kurul invitation; recipients incl. overdue / selected flats), Talepler (resident requests), **Sakin kamera erişimi** (master switch, off by default; defaults "Tüm sakinler" + per blok; per-flat override in the flat editor; residents get live picture only, first daily view per camera logged).
- **Dairem (resident):** own flat, household, vehicles, aidat balance/overdue/history and IBAN, requests to management with answers, password, links to their cameras.
- **Yönetim (Phase C):** Takvim ve görevler (6 yearly + 12 monthly KMK duties, tickable per year, own tasks + templates; page says it is not legal advice), Sözleşmeler ve poliçeler (valid / 30 gün içinde bitiyor / süresi geçti, linked document), Belgeler (upload/download/delete archive in `data/belgeler/`). Admin only; every change and download logged. Admin alert cards on Bugün.
- **Resident services (Phase 1 of the 14-feature roadmap):** **Duyurular** (publish/pin/delete; residents mark Okudum; admin sees read count and reader names; unread badge), **guest passes** (resident creates name/plate/date/time window; Vardiya lists today's passes with "Giriş"; used passes close), **parcel log** (guard logs courier + flat, "Teslim", resident sees it in Dairem).
- **Phase 2:** request **categories + photos** (JPG/PNG/WEBP ≤5 MB, photo visible to admin and owner, blocked for staff), **global search** (⌘K: people, phones, plates, flats, documents, contracts, announcements), **audit filters + CSV**, **daily automatic backups** in `data/yedekler/` with download and in-place restore (typed GERI YUKLE; safety backup first; DB restored via SQLite backup API).
- **Security basics:** personal accounts, PBKDF2, 12 h sessions, login throttle, Origin check on POST, NVR password never saved (one attempt, then stops), audit log for downloads, exports, imports, plate lookups, device and aidat changes, messages, announcements, guest passes and parcels.

## Next tasks (suggested order; ask me before starting a big one)

### 1. Bring back the site results (when I have them)
Update `devices.json`, the troubleshooting status below, and the checklist from what I bring back. Then go through `ON-SITE-CHECKLIST.md` step 2.3: every NVR feature of the panel has only been tested against a **simulated** Dahua NVR. Fix whatever the real NVR answers differently (snapshots, record search/download, logs, disk info).

### 2. True live video (after checklist step 2.4 results)
Snapshots are near-live only. Plan: a small media server (**go2rtc**, single exe, or MediaMTX) on the panel PC pulls each camera's **RTSP sub stream** from the NVR (`rtsp://<user>:<pass>@192.168.0.23:554/cam/realmonitor?channel=N&subtype=1`) **once**, however many people watch, and serves it to the browser (WebRTC/MSE, under 1 s delay). It must listen on localhost only; browsers reach it **through the panel**, so roles and resident camera access still apply (needs a WebSocket/HTTP proxy in the panel, standard library). Needs the sub stream in **H.264** (H.265 is unreliable in browsers; keep the main stream H.265 for recording) and a separate **view-only NVR user** whose login go2rtc stores (on the panel PC only, never in git). Fallback if it suits: the NVR's MJPEG stream (`/cgi-bin/mjpg/video.cgi?channel=N&subtype=1`) proxied by the panel. Keep the snapshot view as the fallback when video fails.

### 3. Remote access (decision still open; I haven't chosen yet)
My recommendation so far: **managers/security** reach the panel from outside through a **VPN** (WireGuard or Tailscale), nothing port-forwarded. **Residents** get a separate **"portal mode"** (`--portal`) whose server has no camera/NVR/device routes at all, published with HTTPS (Cloudflare Tunnel needs no static IP or open port; or a small server). Resident camera viewing from home would then need the VPN or a later, separately hardened video path. Before anything faces the internet: login throttle **per user and IP** (today it is global, so one attacker could lock everyone out), `Secure` cookies, two-step login for Yönetici, password reset, backups, rate limits, KVKK aydınlatma metni. "Install as an app" = PWA (manifest + service worker), needs HTTPS. I mentioned a static IP and a domain; I still need to decide.

### 4. Roadmap phases still to build
| Phase | What | State |
|---|---|---|
| A | Roles, flats/people/vehicles, import, plate lookup, visitor log | **Done** |
| B | Send to residents (WhatsApp, e-mail, letters + signature list) | **Done** |
| E (part) | Aidat, payments, overdue, late fee, reminders | **Done** |
| C | Yearly calendar with legal deadlines and alerts (see KMK list below), contracts and insurance policies with expiry dates, document archive | **Done** (18 Nov) |
| D | Meetings and decision book: agenda, invitation letters (exists in Mesaj gönder), signature list, proxies, quorum by arsa payı and headcount, minutes; notarised paper book stays official, the panel prints the working copy | Later |
| E (rest) | Income/expense beyond aidat, işletme projesi (KMK 37, yearly estimate → advance per flat; 7-day objection), yearly account report (KMK 39), receipts (makbuz) for payments | Later; ask whether the site already uses accounting software |
| F | Online resident portal (see task 3) | Depends on the remote-access decision |

**Site manager legal duties to turn into features** (Kat Mülkiyeti Kanunu 35–39):
- KMK 35: carry out kurul decisions; maintenance, repair and protection of common areas; insurance; collect advances; pay debts; receive notifications; protect deadlines and rights; enforcement and kanuni ipotek against non-payers; bank account in the site's name; call the kurul.
- KMK 36: decision/operating book in date order, keep all documents; **book closed at the notary within one month after year end (by end of January)**.
- KMK 37: işletme projesi notified to owners; final if not objected to within 7 days.
- KMK 38: manager liable like a proxy (vekil); ibra doesn't cover irregularities found later.
- KMK 39: present accounts to the kurul in January (or per yönetim planı), or when half of the owners ask.
- Practical list: ordinary general assembly in January, invitation ≥15 days before; enforce yönetim planı rules; elevator monthly maintenance, yearly A-type inspection, red label = stop use; roof, facade, sewage, booster pump, generator, heating; cleaning and landscaping; staff SGK, salaries, leave, severance; İSG (6331): risk assessment, safety specialist, workplace doctor; enforcement, kanuni ipotek, müdahalenin men'i.

### 5. Known gaps and small fixes
- Login throttle is global; make it per user/IP (needed before any remote access).
- Resident accounts are created one by one in Ayarlar; consider bulk creation or invitation codes per flat.
- Import preview counts duplicates of the same new person/plate within one file twice (flats are already handled).
- No payment receipts (makbuz) yet; aidat late fee is only calculated, never charged automatically (on purpose).
- Map block labels are empty (put back A/B/C blok, Bahçe, Havuz, Otopark on the rotated render if I agree); `site.json` rotation is **45°** (earlier 34°): check it looks right on site. Camera map positions are guesses until the site walk.
- `NEXT-PROMPT.md` itself: update it at the end of each session.

## Open questions for me
- How many flats and residents; is there an Excel list (then import it)?
- Real aidat amount and method (equal or by arsa payı), due day, the site's bank account name and IBAN.
- Which cameras are shared areas for all residents (main gate, main street, parking entrance)? Should resident camera access be switched on at all (ideally a kurul decision + aydınlatma metni)?
- Remote access: VPN + separate resident portal, or something else? Static IP/domain or Cloudflare Tunnel?
- Does the site already use accounting software?
- Access point: buy the IP-Com iUAP-AC-M or not (decide from the 1.7/1.8 measurements)?

## Project details

### Site network (as found and partly fixed)
- **Router:** TP-Link Aginet **EX520v** (ISP-class), `192.168.0.1`, DHCP server and the only main 2.4 GHz radio (channel 11). No public local API; the ISP can manage/reset it remotely (TAUC/TR-069). If it's reset, the DHCP reservations disappear → back up its config.
- **DHCP pool:** changed from `.10–.200` to **`.150–.200`**. Cameras and infrastructure get fixed addresses below `.150` via **Address Reservation** (MAC → IP).
- **Switch:** Ruijie Reyee **RG-ES208GC**, 8 ports (6 used), no PoE, no SNMP, at `192.168.0.16`. Web login unknown (label default didn't work); reset only in a maintenance window.
- **NVR:** Dahua **DHI-NVR4232-4KS3** (32 channels), `192.168.0.23` (also answers on `.254`), MAC `E0:2E:FE:36:12:69`. Clock was wrong; HDD/recording not yet verified.
- **Cameras:** Dahua **DH-P3B-PV** (Wi-Fi pan/tilt, 12V adapters), MACs `E0:2E:FE:67:…`. Mixed passwords: newer firmware (`.26.R`, e.g. `.100`, `.12`) take the NVR password; older (`.24.R`) use an installer password. Wrong attempts lock a camera for 5–30 min.
- **Repeaters:** 2× Tenda **T10** (one at `192.168.0.10`, MAC `B4:0F:3B:45:9C:F8/F9`) and TP-Link **TL-WA855RE** (`192.168.0.17`). All 2.4 GHz consumer repeaters.
- **Monitor:** Hikvision screen on the NVR's HDMI. UPS under the desk.

### Root causes found
1. DHCP churn: cameras on temporary leases while the NVR expected fixed IPs → working cameras "disappeared".
2. Old subnet: some cameras still static on `192.168.1.x`.
3. Weak Wi-Fi: far cameras on one 2.4 GHz radio and consumer repeaters → 20–40 s dropouts (Wi-Fi reconnect + NVR retry).

### Troubleshooting status (still open; steps are in ON-SITE-CHECKLIST.md)
- [ ] Finish MAC → IP reservations for all cameras (one was wrongly pinned to `.151`; must be `.12` for `E0:2E:FE:67:BB:B3`).
- [ ] Recover the 2 cameras static on **`192.168.1.128`** (A Blok asansör, MAC `…BC:63`) and **`192.168.1.129`** (A Blok otopark, MAC `…B9:F1`) → `192.168.0.128/.129`, then NVR channels 17/18.
- [ ] NVR channel list cleanup: channel 16 → `192.168.1.104` (A Blok otopark 1, not seen anywhere), channel 19 duplicate of `.102` → delete.
- [ ] About **9 cameras not on the network** (incl. Batı çevre 1, B Blok otopark, C Blok asansör MAC `E0:2E:FE:87:8C:68`, B Blok asansör, undocumented ones) → site walk. Add each one that comes back through **Ağ cihazları → Cihaz envanteri**.
- [ ] Weak signal: **Güney çevre 1** (`.106`, RSSI 18), **Güney çevre 2** (`.108`, RSSI 25), **Cblok** (`.12`, RSSI 28); Tenda uplink RSSI 34. Tune (H.265 main 1–1.5 Mbps, lower FPS, repeaters on their own SSID and channel 1/6), measure before/after with `python adalya_test.py watch --only 106,108,12`.
- [ ] NVR: clock/NTP, HDD and recording on all channels, BPS screenshot.
- [ ] Router: ISP name, firmware, SNMP/remote management; config backup.
- [ ] Live-video tests (checklist 2.4): RTSP sub stream in VLC, codecs per channel, MJPEG URL, RTSP port.
- [ ] Change default passwords (switch etc.) at the end.
- [ ] Elevator cameras need a dedicated elevator bridge kit or travelling cable.

### Access point decision (NOT decided yet)
- Candidate: **IP-Com iUAP-AC-M** outdoor AP (~4,470 TL): IP65, dual band, 802.3af/at or 24V passive PoE (needs an injector, the switch has no PoE), adjustable TX power, RSSI threshold, up to 8 SSIDs with VLAN, SNMP. Legal 2.4 GHz power ≈20 dBm EIRP, so realistic 40–60 m to the cameras with line of sight.
- **Trap:** factory IP **`192.168.0.254`** conflicts with the NVR's second address. Configure it on a laptop first, move it to `192.168.0.13`.
- If bought: one AP on cable for the south side, same SSID/password as the cameras, channel 1 or 6, 20 MHz, medium power; measure before/after; a second only if the numbers justify it; then remove repeaters. Alternatives: 5 GHz point-to-point bridges, Cat6 + PoE (runs < 100 m), long term business PoE cameras.

### Files
- `adalya_panel.py` server (routes, monitoring, registry, aidat, camera access), `panel.html` UI (single page, inline SVG icons, no external libraries; works offline), `nvr.py` Dahua client + demo NVR, `adalya_test.py` site toolkit, `devices.json` (devices, NVR channels, map positions), `site.json` (site name, memorial, map labels, map rotation), `media/` (memorial photo), `site-plan.webp` (3D render; rotated in the browser, white background removed, rotation adjustable in map edit mode), `start-panel.bat`, `tests/api_smoke.py`, docs (`README.md`, `INSTALL.md`, `TEST-PLAN.md`, `ON-SITE-CHECKLIST.md`, this file), `docs/` (README images, old Google aerial).
- Data (never committed): `data/panel.db` (SQLite: events, notes, audit, flats, people, vehicles, visitors, requests + category/photo, charges, payments, settings, flat_cams, calendar_tasks, duty_done, contracts, documents, announcements, announcement_reads, guest_passes, packages), `data/belgeler/` (documents), `data/talepler/` (request photos), `data/yedekler/` (backups), `data/config.json` (users with PBKDF2 hashes, NVR host/user).
- Deployment plan: one always-on PC at `192.168.0.30` (DHCP reservation) running `start-panel.bat` (`--lan`); everyone uses a browser.

### Testing
- Demo: `python adalya_panel.py --demo` or `demo.bat` → `http://127.0.0.1:8080` (next free port if busy; no login, simulated NVR, fictional residents and six months of aidat, role selector in the menu). It works on copies of `devices.json`/`site.json` in `data-demo/`, recreated at each start, so the real files stay untouched.
- API tests (129 checks: roles and KVKK limits, import CSV/XLSX, plate lookup, visitors, devices, aidat math, resident camera access, calendar/duties/tasks, contracts, document upload/download/delete, announcements/read receipts, guest passes, parcels, request categories/photos, search, audit filters/CSV, backups and restore, audit): start a fresh demo with `--no-browser --port 8091`, then `python tests/api_smoke.py http://127.0.0.1:8091`.
- Python compatibility: on Python 3.12+ the tokenizer can find f-strings that older Pythons reject (backslash or reused quote inside `{}`); keep the code 3.9-compatible.
- After big HTML edits: extract the `<script>` from `panel.html` and run `node --check` on it; `python -m py_compile adalya_panel.py`. Then check the pages in the browser (use a separate tab if I'm clicking around in the preview myself).
