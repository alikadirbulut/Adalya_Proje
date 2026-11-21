# Adalya — Site Test Day

Everything here is read-only. Nothing changes a device. Results land in the `results` folder next to the script.

## Before you go
- [ ] Laptop has Python 3 (`python --version`). No extra packages needed.
- [ ] Copy this whole `adalya-toolkit` folder to the laptop (not into OneDrive).
- [ ] Update `devices.json` if any IP changed since the last visit.
- [ ] Bring an Ethernet cable. Test from a **wired** connection to the Ruijie switch, so the laptop's own Wi-Fi doesn't distort the results.
- [ ] Confirm the installed build: open **Nasıl kullanılır** and check the version at the bottom (**3.4**), or `/api/me` → `version`.
- [ ] Phone test once before leaving: log in as Yönetici and as Güvenlik; the top bar/menu, Vardiya and camera wall should work. Add it to the home screen if wanted.

## 1. Baseline health check (2 min)
```
python adalya_test.py check
```
Look for:
- **Cameras up on 192.168.0.x: N / 18**: write down N.
- **WRONG DEVICE**: the IP answers but with a different MAC, so something drifted or two devices share an address.
- **Still answering on old subnet**: only shows if the laptop also has a 192.168.1.x address (step 5).
- **SNMP** lines: does the router or switch answer? (Expected: the switch won't; the router is unknown.)

## 2. Drop log for the weak cameras (30–60 min)
```
python adalya_test.py watch --only 106,108,12
```
Leave it running while you do other work, then press Ctrl+C. It prints every drop with its duration and saves:
- `drops-*.csv`: every drop, when it started, when it came back, how many seconds
- `watch-summary-*.csv`: uptime % and drop count per camera

To watch everything at once: `python adalya_test.py watch`

Compare with the NVR screen: if the ping drops at the same time the picture goes black, the cause is the Wi-Fi link. If ping stays up but the picture drops, it's camera load or bitrate.

## 3. NVR API test (2 min)
```
python adalya_test.py nvr --user admin
```
It asks for the NVR password (not saved, not shown). It makes one login attempt and **stops on the first refusal**, so it can't lock the account.

Note which lines say **OK**: device type, firmware, time, NTP, channel names, camera states, record mode, disks. This decides what the panel can show. The raw answers are saved in `results/nvr-*/`. They contain channel names and settings, not passwords.

## 4. SNMP tests (1 min each)
```
python adalya_test.py snmp --host 192.168.0.1
python adalya_test.py snmp --host 192.168.0.13
```
The first is the router, the second an IP-Com AP once installed. It tries the communities `public` and `private`. "no answer" means SNMP is off or uses another community; check the device's web settings.

## 5. Old-subnet cameras (A Blok asansör, A Blok otopark)
Give the laptop's wired adapter a second address. Run in an **administrator** command prompt (replace "Ethernet" with your adapter's name from `ipconfig`):
```
netsh interface ip add address "Ethernet" 192.168.1.50 255.255.255.0
```
Then run `python adalya_test.py check` again. If they answer on 192.168.1.128 / .129, they're alive and only need re-addressing. Remove the extra address afterwards:
```
netsh interface ip delete address "Ethernet" 192.168.1.50
```

## 6. Before / after the new access point
1. Run step 2 for 30 minutes **before** installing the IP-Com AP. Save the summary.
2. Install and configure the AP (factory IP 192.168.0.254 clashes with the NVR: set it up on the laptop first, move it to 192.168.0.13).
3. Run step 2 again for 30 minutes **after**.
4. Compare uptime % and drop counts. This is the evidence for the client and for a second AP.

## 7. Panel (web UI)
Try it at home first with simulated data:
```
python adalya_panel.py --demo
```
For a phone on the same Wi-Fi use a fixed port and `--lan`, then the role links:
```
python adalya_panel.py --demo --lan --port 3000
http://<laptop-ip>:3000/demo?role=resident&to=/%23/dairem
http://<laptop-ip>:3000/demo?role=staff&to=/%23/vardiya
```
Demo checklist before the site day:
- [ ] **İstekler**: send a request as resident, reply/change status as admin, follow up as resident; close it.
- [ ] **Bildirimler**: bell/phone tab badge, mark read; publish a Duyuru and send an application message from **Mesaj gönder**.
- [ ] **Dairem → Araçlarım**: add, edit, remove; third car is refused.
- [ ] **Vardiya**: 4/6/8/10 wall, click a tile to enlarge; quick guest entry.
- [ ] **Kayıt istekleri**: submit `/kayit` from the phone, approve on the PC, open the generated invite link.
- [ ] **Mobil**: on a phone-width window as Yönetici/Güvenlik, the top bar opens the drawer, groups show badges, forms are full width, and tables scroll sideways.
- [ ] **Canlı video**: click a tile → **Canlı video**; it should play the RTSP sub-stream (~1 s delay), not a slideshow. If it falls back to snapshots, check `ffmpeg -version` and the NVR password/connection.
- [ ] **Nasıl kullanılır**: watch the video, check the biography and memorial dates.

On site, live (opens http://127.0.0.1:8080 in the browser):
```
python adalya_panel.py
```
The first live start shows a setup screen: enter your name and pick an admin password (8+ characters). Then connect the NVR from **Ayarlar → NVR bağlantısı** (address 192.168.0.254, user, password). The NVR password is never saved; enter it again after each restart, or start with:
```
python adalya_panel.py --nvr-user admin
```
To show it on another screen on the site network: add `--lan`, then open `http://<laptop-ip>:8080` there. Windows may ask to allow Python through the firewall. Set a **görevli** (staff) password in Ayarlar for security staff.

Pages to test against the real NVR (each was tested only against a simulated Dahua NVR so far):
- [ ] **Canlı izleme**: do the camera tiles show pictures? If all say "Görüntü alınamadı", the NVR's snapshot feature may be off.
- [ ] **Kayıtlar**: pick Güney çevre 1, today, search. Do files appear? Download one and open it in Dahua SmartPlayer or VLC.
- [ ] **Olay geçmişi → NVR logları**: do log lines appear?
- [ ] **Dışa aktar → Tam paket**: does the zip contain `nvr-log.csv`?
- [ ] **NVR arayüzü**: does the NVR's own page open inside the panel, or only in a new tab?
- [ ] **Genel bakış → NVR disk**: shows a %? If "?", bring back the `nvr-*` results from step 3.
- [ ] Check that the channel numbers in the panel match the NVR (channel numbers are in `devices.json`, taken from the NVR list on 14 Nov).

On the day:
- [ ] **Site haritası → Haritayı düzenle**: drag each camera to its real spot during the site walk. Positions save to `devices.json`.
- [ ] Optional: save a site plan image as `site-plan.png` in this folder; it becomes the map background.
- [ ] Leave the panel running during the before/after AP test and compare the "24 sa erişim" and "Bugün kopma" columns.
- [ ] **Ayarlar → Proje bilgileri**: add the support contact and, if wanted, a few words for Esin Noğan.
- [ ] Write what you did in **Bakım günlüğü**.

## Results to bring back
- [ ] `check-*.csv` (baseline)
- [ ] `drops-*.csv` and `watch-summary-*.csv` (before and after the AP)
- [ ] `nvr-*` folder
- [ ] SNMP results (copy from the screen)
- [ ] Router: ISP name, firmware version, screenshot of SNMP / remote management settings
- [ ] NVR: Info → BPS screenshot (live bitrate per channel)


