# Adalya Panel — Installation

## The idea: one panel, many screens

Install the panel on **one computer that stays on** in the security room. Everyone else (watchman, manager, you) opens it in a **browser** on their own phone, tablet or PC. Nothing gets installed on their devices.

Why not install it on each device: every copy would ping the cameras on its own, keep its own history and its own download log, and need its own NVR login. Logs would disagree, and if something happens you would not have one record of who downloaded what.

```
            Always-on panel PC (192.168.0.30, wired to the switch, on the UPS)
                 │  runs adalya_panel.py --lan
   ┌─────────────┼───────────────────┐
 Watchman phone   Manager laptop      Security room screen
 http://192.168.0.30:8080 (browser, own username and password)
```

## 1. The panel computer

- Any Windows PC or small mini PC that can stay on: Intel N100 class, 8 GB RAM, SSD is plenty.
- **Wired** to the Ruijie switch, and plugged into the **UPS** with the NVR.
- Install **Python 3** from python.org (tick "Add python.exe to PATH").
- Copy the whole `adalya-toolkit` folder to it, for example `C:\AdalyaPanel`.
- Power settings: never sleep, never turn off the disk.

## 2. Give it a fixed local IP

This is the "static IP" that matters for the panel: a fixed address **inside the site network**, so everyone's bookmark keeps working.

Easiest, on the router (TP-Link EX520v): DHCP → **Address Reservation** → add the panel PC's MAC with IP **192.168.0.30**. (`.30` is outside the DHCP pool `.150–.200`, like the cameras.)

Find the PC's MAC with `ipconfig /all` (the "Physical Address" of the wired adapter).

## 3. Start it automatically

1. Double-click `start-panel.bat` once. Windows Firewall will ask: allow Python on **private networks**.
2. Open `http://127.0.0.1:8080` on the panel PC, create the first admin account.
3. **Ayarlar → NVR bağlantısı**: connect the NVR. The NVR password is never saved, so after a restart someone logs in as admin and reconnects (the rest of the panel keeps working without it).
4. Press `Win+R`, type `shell:startup`, and put a **shortcut** to `start-panel.bat` in that folder. The panel now starts with Windows and restarts itself if it ever stops.

## 4. Accounts for each person

**Ayarlar → Kullanıcılar → Yeni kullanıcı.** One account per person, never shared:

| Who | Role | Can |
|---|---|---|
| Watchman / security | Güvenlik | Watch, search and download recordings, pull logs, write maintenance notes; read-only residents' names, phones, vehicles, plate lookup, visitor log |
| Manager | Yönetici | Everything, including settings, users, residents, aidat, messages and the download log |
| You | Yönetici | Everything |

Give each person their username and password privately. They can change their own password under **Ayarlar → Şifrem**. When someone leaves, delete their account.

## 5. Opening it on phones and laptops

On any device connected to the **site network**, open `http://192.168.0.30:8080` and add it to the home screen / bookmarks.

## 6. Access from outside the site (only if really needed)

A "static IP" from the internet provider is a different thing: a fixed **public** address. **Do not** use it to forward the panel or the NVR to the internet. NVRs and small web panels exposed like that get found and attacked within days, and these recordings are personal data under KVKK.

If remote access is truly needed, use a **VPN** that only your people can join, for example WireGuard or Tailscale on the panel PC. People connect to the VPN first, then open the panel as if they were on site.

## 7. Backups

Copy these to a USB stick or another computer every month, and after big changes:

- `data/` (history, users, maintenance notes, download log, **belgeler/** uploaded documents)
- `site.json`, `media/` (memorial section and map labels)
- `devices.json` (cameras, IPs, channels, map positions)
- `site-plan.webp` (the map image)

## 8. Changing the map image

Save a new image as `site-plan.png` (or `site-plan.jpg`) in the panel folder, replacing the old one, and refresh the page. Then **Site haritası → Haritayı düzenle** and drag the block labels and cameras onto it.
