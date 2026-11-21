# On-site checklist: cameras first, then the panel

A step-by-step plan for the next visit to Adalya Sitesi. **Part 1** fixes and measures the cameras. **Part 2** connects the panel to the real NVR. Do them in this order: the panel is only as good as the network under it.

Tick each box, fill in the tables, and bring the results back. The command details for the test toolkit are in [TEST-PLAN.md](TEST-PLAN.md).

---

## Before you go

- [ ] Laptop with Python 3, the `adalya-toolkit` folder copied onto it, and Dahua **ConfigTool** installed.
- [ ] **Ethernet cable**: always test wired to the Ruijie switch, never over Wi-Fi.
- [ ] Passwords: router, NVR, and the camera list (safety codes / installer password) in a password manager, not on paper.
- [ ] Phone with a Wi-Fi analyser app (to read signal strength next to cameras).
- [ ] Multimeter or a spare **12V 1A adapter** (to test camera power).
- [ ] Optional: a small ladder for camera labels.

## Ground rules on site

- **Never reset the router, switch or NVR.** Camera addresses depend on their settings.
- **One password attempt per camera.** Dahua cameras lock for 5–30 minutes after about 5 wrong tries. If one locks, leave it and come back later.
- Back up before changing: router config first (step 1.3).
- Write what you do in the panel's **Bakım günlüğü** (or on paper, then copy it in later).

---

# Part 1: Cameras

### 1.1 Snapshot of the current state (5 min)
- [ ] Photo of the NVR screen: which channels are black? Time: ____
- [ ] Plug the laptop into the switch. `ipconfig` should show a `192.168.0.x` address.
- [ ] Baseline check:
  ```
  python adalya_test.py check
  ```
  Cameras up on 192.168.0.x: **____ / 18**. Any "WRONG DEVICE" lines? ____________

### 1.2 Router: finish the address reservations (20 min)
On the router (`http://192.168.0.1`), **DHCP → Address Reservation**. Make sure every line below exists (MACs with dashes if the form wants them):

| MAC | IP | Camera | Done |
|---|---|---|---|
| E0-2E-FE-67-B8-C3 | 192.168.0.100 | Güvenlik | ☐ |
| E0-2E-FE-67-B9-89 | 192.168.0.102 | Doğu çevre 2 | ☐ |
| E0-2E-FE-67-BB-D7 | 192.168.0.104 | Doğu çevre 1 | ☐ |
| E0-2E-FE-67-BB-C7 | 192.168.0.105 | A Blok giriş | ☐ |
| E0-2E-FE-67-B9-C9 | 192.168.0.106 | Güney çevre 1 | ☐ |
| E0-2E-FE-67-B9-E9 | 192.168.0.107 | Kuzey çevre 1 | ☐ |
| E0-2E-FE-67-B9-27 | 192.168.0.108 | Güney çevre 2 | ☐ |
| E0-2E-FE-67-B9-F3 | 192.168.0.111 | Araç şarj | ☐ |
| E0-2E-FE-67-B8-EF | 192.168.0.112 | C Blok otopark | ☐ |
| E0-2E-FE-67-BC-1D | 192.168.0.113 | B Blok giriş | ☐ |
| E0-2E-FE-67-BB-B1 | 192.168.0.115 | B Blok asansör giriş | ☐ |
| E0-2E-FE-67-BB-B3 | **192.168.0.12** (not .151!) | Cblok | ☐ |
| E0-2E-FE-67-B9-E5 | 192.168.0.14 | Kuzey çevre 2 | ☐ |
| E0-2E-FE-36-12-69 | 192.168.0.254 | NVR | ☐ |

- [ ] DHCP pool is still **192.168.0.150 – 192.168.0.200**.
- [ ] Reboot **only** the cameras that are on a wrong IP (power-cycle or NVR channel menu), so they pick up their reserved address.
- [ ] NVR: those channels turn green? ____

### 1.3 Router: backup and facts (5 min)
- [ ] **System Tools → Backup**: save the config file to the laptop.
- [ ] ISP name: ________ Firmware version: ________
- [ ] Is there an SNMP option? ☐ yes ☐ no. Remote management / TR-069 on? ☐ yes ☐ no (screenshot)
- [ ] `python adalya_test.py snmp --host 192.168.0.1` → ________

### 1.4 NVR channel cleanup (10 min)
On the NVR, Camera → Camera List:
- [ ] **Delete channel 19** (duplicate of `.102`).
- [ ] Channel 16 points at `192.168.1.104` (A Blok otopark 1). Leave it until that camera is found (1.6).
- [ ] Note any channel whose IP doesn't match the table in 1.2: ____________

