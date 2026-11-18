# Next session prompt: Adalya Paneli + Adalya CCTV troubleshooting

Copy everything below the line into a new session. Replace `<FOLDER>` with where the `adalya-toolkit` folder lives.

---

I'm continuing two connected pieces of work for **Adalya Sitesi** (a residential complex in Turkey, about 90 × 90 m, three blocks: A, B, C). The project folder is `<FOLDER>/adalya-toolkit` (a git repo). Please read `README.md`, `INSTALL.md` and `TEST-PLAN.md` there first.

1. **Network/CCTV troubleshooting** on site. We took the system over after the previous MSP stopped supporting it. This is still in progress.
2. **Adalya Paneli**, a local web panel (Python standard library only, Turkish UI) we built to monitor and manage the cameras and the site, **dedicated to the memory of Esin Noğan** (13 Ocak 1995 – 18 Ocak 2026, okul öncesi öğretmeni), built by **Ali Kadir Bulut** (me).

## Rules for this session
- Explain to me in English; the panel UI stays in Turkish.
- The repo is on GitHub (private): https://github.com/alikadirbulut/Adalya_Proje. **Don't push** until I say so. It's a personal project, all rights reserved (see `LICENSE`); keep it that way unless I decide otherwise.
- The on-site plan is in `ON-SITE-CHECKLIST.md`; when I bring results back, use them to update the troubleshooting status below and `devices.json`.
- Nothing in the panel may change devices (router, NVR, cameras): read-only towards devices.
- Never commit `data/`, `data-demo/` or `results/` (accounts, history, download log). Never put passwords or camera safety codes in files.
- Recordings and resident data are personal data under **KVKK**: role-based access, residents never see cameras/recordings, every export logged.

## First tasks (do these first, in this order)

### 1. Canlı izleme: grid size option (8 / 16 / 32 views)
The site has about 24 or more cameras. Add a view selector on the Canlı izleme page: **8, 16 or 32 tiles** per screen, with paging when there are more cameras than tiles (e.g. 24 cameras at 16 → 2 pages). Remember the choice per browser (localStorage, wrapped in try/catch). The grid should fill the screen (good for the security room monitor), and the existing "click a tile to enlarge" must keep working. Keep the snapshot refresh load reasonable at 32 tiles: stagger requests, don't hammer the NVR.

### 2. Site haritası: device icons by type
Right now every device is a dot (cameras) or a square (others). Use **distinct icons per type**: camera, access point, router, switch, NVR, repeater. Keep the status colour (green working, amber dropped today, red down, grey unknown) on the icon. Update the legend. Icons must be inline SVG (no external icon libraries; the panel works offline).

### 3. Site haritası: labels unreadable when a device is clicked
When you click a device on the map, its name label is hard or impossible to read: it overlaps the aerial image and other labels. Fix it so the selected device shows a clear label, for example a small card/tooltip with a solid background, name, IP, channel and status, placed so it stays inside the map. The same applies to the "Cihaz adları" toggle (all names on): make names readable over the photo (background pill or halo) and avoid them piling on top of each other where possible.

After each fix: run the demo (`python adalya_panel.py --demo`), check the page in the browser, and run the existing tests (see "Testing" below).

## Then: brainstormed roadmap (discuss with me before building)

**New roles:** Site yöneticisi (everything), Güvenlik (cameras, recordings, plate lookup, visitor log, needed resident contact info), Site sakini (own flat/household/vehicles, dues, announcements, requests, meetings; **no cameras**). Optional: Denetçi (read-only finance and decision book), Teknik personel. Today's roles are `admin` → Yönetici and `staff` (Görevli) → becomes Güvenlik.

**Phases:**
| Phase | What |
|---|---|
| A | New roles; registry of flats (blok, kapı, daire, arsa payı, malik/kiracı), people (name, phone, e-mail, role, move-in/out, emergency contact) and vehicles (plate, make/model/colour, parking spot, sticker no); Excel/CSV import; plate lookup for güvenlik; visitor log |
| B | "Send to residents" button: WhatsApp click-to-send (wa.me with prefilled text), e-mail, printable letters with signature list (legally required for genel kurul invitations: imza karşılığı or taahhütlü, at least 15 days before); "confirm your registered details/vehicles" message |
| C | Yearly calendar with legal deadlines and alerts (see below), contracts and insurance policies with expiry dates, document archive |
| D | Meetings and decision book: agenda, invitation letters, signature list, proxies, quorum by arsa payı and headcount, minutes (the notarized paper book stays official; the panel is the working copy and prints it) |
| E | Dues and finance: dues per flat by arsa payı, payments, 5% monthly late fee, debtor list, income/expense, yearly account report (KMK 39). Decide: in the panel or connect to existing accounting |
| F | Online resident portal. Needs hosting with HTTPS; must be **separate** from the camera system, which stays LAN-only |

