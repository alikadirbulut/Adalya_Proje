# Adalya Paneli — Installation

One always-on computer runs the panel. Everyone else uses a browser on their phone, tablet or PC. Nothing is installed on their devices.

```
              Always-on panel PC  (192.168.0.30, wired, on the UPS)
                         │  python adalya_panel.py --lan
      ┌──────────────────┼──────────────────┐
  Security phone      Manager laptop     Security room screen
  http://192.168.0.30:8080  (own username + password)
```

> **Do not port-forward** the panel, the NVR or the cameras to the internet. For remote access use a VPN (WireGuard/Tailscale). The panel is a local site tool.

---

## 0. What you need

| | |
|---|---|
| Computer | Any Windows PC/mini PC that stays on: Intel N100 class, 8 GB RAM, SSD |
| Python | **Python 3.9 or newer** from [python.org](https://www.python.org/downloads/) — no other packages |
| Network | Wired to the site switch (Ruijie), same network as the NVR and cameras |
| Power | Plugged into the **UPS** with the NVR |
| Folder | `adalya-toolkit` (this folder), copied to the PC |
| Time | About 20 minutes for the first install |

**Important:** keep the live folder **out of OneDrive/Dropbox**. Syncing a live SQLite database can lock or corrupt it. Copy it to `C:\AdalyaPanel`.

Files you must never delete: `data/`, `media/`, `site.json`, `devices.json`, `site-plan.webp`.

---

## 1. Install Python

1. Download Python 3.9+ (64-bit) from python.org.
2. Run the installer and tick **“Add python.exe to PATH”** before Install.
3. Open Command Prompt and check:
   ```bat
   python --version
   ```
   You should see `Python 3.13.x` (or 3.9+). If Windows opens the Store instead, install from python.org again with the PATH option.

---

## 2. Try the demo first (5 minutes)

The demo uses fictional data in `data-demo/` and never touches the real `data/`.

```bat
cd /d C:\AdalyaPanel
python adalya_panel.py --demo --lan --port 3000
```

- On the panel PC: **http://127.0.0.1:3000**
- On a phone on the same Wi-Fi: `http://<pc-ip>:3000/demo?role=resident&to=/%23/dairem` (manager: `role=admin`, guard: `role=staff`; `/demo` only exists in demo mode)
- Stop it with `Ctrl+C`.

Use this to show the panel to the manager or the guard before the real setup.

---

## 3. Install it for real

1. Copy the folder to `C:\AdalyaPanel`.
2. Open Command Prompt in that folder (Shift + right-click → “Open PowerShell window here”, or `cd /d C:\AdalyaPanel`).
3. Start:
   ```bat
   python adalya_panel.py --lan --port 8080
   ```
   Or double-click **`start-panel.bat`** (it always uses port 8080 and restarts itself if it stops).
4. Windows Firewall asks: **allow Python on private networks**. If it does not ask, add the rule from an **administrator** PowerShell:
   ```powershell
   New-NetFirewallRule -DisplayName "Adalya Paneli 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow -Profile Private
   ```
5. Find the PC's LAN address:
   ```bat
   ipconfig
   ```
   Use the **IPv4 Address** of the wired adapter (e.g. `192.168.0.30`).
6. On the panel PC open **http://127.0.0.1:8080** and complete the first-run screen:
   - ad soyad,
   - username (lowercase, e.g. `ali`),
   - password **8+ characters**.

The first admin account is created. Keep that password somewhere safe.

---

## 4. Give the panel a fixed local IP

On the router (TP-Link EX520v): **DHCP → Address Reservation** → add the panel PC's MAC with IP **192.168.0.30**.

`.30` is outside the DHCP pool (`.150–.200`), like the cameras. Find the MAC with `ipconfig /all` (“Physical Address” of the wired adapter).

Bookmarks and phones then keep working after a reboot.

---

## 5. Connect the NVR

1. **Ayarlar → NVR bağlantısı**.
2. Enter address `192.168.0.254`, NVR user and password.
3. The password is **never saved**, so after every restart an admin reconnects it. Everything else keeps working without it.
4. Try **Canlı izleme** (snapshots) and **Kayıtlar** (search/download). If something fails, see `TEST-PLAN.md` step 7 and bring back the `nvr-*` results.
5. **Live video (needs ffmpeg).** Install ffmpeg on the panel PC once:
   ```powershell
   winget install Gyan.FFmpeg
   ```
   Close and reopen the panel (or the PC) so `ffmpeg` is on its PATH, then check:
   ```bat
   ffmpeg -version
   ```
   In **Canlı izleme**, click a camera and press **Canlı video**. The panel pulls the NVR sub-stream
   (`rtsp://<user>:<password>@192.168.0.254:554/cam/realmonitor?channel=N&subtype=1`) and streams it as MJPEG
   (~1 s delay, 8 fps, 960 px); up to **4** live streams at once. If ffmpeg is missing or the NVR refuses,
   the button falls back to snapshots and says so. The NVR password stays in memory only — never in a file.

   The sub-stream is HEVC in this NVR; ffmpeg transcodes it, so live video costs CPU. For a wall of many live
   tiles use the NVR's own client; the panel's live button is meant for one enlarged camera at a time.

---

## 6. Accounts for people

**Ayarlar → Kullanıcılar → Yeni kullanıcı.** One account per person, never shared.

| Who | Role | Can |
|---|---|---|
| Watchman / security | **Güvenlik** | Live view, cameras, map, recordings, logs, exports, maintenance notes, residents' names/phones/vehicles (read-only), plate lookup, visitor log |
| Manager | **Yönetici** | Everything, including settings, users, residents, aidat, messages and the download log |
| Site sakini | **Site sakini** | Only their own flat: aidat, requests, guest passes, parcels, notifications, own password; allowed cameras live only |

Security tips:

- Unique passwords, 8+ characters.
- For admins: turn on **two-step login (TOTP)** in Ayarlar.
- The panel locks a username+IP after 5 wrong passwords for 30 seconds, and an IP after 25 failures for 5 minutes.
- Deleting a user logs them out everywhere and removes their devices.

---

## 7. Onboard residents (self-registration)

Residents register themselves; you approve.

1. Share the link **`http://192.168.0.30:8080/kayit`** (WhatsApp group, notice board, QR code — any QR generator).
2. They fill in: ad soyad, **phone (required)**, e-mail (optional), blok/kat/daire, mülk sahibi/kiraci, up to **2 vehicles**, **1–2 devices**, KVKK consent.
3. On the panel: **Site sakinleri → Kayıt istekleri → Onayla**.
   - The panel creates the flat if it does not exist, adds the person and vehicles, and creates the account as **Davetli**.
   - It shows a **14-day invite link**. Send it to the resident (WhatsApp is fine).
4. The resident opens the link, chooses a password, and is logged in.
5. The resident can add/edit/remove up to **2 vehicles** under **Dairem → Araçlarım**.

Limits:

- **2 accounts per flat** (an admin can explicitly force a third).
- **2 devices per account** (browser cookie now, app install id later).
- Only an admin can delete an account.
- Registration is rate-limited (5 per hour per IP) and needs admin approval.

If someone has no smartphone, create their account manually in **Ayarlar** and give them the password.

---

## 8. Phones and “install as an app”

- **Everyone:** open `http://192.168.0.30:8080`, add a bookmark.
- **iPhone:** Safari → Share → **Add to Home Screen**. It opens full screen without the Safari address bar (the panel has the Apple standalone tags). Add it from **Safari**, not Chrome.
- **Android:** over plain `http://` Chrome only makes a **shortcut**, so the address bar stays. A real install without the URL bar needs **HTTPS**; “Uygulamayı yükle” appears only when the browser offers it.
- **Notifications:** on the **Bildirimler** page press **Telefon bildirimi izni**. While the app is open, new duyuru/mesaj notifications appear on the phone. Background push (phone locked) needs HTTPS + VAPID; not included in the standard-library build.

If you later want a proper HTTPS address, put the panel behind a **VPN** or a tunnel (Cloudflare Tunnel/Tailscale) — never a port-forward.

---

## 9. Start automatically with Windows

1. Press `Win+R`, type `shell:startup`, press Enter.
2. Put a **shortcut** to `start-panel.bat` in that folder.
3. Power settings: never sleep, never turn off the disk.
4. Reboot once and check the panel comes back: `http://127.0.0.1:8080`.

`start-panel.bat` restarts the server after 10 seconds if it stops; close its window to stop it for good.

---

## 10. Backups (do this)

The panel writes a **daily automatic backup** to `data/yedekler/` (and before a restore). That is not enough — the disk can die.

Monthly, copy these to a USB stick or another computer:

- `data/` — database, users, documents, request photos, backups
- `media/` — memorial photo and onboarding video
- `site.json`, `devices.json`, `site-plan.webp`

Restore:

1. **Sistem → Yedekler** → choose a backup → **Geri yükle** → type **`GERI YUKLE`**.
2. A safety backup is created first.
3. After restoring, check **Bugün** and **Daireler ve sakinler**.

Test a restore once before you need it.

---

## 11. Updating the panel

1. Stop the panel (`Ctrl+C`, or close the `start-panel.bat` window).
2. **Back up** `data/` and `media/`.
3. Replace the code files from the new folder: `adalya_panel.py`, `panel.html`, `nvr.py`, `adalya_test.py`, `tests/`, icons, `README.md` etc.
4. **Never replace** `data/`, `media/`, `site.json`, `devices.json`, `site-plan.webp` — they are your site.
5. Start again and check the version at the bottom of **Nasıl kullanılır** (or `/api/me` → `version`).
6. Optional: run the API tests against a throwaway demo, not the live server:
   ```bat
   python adalya_panel.py --demo --no-browser --port 8091
   python tests/api_smoke.py http://127.0.0.1:8091
   ```
   Expected: **210 passed, 0 failed**.

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `python` is not recognized | Reinstall Python from python.org with “Add to PATH”, or use `py` instead of `python` |
| `Port 8080 is in use` | Another panel/copy is running. Close it, or start with another port: `python adalya_panel.py --lan --port 3000` |
| Phone cannot open the page | Same Wi-Fi? Windows Firewall allow Python on **private** networks? Check `ipconfig` for the right IPv4 address. Guest Wi-Fi often isolates clients |
| Page opens on the PC but not on the phone | Firewall rule (see step 3), or the phone is on mobile data / another network |
| NVR does not connect | NVR address/user/password; the NVR password is not saved, reconnect after every restart; check the NVR is on `192.168.0.254` |
| Camera tiles say “Görüntü yok” | NVR snapshot may be disabled, or the camera is down. Check **Kameralar** and **Olay geçmişi** |
| `database is locked` | You are running the panel from OneDrive/a synced folder. Move it to `C:\AdalyaPanel` |
| Time is wrong in the panel | Fix the PC clock/time zone (Europe/Istanbul), restart the panel |
| Forgot the admin password | Use another admin account. If none: stop the panel, restore `data/config.json` from a backup, or set up a fresh install and re-import residents. Do not edit the database by hand |
| The demo and the real panel look mixed up | Demo uses `data-demo/`; run the real one without `--demo`. They can run on two ports (e.g. real 8080, demo 3001) |
| “Install app” does not appear (Android) | Expected over HTTP. Use HTTPS or accept the browser shortcut |
| Browser shows an old version | Close/reopen the app; the service worker updates (`adalya-v4`). If needed, clear site data |
| Video does not play | Check `/media/onboarding.mp4` opens; the server supports Range requests. Browser must allow MP4/H.264 |

---

## 13. Security checklist before you leave

- [ ] First admin password is strong and stored safely
- [ ] **Two-step login** enabled for every admin
- [ ] Staff accounts are personal (no shared “admin”)
- [ ] Demo mode is **not** running on the site
- [ ] Firewall allows only the private network
- [ ] No port-forwarding on the router
- [ ] Backups copied off the machine at least once
- [ ] KVKK: residents were informed how their data is used; only authorised people have accounts
- [ ] Panel PC is on the UPS and starts with Windows

---

## 14. Command-line options

| Option | Default | Meaning |
|---|---|---|
| `--port` | `8080` | Web port. A busy default port moves to the next free one; an explicit busy port stops with a message |
| `--lan` | off | Listen on the site network, not only `127.0.0.1` |
| `--demo` | off | Fictional data in `data-demo/`, no login, role selector |
| `--no-browser` | off | Do not open the browser (for autostart/services) |
| `--nvr-user` | — | Prefill the NVR username on the login attempt; password is still asked and never saved |

Full technical state and next tasks: [NEXT-PROMPT.md](NEXT-PROMPT.md). Site day: [ON-SITE-CHECKLIST.md](ON-SITE-CHECKLIST.md) and [TEST-PLAN.md](TEST-PLAN.md).