### 1.5 The two cameras on the old subnet (20–40 min)
`192.168.1.128` (A Blok asansör, MAC `…BC:63`) and `192.168.1.129` (A Blok otopark, MAC `…B9:F1`).
1. [ ] Give the laptop's wired adapter a second address (administrator command prompt; adapter name from `ipconfig`):
   ```
   netsh interface ip add address "Ethernet" 192.168.1.50 255.255.255.0
   ```
2. [ ] ConfigTool → Search: both appear? ☐ .128 ☐ .129
3. [ ] **Modify IP**, Static, mask `255.255.255.0`, gateway `192.168.0.1`:
   - `.1.128` → **192.168.0.128**. Password tried: ☐ own safety code ☐ installer password. Result: ____
   - `.1.129` → **192.168.0.129**. Password tried: ☐ own safety code ☐ installer password. Result: ____
4. [ ] If a password is refused twice: **stop**. That camera needs a physical reset (hold its reset button about 10 s), then re-initialise it with the site password. Note its location: ____
5. [ ] Add reservations for them on the router (`E0-2E-FE-67-BC-63` → `.128`, `E0-2E-FE-67-B9-F1` → `.129`).
6. [ ] NVR: change channel 17 → `192.168.0.128`, channel 18 → `192.168.0.129`. Green? ____
7. [ ] Remove the extra address: `netsh interface ip delete address "Ethernet" 192.168.1.50`

### 1.6 Site walk: cameras not on the network (60+ min)
For every camera that isn't in ConfigTool or the router list (Batı çevre 1, B Blok otopark, C Blok asansör `E0:2E:FE:87:8C:68`, B Blok asansör, A Blok otopark 1, and any undocumented ones):

| Camera / location | LED on? | 12V at adapter? | Wi-Fi signal at camera (phone) | Nearest repeater/router | MAC / SN from label | Result |
|---|---|---|---|---|---|---|
| | | | | | | |
| | | | | | | |
| | | | | | | |
| | | | | | | |
| | | | | | | |

Possible results: **dead adapter** (swap it), **no Wi-Fi** (too far / repeater down), **wrong network** (camera configured for another SSID), **dead camera**. When one comes back: reserve its IP on the router, add it to the NVR, and add it to `devices.json`.

### 1.7 Weak-signal cameras: measure, tune, measure again (2 × 30 min)
Cameras: Güney çevre 1 (`.106`), Güney çevre 2 (`.108`), Cblok (`.12`).

1. [ ] **Before**: record the signal in the router's wireless client list, then log drops for 30 minutes:
   ```
   python adalya_test.py watch --only 106,108,12
   ```
2. [ ] **Tune** (camera web page or NVR remote settings): H.265, main stream **1–1.5 Mbps**, **10–15 FPS**.
3. [ ] **Repeaters**: a **single-radio** extender (Tenda T10, TL-WA855RE) **must repeat on the router's channel** — it cannot receive on 11 and transmit on 1 with one radio. Do **not** change the repeaters' channel; that just breaks their uplink. What you can do today:
   - give them their own SSID so cameras stop hopping between router and repeater (helps stability, not airtime);
   - move a repeater closer to the router (the Tenda uplink is RSSI 34 — too weak; every hop also **doubles** the airtime a camera stream uses);
   - check whether the Tenda/TP-Link can run in **AP mode over Ethernet**; if one can be wired, it becomes a real AP and *then* channel 1 or 6 makes sense.
   Channels can only be separated with **wired-backhaul APs**; with these repeaters everything stays on the router's single 2.4 GHz channel.
4. [ ] **After**: 30 minutes of the same watch log.

| Camera | RSSI before | Drops / 30 min before | Uptime before | RSSI after | Drops / 30 min after | Uptime after |
|---|---|---|---|---|---|---|
| Güney çevre 1 (.106) | 18 | | | | | |
| Güney çevre 2 (.108) | 25 | | | | | |
| Cblok (.12) | 28 | | | | | |

### 1.7b Router: who is on which radio? (10 min)
The router is not broken — it is the **shared medium**. One 2.4 GHz radio (channel 11) carries all 18 cameras, both repeaters and every resident phone. A camera behind a repeater uses the channel **twice** (camera→repeater, repeater→router), so each repeater camera costs double airtime.

- [ ] Router **Wireless → Client list**: for each camera, note RSSI, Tx/Rx rate, and whether it sits on the router or a repeater (if the UI shows it).
- [ ] Phone Wi-Fi analyser: channel occupancy on 1 / 6 / 11 and how many neighbour networks. Is 11 actually the quietest?
- [ ] Do the three weak cameras (`.106`, `.108`, `.12`) connect to the router or to a repeater? If a camera is on a repeater while the router is closer, that is a placement problem, not a camera problem.
- [ ] Note the repeater uplink RSSI (Tenda was 34) — below ~40 the repeater halves the usable throughput.
- [ ] If 11 is crowded, move the **whole** 2.4 GHz cell (router) to 1 or 6; the repeaters follow automatically because they repeat the same channel.