**Site manager legal duties to turn into features** (Kat Mülkiyeti Kanunu 35–39):
- KMK 35: carry out kurul decisions; maintenance, repair and protection of common areas; insurance; collect advances; pay debts; receive notifications; protect deadlines and rights; enforcement and kanuni ipotek against non-payers; bank account in the site's name; call the kurul.
- KMK 36: decision/operating book in date order, keep all documents; **book closed at the notary within one month after year end (by end of January)**.
- KMK 37: işletme projesi (yearly income/expense estimate, share per flat by arsa payı, advance amount), notified to owners; becomes final if not objected to within 7 days.
- KMK 38: manager is liable like a proxy (vekil) for damage from intent or negligence; release (ibra) doesn't cover irregularities found later.
- KMK 39: present accounts to the kurul in January (or per yönetim planı), or when half of the owners ask.
- Practical list: ordinary general assembly in January, extraordinary when needed, invitation ≥15 days before; enforce yönetim planı rules (common areas, noise, pets, parking); elevator monthly maintenance, yearly A-type inspection, red label = stop use; roof, facade, sewage, booster pump, generator, heating; cleaning and landscaping; staff SGK, salaries, leave, severance fund; İSG (6331): risk assessment, safety specialist, workplace doctor; enforcement, kanuni ipotek, müdahalenin men'i cases.
- Present these as tools that help the manager; not legal advice (yönetim planı and a lawyer have the final word).

**Proposed menu:** Yönetici: Genel bakış · Güvenlik (Canlı izleme, Kameralar, Kayıtlar, Olaylar, Ziyaretçi ve araç, Harita) · Sakinler (Daireler, Kişiler, Araçlar, Mesaj gönder) · Yönetim (Takvim ve görevler, Toplantılar ve kararlar, Aidat ve tahsilat, Gelir-gider, Bakım ve sözleşmeler, Personel, Hukuki takip, Belgeler) · Sistem (Ağ, NVR, Bakım günlüğü, Dışa aktar, Kullanıcılar, Ayarlar) · Proje. Güvenlik and Site sakini get reduced menus.

**Open questions for me:** how many flats/residents and is there an Excel list; should residents use it from home (online portal) or manager/security only for now; does the site already have accounting software.

## Project details