### 1.8 Access point: collect what's needed to decide (15 min)
The AP (IP-Com iUAP-AC-M, ~4,470 TL) is **not decided yet**. This visit only collects the facts:
- [ ] After tuning (1.7), do the Güney cameras still drop? ☐ yes ☐ no. Suggested rule: still dropping or uptime under 99% → the AP is worth it.
- [ ] Where would it mount (south side, line of sight to Güney 1 and 2)? ____________
- [ ] Cable route from the switch to that spot, approximate length: ____ m (must be under 100 m)
- [ ] Power: a PoE injector next to the switch (the switch has no PoE). Socket available? ☐
- [ ] Shade at the mounting spot (the AP is rated to 50 °C)? ☐
- [ ] Remember if bought: its factory IP `192.168.0.254` **clashes with the NVR**. Set it up on the laptop first, move it to `192.168.0.13`.

### 1.9 NVR health (10 min)
- [ ] Date and time correct; NTP on (time zone Istanbul).
- [ ] HDD status OK, capacity: ____, recording on **all** channels (Storage → Schedule).
- [ ] **Info → BPS** screenshot (live bitrate per channel).
- [ ] Firmware version: ____
- [ ] NVR log (Info → Log) for the last 7 days: photo of video-loss / network lines.

### 1.10 Elevator cameras (note only)
- [ ] Which elevators have cameras? ____ How do they connect now? ____
- A moving metal cabin needs a dedicated elevator bridge kit or a travelling cable; repeaters won't stay connected. Note the elevator brand and machine room access for a quote.

### 1.11 Before leaving: passwords (10 min)
- [ ] List devices still on default passwords (switch: `admin`). Change them only when the work is done, and store the new ones in the password manager.

---

# Part 2: The panel (UI) on the real network

### 2.1 Start it live
- [ ] On the laptop (wired):
  ```
  python adalya_panel.py
  ```
- [ ] Setup screen: create the first admin account (your name, username, password).
- [ ] **Genel bakış**: the camera count matches step 1? ____

### 2.2 Connect the NVR
- [ ] **Ayarlar → NVR bağlantısı**: `192.168.0.254`, user, password → **Bağlan**. Connected, model shown? ____
- [ ] If it says the password was refused: the panel stopped on purpose (no lockout). Fix the password and press Bağlan once more.
- [ ] Tip: create a separate view-only user on the NVR for the panel.

### 2.3 Test every NVR feature (each one was only tested against a simulated NVR)

| Page | Test | Works? | Notes |
|---|---|---|---|
| Genel bakış | NVR disk shows a % (not "?") | ☐ | |
| Canlı izleme | Tiles show pictures | ☐ | |
| Canlı izleme | Click a tile → large view updates continuously; frame rate shown next to the name: ____ kare/sn (good camera) / ____ kare/sn (Güney çevre 1) | ☐ | |
| Canlı video | Click a tile → **Canlı video** → real RTSP stream (no slideshow), ~1 s delay; which channels work: ____ | ☐ | ffmpeg installed? |
| Canlı video | Open a second live camera; close both and check ffmpeg processes stop | ☐ | |
| Yük testi | Open the wall, then the enlarged view, then live video, one at a time; does the NVR monitor show "network disconnected"? Which page triggers it: ____ | ☐ | 3.5 caps 4 snapshot/s + 2 streams; if it still drops, `--snap-rate 2 --live-max 1` |
| Kayıtlar | Güney çevre 1, today, **Kayıtları bul** → files listed | ☐ | |
| Kayıtlar | **İndir** one file, open in Dahua SmartPlayer / VLC | ☐ | |
| Olaylar ve loglar | **Logları getir** → NVR log lines | ☐ | |
| Dışa aktar | Tam paket zip contains `nvr-log.csv` | ☐ | |
| NVR arayüzü | NVR page opens inside the panel (or only in a new tab?) | ☐ | |
| Kameralar | Channel numbers match the NVR's channel names | ☐ | |

If something fails, run `python adalya_test.py nvr --user admin` and bring back the `results/nvr-*` folder; it shows exactly what the NVR answers.

### 2.3b Which channels really have a camera? (5 min)
Run this with the panel's camera pages closed. It asks the NVR for **one snapshot per channel, 2 s apart**, so it cannot overload the box or the Wi-Fi cameras:
```
python adalya_test.py channels --user admin
```
It prints the NVR's own channel names and marks `OK` for channels with a picture; results are saved in `results/channels-*.csv`. Write the OK channels into `devices.json` (**Ağ cihazları → Cihaz envanteri**) and remove channels that have no camera. If a channel is OK here but shows nothing in the panel, the channel name/number in `devices.json` is wrong.

### 2.3c Person / vehicle detection (10 min)
The cameras are advertised as detecting people and cars. This is the test that proves it and captures the exact payload the panel would consume:
```
python adalya_test.py events --user admin --minutes 10
```
While it runs, walk in front of a camera, then drive a car past one. Leave with the `results/events-*.jsonl` file. On the NVR, also check **Event → Smart Motion / IVS** per channel: enabled? Human/Vehicle classification on? Which channels? Write the list here: ____________

If the payload contains `"ObjectType": "Human"` / `"Vehicle"`, the feature is straightforward. If it only sends `VideoMotion`, the cameras detect motion, not objects — still useful, but the panel must say "hareket", not "kişi".

### 2.4 Live video: confirm the stream parameters (15 min)
Live video now ships as ffmpeg → MJPEG (3.4). These tests confirm the sub-stream settings it depends on and whether the NVR can serve MJPEG directly. Nothing is changed on the NVR; only read.

Use the **view-only NVR user** from 2.2 for these tests, not the admin account. Pick one good camera (e.g. channel 3, Güvenlik) and one weak one (channel 9, Güney çevre 1).

1. [ ] **RTSP sub stream in VLC** (Media → Open Network Stream):
   ```
   rtsp://<user>:<password>@192.168.0.254:554/cam/realmonitor?channel=3&subtype=1
   ```
   Plays? ☐ yes ☐ no. Delay vs. reality (wave at the camera): ____ s. Same for channel 9: ☐ plays, delay ____ s.
   If it doesn't play, try `subtype=0` (main stream) and note the result: ____
   Afterwards clear VLC's recent media list (Media → Open Recent → Clear), since it stores the URL with the password.
2. [ ] **Codecs** (NVR → Camera → Encode, or VLC → Tools → Codec Information while playing). Note for 3–4 cameras:

   | Channel | Main stream codec / resolution / FPS / bitrate | Sub stream codec / resolution / FPS / bitrate |
   |---|---|---|
   | 3 | | |
   | 9 | | |
   | | | |
   | | | |

   Browsers play **H.264** reliably, H.265 only on some devices; the panel avoids that by transcoding the HEVC sub stream to MJPEG with ffmpeg, so the sub-stream codec only sets the CPU cost. Target for the box: main stream H.265 (recording, saves Wi-Fi), **sub stream H.264** or HEVC, about 640×360–704×576, 10–15 FPS, 256–512 kbps. Don't change it today unless asked; just note what is there and whether the sub stream codec can be set to H.264 (☐ yes ☐ no) and to MJPEG (☐ yes ☐ no).
3. [ ] **MJPEG in a browser** (the browser asks for the NVR login):
   ```
   http://192.168.0.254/cgi-bin/mjpg/video.cgi?channel=3&subtype=1
   ```
   Moving picture? ☐ yes ☐ only one frame / error ☐ asks for login, then error. Message shown: ____________
4. [ ] NVR → Network → Port: RTSP port is 554? ☐ (other: ____). Max. remote/online users setting: ____
5. [ ] On the NVR **Info → BPS** screen, note the total incoming bitrate while the VLC stream from test 1 is playing: ____ Mbps.

### 2.5 Map
- [ ] **Site haritası → Haritayı düzenle**: drag each camera to its real spot during the site walk (1.6).
- [ ] Put the block labels back (Ayarlar → Site ve harita → Etiket ekle: A blok, B blok, C blok, Bahçe, Havuz, Otopark) and drag them into place.
- [ ] Check the rotation looks right (slider in edit mode).

### 2.6 Other screens
- [ ] Restart with `python adalya_panel.py --lan` and allow Python through Windows Firewall (private networks).
- [ ] From a phone on the site Wi-Fi: `http://<laptop-ip>:8080` (or the port you chose) opens and logs in? ☐
- [ ] Create one **Güvenlik** account for security and test it on their device.

### 2.7 Leave it running
- [ ] Keep the panel running during the 1.7 measurements and compare its **Bugün kopma** and **24 sa erişim** columns with the watch log.
- [ ] Write what was done today in **Bakım günlüğü**.

---

## Bring back

- [ ] Photos: NVR screen (before/after), router reservation list, NVR channel list
- [ ] `results/` folder (check, drops, watch summaries, nvr probe)
- [ ] Router config backup file (keep private)
- [ ] Filled-in tables from 1.2, 1.5, 1.6, 1.7, 1.8, 2.3, 2.4 (live video: RTSP, codecs, MJPEG)
- [ ] NVR BPS screenshot and firmware version
- [ ] Router ISP, firmware, SNMP/remote management screenshot
- [ ] Your AP decision notes (1.8)