### Site network (as found and partly fixed)
- **Router:** TP-Link Aginet **EX520v** (ISP-class), `192.168.0.1`, DHCP server and the only main 2.4 GHz radio (channel 11). No public local API; the ISP can manage/reset it remotely (TAUC/TR-069). If it's reset, the DHCP reservations disappear → back up its config.
- **DHCP pool:** changed from `.10–.200` to **`.150–.200`**. Cameras and infrastructure get fixed addresses below `.150` via **Address Reservation** (MAC → IP).
- **Switch:** Ruijie Reyee **RG-ES208GC**, 8 ports (6 used), no PoE, no SNMP, at `192.168.0.16`. Web login unknown (label default didn't work); reset only in a maintenance window.
- **NVR:** Dahua **DHI-NVR4232-4KS3** (32 channels), `192.168.0.23` (also answers on `.254`), MAC `E0:2E:FE:36:12:69`. Clock was wrong; HDD/recording not yet verified.
- **Cameras:** Dahua **DH-P3B-PV** (Wi-Fi pan/tilt, 12V adapters), MACs `E0:2E:FE:67:…`. Mixed passwords: newer firmware (`.26.R`, e.g. `.100`, `.12`) take the NVR password; older (`.24.R`) use an installer password. Wrong attempts lock a camera for 5–30 min.
- **Repeaters:** 2× Tenda **T10** (N300 extender, one at `192.168.0.10`, MAC `B4:0F:3B:45:9C:F8/F9`) and TP-Link **TL-WA855RE** (`192.168.0.17`). All 2.4 GHz consumer repeaters.
- **Monitor:** Hikvision screen on the NVR's HDMI. UPS under the desk.

### Root causes found
1. DHCP churn: cameras on temporary leases while the NVR expected fixed IPs → working cameras "disappeared".
2. Old subnet: some cameras still static on `192.168.1.x`.
3. Weak Wi-Fi: far cameras on one 2.4 GHz radio and consumer repeaters → 20–40 s dropouts (Wi-Fi reconnect + NVR retry).

### Troubleshooting status (still open)
- [ ] Finish MAC → IP reservations for all cameras (one was wrongly pinned to `.151`; must be `.12` for `E0:2E:FE:67:BB:B3`).
- [ ] Recover the 2 cameras static on **`192.168.1.128`** (A Blok asansör, MAC `…BC:63`) and **`192.168.1.129`** (A Blok otopark, MAC `…B9:F1`). ConfigTool sees them; `.128` refused the password. Option: laptop with a second IP `192.168.1.50`, one password attempt each (own safety code, then installer password), else physical reset. Target IPs `192.168.0.128/.129`, then update NVR channels 17/18.
- [ ] NVR channel list cleanup: channel 16 points at `192.168.1.104` (A Blok otopark 1, not seen anywhere), channel 19 is a duplicate of `.102` → delete.
- [ ] About **9 cameras not on the network at all** (incl. Batı çevre 1, B Blok otopark, C Blok asansör MAC `E0:2E:FE:87:8C:68`, B Blok asansör, and undocumented ones) → site walk: power, Wi-Fi link, label info.
- [ ] Weak signal: **Güney çevre 1** (`.106`, RSSI 18, 51 Mbps), **Güney çevre 2** (`.108`, RSSI 25), **Cblok** (`.12`, RSSI 28); Tenda uplink RSSI 34. Short-term: H.265, 1–1.5 Mbps main stream, lower FPS; give repeaters a different SSID from the router; separate channels 1/6/11.
- [ ] Measure: NVR Info → BPS screenshot; `python adalya_test.py watch --only 106,108,12` for 30+ min to log drops.
- [ ] NVR: set clock/NTP, verify HDD and recording on all channels.
- [ ] Router: ISP name, firmware, SNMP/remote management settings; config backup.
- [ ] Change default passwords (switch etc.) at the end.
- [ ] Elevator cameras need a dedicated elevator bridge kit or travelling cable (moving metal cabin).

### Access point decision (NOT decided yet; I may or may not buy it)
- Candidate: **IP-Com iUAP-AC-M** outdoor AP (~4,470 TL): IP65, dual band, 802.3af/at or 24V passive PoE (needs an injector, the switch has no PoE), adjustable TX power (1 dB), RSSI threshold, up to 8 SSIDs with VLAN, frequency analysis, SNMP in its user guide. Max legal 2.4 GHz power in TR/EU is about 20 dBm EIRP, so "200 m" won't happen; realistic 40–60 m to the cameras with line of sight.
- **Trap:** factory IP **`192.168.0.254`** conflicts with the NVR's second address. Configure it on a laptop first, move it to `192.168.0.13`.
- Plan if bought: one AP on cable for the south side (Güney cameras), same SSID/password as the cameras, channel 1 or 6, 20 MHz, medium power; measure before/after with the watch log; buy a second only if the numbers justify it; then remove repeaters. One AP fixes one area, not the whole site (rule of thumb 8–12 cameras per 2.4 GHz AP).
- Alternatives discussed: point-to-point 5 GHz bridges where cable is impossible, Cat6 + PoE to cameras (site is small enough for <100 m runs), long term business PoE cameras.

### The panel (what exists)
- `adalya_panel.py` server, `panel.html` UI (sidebar menu), `nvr.py` Dahua client (+ demo NVR), `adalya_test.py` site toolkit, `devices.json` (devices, NVR channels, map positions), `site.json` (site name, memorial, map labels, map rotation), `media/memorial.jpg`, `site-plan.webp` (3D render of the site; rotated 34° in the browser so B/C blok are at the top, white background removed automatically, rotation adjustable in map edit mode), `start-panel.bat`, `INSTALL.md`, `TEST-PLAN.md`, `README.md`, `docs/` (README images, old Google aerial).
- Pages: Genel bakış, Canlı izleme, Kameralar, Site haritası, Ağ cihazları, Kayıtlar, Olaylar ve loglar, Dışa aktar, Bakım günlüğü, NVR arayüzü, Ayarlar, Proje (memorial with photo).
- Accounts: personal username/password, roles `admin` (Yönetici) and `staff` (Görevli), PBKDF2 hashes, 12 h sessions, login throttle, download/export audit log, Origin check on POST. NVR password never saved; one login attempt per call, client locks itself on refusal.
- Deployment plan: one always-on PC at `192.168.0.30` (DHCP reservation) running `start-panel.bat` (`--lan`); everyone uses a browser. No port-forwarding; VPN only if remote access is needed.
- **Known state to fix or check:** map block labels were cleared from Ayarlar (put back C blok, B blok, A blok, Bahçe, Havuz, Otopark on the rotated render if I agree); camera map positions are guesses; nothing tested against the real NVR yet (TEST-PLAN step 7).

### Testing
- Demo: `python adalya_panel.py --demo` → `http://127.0.0.1:8080` (no login, simulated NVR, separate `data-demo/`).
- There were throwaway test scripts (fake Dahua NVR with digest auth, account/site/photo tests) in the previous session's scratch folder; recreate equivalent tests if needed. Check the page script with `node --check` on the extracted `<script>` after big HTML edits.
