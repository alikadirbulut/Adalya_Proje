"""Smoke tests for Adalya Paneli roles, registry, import, plate lookup and device inventory.

Run against a DEMO server only (it adds and deletes test records; the demo works on copies in data-demo/,
so the real devices.json and site.json are not touched). Start a fresh demo for each run:
    python adalya_panel.py --demo --no-browser --port 8091
    python tests/api_smoke.py http://127.0.0.1:8091
"""
import base64, io, json, sys, urllib.request, urllib.error, zipfile

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091"
ok = fail = 0


def call(path, role="admin", body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Cookie": f"adalya_demo_role={role}", "Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req) as r:
            data = r.read()
            return r.status, (json.loads(data) if r.headers.get("Content-Type", "").startswith("application/json") else data)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


# roles
check("resident cannot read status", call("/api/status", "resident")[0] == 403)
check("resident cannot get snapshot", call("/api/nvr/snapshot?ch=2", "resident")[0] == 403)
check("resident cannot read registry", call("/api/registry", "resident")[0] == 403)
check("resident cannot plate lookup", call("/api/plate?q=07", "resident")[0] == 403)
c, my = call("/api/my", "resident")
check("resident sees own flat", c == 200 and my["flat"]["id"] == 1, my)
check("resident flat has no notes field", "notes" not in (my.get("flat") or {}))
check("staff reads status", call("/api/status", "staff")[0] == 200)
c, reg = call("/api/registry", "staff")
check("staff registry has no emails/arsa payı", c == 200 and all("email" not in p for f in reg["flats"] for p in f["people"]) and "arsa_payi" not in reg["flats"][0])
check("staff cannot edit flats", call("/api/flats", "staff", {"daire": "99"})[0] == 403)
check("staff cannot add devices", call("/api/devices", "staff", {"name": "x"})[0] == 403)
check("staff cannot export registry", call("/api/registry.csv", "staff")[0] == 403)
c, reg = call("/api/registry")
check("admin registry has emails", c == 200 and any(p.get("email") for f in reg["flats"] for p in f["people"]))

# resident request
check("resident can send request", call("/api/my/request", "resident", {"text": "Test talebi"})[0] == 200)
c, rq = call("/api/requests")
check("admin sees requests", c == 200 and any(r["text"] == "Test talebi" for r in rq["requests"]))

# plate lookup
plate = reg["flats"][0]["vehicles"][0]["plate"] if reg["flats"][0]["vehicles"] else next(v["plate"] for f in reg["flats"] for v in f["vehicles"])
c, pl = call("/api/plate?q=" + urllib.request.quote(plate.replace(" ", "").lower()), "staff")
check("plate lookup finds lower-case no-space plate", c == 200 and any(r["plate"] == plate for r in pl["results"]), pl)

# visitors
check("staff adds visitor", call("/api/visitors", "staff", {"name": "Test ziyaretçi", "flat_id": 2, "plate": "07 ziy 1"})[0] == 200)
c, vs = call("/api/visitors", "staff")
v = next((x for x in vs["visitors"] if x["name"] == "Test ziyaretçi"), None)
check("visitor listed with uppercased plate", v and v["plate"] == "07 ZIY 1", v)
check("visitor checkout", call("/api/visitors/out", "staff", {"id": v["id"]})[0] == 200)

# import CSV (semicolon, Turkish headers, cp1254)
csv_text = "Blok;Daire;Ad Soyad;Telefon;Durum;Plaka;Marka Model\nD;1;Şule Test;0500 111 22 33;Kiracı;07 tst 1;Fiat Egea\nD;1;Ömer Test;;Malik;;\nD;2;;;;07 TST 2;\n"
data = "data:text/csv;base64," + base64.b64encode(csv_text.encode("cp1254")).decode()
c, pv = call("/api/registry/import", "admin", {"data": data, "preview": True})
check("csv preview counts", c == 200 and pv["counts"] == {"flats": 2, "people": 2, "vehicles": 2, "updated": 0}, pv)
c, im = call("/api/registry/import", "admin", {"data": data})
c, reg2 = call("/api/registry")
d1 = next((f for f in reg2["flats"] if f["blok"] == "D" and f["daire"] == "1"), None)
check("csv import wrote flat, people, vehicle", d1 and len(d1["people"]) == 2 and d1["vehicles"][0]["plate"] == "07 TST 1"
      and next(p for p in d1["people"] if p["name"] == "Şule Test")["kind"] == "kiraci", d1)
c, again = call("/api/registry/import", "admin", {"data": data, "preview": True})
check("re-import only updates", again["counts"]["flats"] == 0 and again["counts"]["people"] == 0 and again["counts"]["updated"] == 4, again)

# import XLSX (hand-made minimal workbook with shared + inline strings)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
    z.writestr("xl/sharedStrings.xml", "<sst xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><si><t>Blok</t></si><si><t>Daire</t></si><si><t>Ad soyad</t></si><si><t>Plaka</t></si><si><t>E</t></si><si><t>Xlsx Kişi</t></si></sst>")
    z.writestr("xl/worksheets/sheet1.xml", "<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData>"
               "<row r='1'><c r='A1' t='s'><v>0</v></c><c r='B1' t='s'><v>1</v></c><c r='C1' t='s'><v>2</v></c><c r='D1' t='s'><v>3</v></c></row>"
               "<row r='2'><c r='A2' t='s'><v>4</v></c><c r='B2'><v>5</v></c><c r='C2' t='s'><v>5</v></c><c r='D2' t='inlineStr'><is><t>07 XLS 5</t></is></c></row>"
               "</sheetData></worksheet>")
data = "data:application/octet-stream;base64," + base64.b64encode(buf.getvalue()).decode()
c, im = call("/api/registry/import", "admin", {"data": data})
c, reg3 = call("/api/registry")
e5 = next((f for f in reg3["flats"] if f["blok"] == "E" and f["daire"] == "5"), None)
check("xlsx import", e5 and e5["people"][0]["name"] == "Xlsx Kişi" and e5["vehicles"][0]["plate"] == "07 XLS 5", (im, e5))
check("bad import rejected", call("/api/registry/import", "admin", {"data": "a,b\n1,2"})[0] == 400)

# resident user needs a flat (demo refuses user creation, so just check the validation path)
c, r = call("/api/users", "admin", {"username": "x.y", "name": "X", "role": "resident", "password": "12345678"})
check("user creation blocked in demo", c == 400)

# devices
check("add device", call("/api/devices", "admin", {"name": "Test kamera", "ip": "192.168.0.140", "mac": "e0-2e-fe-11-22-33", "type": "camera", "channel": 30})[0] == 200)
c, dv = call("/api/devices")
t = next((d for d in dv["devices"] if d["ip"] == "192.168.0.140"), None)
check("device saved with normalised MAC", t and t["mac"] == "E0:2E:FE:11:22:33" and t["channel"] == 30, t)
check("duplicate IP refused", call("/api/devices", "admin", {"name": "Dup", "ip": "192.168.0.140", "type": "ap"})[0] == 400)
check("duplicate channel refused", call("/api/devices", "admin", {"name": "Dup", "ip": "192.168.0.141", "type": "camera", "channel": 30})[0] == 400)
check("public IP refused", call("/api/devices", "admin", {"name": "Bad", "ip": "8.8.8.8", "type": "ap"})[0] == 400)
check("edit device IP", call("/api/devices", "admin", {"orig_ip": "192.168.0.140", "name": "Test kamera", "ip": "192.168.0.142", "type": "camera", "channel": 30})[0] == 200)
c, st = call("/api/status")
check("status shows edited device", any(d["ip"] == "192.168.0.142" for d in st["devices"]) and not any(d["ip"] == "192.168.0.140" for d in st["devices"]))
check("delete device", call("/api/devices/delete", "admin", {"ip": "192.168.0.142"})[0] == 200)

# aidat (demo: 1500 TL/month for 6 months; flat 1 left the last two months unpaid)
check("staff cannot see dues", call("/api/dues", "staff")[0] == 403)
check("resident cannot see all dues", call("/api/dues", "resident")[0] == 403)
c, du = call("/api/dues")
f1 = du["flats"]["1"]
check("flat 1 overdue = 3000 in 2 items", c == 200 and f1["overdue"] == 3000 and f1["overdue_count"] == 2, f1)
check("late fee estimated", f1["late_fee"] > 0, f1)
c, my = call("/api/my", "resident")
check("resident sees own dues", my["dues"]["state"]["overdue"] == 3000 and len(my["dues"]["ledger"]) >= 6 and my["dues"]["iban"], my.get("dues", {}).get("state"))
check("pay 1500 (Turkish format)", call("/api/dues/pay", "admin", {"flat_ids": [1], "amount": "1.500,00", "date": "2026-11-18"})[0] == 200)
c, du = call("/api/dues")
check("oldest item closed first", du["flats"]["1"]["overdue"] == 1500 and du["flats"]["1"]["overdue_count"] == 1, du["flats"]["1"])
check("bulk extra charge", call("/api/dues/charge", "admin", {"flat_ids": [1, 2], "amount": 250, "note": "Çatı onarımı payı"})[1].get("count") == 2)
check("extra charge needs a note", call("/api/dues/charge", "admin", {"flat_ids": [1], "amount": 10})[0] == 400)
c, r = call("/api/dues/pay", "admin", {"flat_ids": [1, 2]})
check("bulk paid-in-full clears balances", c == 200 and r["count"] == 2, r)
c, du = call("/api/dues")
check("balances zero after bulk pay", du["flats"]["1"]["balance"] == 0 and du["flats"]["2"]["balance"] == 0, (du["flats"]["1"], du["flats"]["2"]))
c, gp = call("/api/dues/generate", "admin", {"period": "2099-01", "preview": True})
check("generate preview for a new month", c == 200 and gp["made"] == len(du["flats"]) and gp["due_date"] == "2099-01-10", gp)
c, gp = call("/api/dues/generate", "admin", {"period": du["month"], "preview": True})
check("no double aidat for an existing month (only flats added later get it)", gp["skipped"] == 24 and gp["made"] + gp["skipped"] == len(du["flats"]), gp)
c, led = call("/api/dues/ledger?flat_id=1")
last = led["ledger"][-1]
check("ledger ends at zero", last["balance"] == 0, last)
check("delete ledger entry", call("/api/dues/delete", "admin", {"type": last["type"], "id": last["id"]})[0] == 200)
check("staff cannot record payments", call("/api/dues/pay", "staff", {"flat_ids": [1]})[0] == 403)

# resident camera access (demo: on; all residents ch 3; A blok cameras by name)
c, me = call("/api/me", "resident")
check("resident /api/me reports camera count", me["user"]["cams"] >= 2, me["user"])
c, mc = call("/api/my/cameras", "resident")
chans = {d["channel"] for d in mc["devices"]}
check("resident sees only allowed cameras, no IPs", c == 200 and 3 in chans and 8 in chans and 9 not in chans and all(d["ip"].startswith("ch") for d in mc["devices"]), chans)
check("resident snapshot allowed camera", call("/api/nvr/snapshot?ch=8", "resident")[0] == 200)
check("resident snapshot other camera refused", call("/api/nvr/snapshot?ch=9", "resident")[0] == 403)
check("resident cannot search recordings", call("/api/nvr/records?ch=8&start=2026-11-18%2000:00:00&end=2026-11-18%2023:59:59", "resident")[0] == 403)
check("per-flat override", call("/api/cam-access/flat", "admin", {"flat_id": 1, "channels": [9]})[0] == 200)
check("override applies", call("/api/nvr/snapshot?ch=9", "resident")[0] == 200 and call("/api/nvr/snapshot?ch=8", "resident")[0] == 403)
call("/api/cam-access/flat", "admin", {"flat_id": 1, "channels": None})
c, ca = call("/api/cam-access")
check("master switch off blocks residents", call("/api/cam-access", "admin", {**ca["access"], "enabled": False})[0] == 200 and call("/api/nvr/snapshot?ch=8", "resident")[0] == 403)
call("/api/cam-access", "admin", ca["access"])
check("staff cannot change camera access", call("/api/cam-access", "staff", {"enabled": True})[0] == 403)

# ---------------------------------------------------------------- Phase C: calendar, contracts, documents
check("staff cannot read calendar", call("/api/calendar", "staff")[0] == 403)
check("staff cannot read contracts", call("/api/contracts", "staff")[0] == 403)
check("resident cannot read documents", call("/api/documents", "resident")[0] == 403)
c, cal = call("/api/calendar")
check("calendar has 6 yearly + 12 monthly duties", c == 200 and len(cal["duties"]) == 18, len(cal.get("duties", [])))
check("calendar has demo tasks and templates", len(cal["tasks"]) >= 3 and len(cal["templates"]) == 4, cal["tasks"])
year = cal["year"]

c, r = call("/api/calendar/task", "admin", {"date": f"{year}-12-05", "title": "Asansör firmasını ara", "kind": "bakim", "note": "teklif"})
check("calendar task added", c == 200 and r.get("id"), r)
tid = r["id"]
c, cal = call("/api/calendar")
t = next((x for x in cal["tasks"] if x["id"] == tid), None)
check("task listed with Turkish text", t and t["title"] == "Asansör firmasını ara" and t["kind"] == "bakim", t)
check("mark task done", call("/api/calendar/task/done", "admin", {"id": tid, "done": True})[0] == 200)
c, cal = call("/api/calendar")
check("task shows done", next(x for x in cal["tasks"] if x["id"] == tid)["done"] is True)
check("task rejects bad date", call("/api/calendar/task", "admin", {"date": "05.12.2026", "title": "x"})[0] == 400)
check("task needs a title", call("/api/calendar/task", "admin", {"date": f"{year}-12-06"})[0] == 400)

check("mark duty done", call("/api/calendar/duty", "admin", {"key": "asansor-01", "year": year, "done": True})[0] == 200)
c, cal = call("/api/calendar")
check("duty done stored", next(x for x in cal["duties"] if x["key"] == "asansor-01")["done"] is True)
check("unknown duty refused", call("/api/calendar/duty", "admin", {"key": "yok", "year": year, "done": True})[0] == 400)

c, ct = call("/api/contracts")
sts = {x["status"] for x in ct["contracts"]}
check("demo contracts cover valid/soon/expired", {"valid", "soon", "expired"} <= sts, sts)

doc_text = "Adalya demo belgesi — ığüşöç İI"
doc_b64 = base64.b64encode(doc_text.encode("utf-8")).decode()
c, d = call("/api/documents", "admin", {"name": "Test belgesi", "kind": "sozlesme", "note": "deneme",
                                        "filename": "test-belgesi.txt", "data": "data:text/plain;base64," + doc_b64})
check("document uploaded", c == 200 and d.get("id"), d)
did = d["id"]
c, docs = call("/api/documents")
doc = next((x for x in docs["documents"] if x["id"] == did), None)
check("document listed with size", doc and doc["size"] == len(doc_text.encode("utf-8")) and doc["filename"].endswith(".txt"), doc)
c, raw = call(f"/api/documents/file?id={did}")
check("document downloads identical bytes", c == 200 and raw == doc_text.encode("utf-8"), c)
check("executable rejected", call("/api/documents", "admin", {"filename": "virus.exe", "data": "aGk="})[0] == 400)
check("fake pdf rejected", call("/api/documents", "admin", {"filename": "sahte.pdf", "data": "aGk="})[0] == 400)

c, saved = call("/api/contracts/save", "admin", {"name": "Test bakım sözleşmesi", "kind": "asansor", "provider": "Örnek A.Ş.",
                                                 "start_date": f"{year}-01-01", "end_date": f"{year}-12-31",
                                                 "amount": "1.500,50", "note": "deneme", "doc_id": did})
check("contract saved", c == 200 and saved.get("id"), saved)
cid = saved["id"]
c, ct = call("/api/contracts")
con = next(x for x in ct["contracts"] if x["id"] == cid)
check("contract amount parsed and doc linked", con["amount"] == 1500.5 and con["doc_id"] == did and con["doc_name"] == "Test belgesi", con)
check("contract end before start refused", call("/api/contracts/save", "admin", {"name": "x", "start_date": f"{year}-06-01", "end_date": f"{year}-01-01"})[0] == 400)
check("contract needs a name", call("/api/contracts/save", "admin", {"kind": "sigorta"})[0] == 400)

c, al = call("/api/alerts")
check("alerts summarise duties/tasks/contracts", c == 200 and set(al["counts"]) == {"duties", "tasks", "contracts"}, al.get("counts"))

check("document delete clears contract link", call("/api/documents/delete", "admin", {"id": did})[0] == 200)
c, ct = call("/api/contracts")
check("contract keeps but loses doc", next(x for x in ct["contracts"] if x["id"] == cid)["doc_id"] is None)
check("contract delete", call("/api/contracts/delete", "admin", {"id": cid})[0] == 200)
c, ct = call("/api/contracts")
check("contract gone", not any(x["id"] == cid for x in ct["contracts"]))

# ---------------------------------------------------------------- announcements, guest passes, packages
check("staff cannot create announcement", call("/api/announcements", "staff", {"title": "x", "body": "y"})[0] == 403)
c, an = call("/api/announcements", "resident")
check("resident sees demo announcements", c == 200 and len(an["announcements"]) >= 3, len(an.get("announcements", [])))
c, an = call("/api/announcements", "admin")
check("admin sees read counts", "read_count" in an["announcements"][0], an["announcements"][0])
check("announcement created", call("/api/announcements", "admin", {"title": "Test duyuru", "body": "Su kesintisi", "kind": "ariza", "pinned": True})[0] == 200)
c, an = call("/api/announcements", "resident")
new = next((a for a in an["announcements"] if a["title"] == "Test duyuru"), None)
check("new announcement unread and pinned", new and new["read"] is False and new["pinned"] is True, new)
check("resident marks read", call("/api/announcements/read", "resident", {"id": new["id"]})[0] == 200)
c, an = call("/api/announcements", "admin")
check("read receipt recorded", next(a for a in an["announcements"] if a["id"] == new["id"])["read_count"] >= 1)
check("announcement title required", call("/api/announcements", "admin", {"body": "x"})[0] == 400)
check("delete announcement", call("/api/announcements/delete", "admin", {"id": new["id"]})[0] == 200)

c, gp = call("/api/guest-passes", "resident")
check("resident guest passes listed", c == 200 and isinstance(gp["passes"], list))
check("resident creates guest pass", call("/api/guest-passes", "resident", {"name": "Test misafir", "plate": "07 tst 9", "date": "2026-11-18", "from": "00:00", "to": "23:59"})[0] == 200)
c, gp = call("/api/guest-passes", "resident")
p = next((x for x in gp["passes"] if x["name"] == "Test misafir"), None)
check("pass plate uppercased and active", p and p["plate"] == "07 TST 9" and p["status"] == "active", p)
check("pass rejects bad time", call("/api/guest-passes", "resident", {"name": "x", "from": "25:00", "to": "26:00"})[0] == 400)
c, gp2 = call("/api/guest-passes", "staff")
check("gate sees today's passes", c == 200 and any(x["id"] == p["id"] for x in gp2["passes"]))
check("gate checks pass in", call("/api/guest-passes/use", "staff", {"id": p["id"]})[0] == 200)
c, gp = call("/api/guest-passes", "resident")
check("used pass marked", next(x for x in gp["passes"] if x["id"] == p["id"])["status"] == "used")
check("reuse refused", call("/api/guest-passes/use", "staff", {"id": p["id"]})[0] == 400)
check("delete pass", call("/api/guest-passes/delete", "admin", {"id": p["id"]})[0] == 200)

check("staff adds package", call("/api/packages", "staff", {"flat_id": 1, "courier": "Test kargo", "note": "1 paket"})[0] == 200)
c, pk = call("/api/packages", "resident")
check("resident sees own package", any(x["courier"] == "Test kargo" for x in pk["packages"]))
c, pk = call("/api/packages", "staff")
k = next(x for x in pk["packages"] if x["courier"] == "Test kargo")
check("staff delivers package", call("/api/packages/out", "staff", {"id": k["id"]})[0] == 200)
check("resident cannot add package", call("/api/packages", "resident", {"flat_id": 1, "courier": "x"})[0] == 403)

# ---------------------------------------------------------------- requests with category and photo, search, backups
png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
check("resident request with category and photo",
      call("/api/my/request", "resident", {"text": "Asansör arızası test", "category": "ariza",
                                           "photo": "data:image/png;base64," + png})[0] == 200)
c, rq = call("/api/requests")
q = next((x for x in rq["requests"] if x["text"] == "Asansör arızası test"), None)
check("request category and photo saved", q and q["category"] == "ariza" and q["has_photo"] is True, q)
c, img = call(f"/api/requests/photo?id={q['id']}")
check("request photo served", c == 200 and img[:4] == b"\x89PNG", c)
check("staff cannot read request photo", call(f"/api/requests/photo?id={q['id']}", "staff")[0] == 403)
check("request rejects fake photo", call("/api/my/request", "resident", {"text": "x", "photo": "data:image/png;base64,aGk="})[0] == 400)

c, sr = call("/api/search?q=" + urllib.request.quote("Yılmaz"))
check("search finds people", c == 200 and any(x["type"] == "person" for x in sr["results"]), sr.get("results"))
c, sv = call("/api/search?q=07")
check("search finds plates", c == 200 and any(x["type"] == "vehicle" for x in sv["results"]), sv.get("results"))
check("resident cannot use global search", call("/api/search?q=" + urllib.request.quote("Yılmaz"), "resident")[0] == 403)

c, au = call("/api/audit?action=Duyuru")
check("audit filters by action", c == 200 and len(au["audit"]) > 0 and all(a["action"] == "Duyuru" for a in au["audit"]), len(au.get("audit", [])))
c, csvd = call("/api/audit.csv?action=Duyuru")
check("audit csv downloads", c == 200 and b"Zaman" in csvd, c)
check("staff cannot download audit csv", call("/api/audit.csv", "staff")[0] == 403)

c, bk = call("/api/backups", "admin", {})
check("backup created", c == 200 and bk["name"].endswith(".zip"), bk)
c, bl = call("/api/backups")
check("backup listed", any(b["name"] == bk["name"] for b in bl["backups"]))
c, zdata = call("/api/backups/download?name=" + urllib.request.quote(bk["name"]))
check("backup downloads as zip", c == 200 and zdata[:2] == b"PK", c)
check("backup settings saved", call("/api/backups/settings", "admin", {"auto": False, "hour": 4, "keep": 5})[0] == 200)
check("restore needs confirmation", call("/api/backups/restore", "admin", {"name": bk["name"], "confirm": "yanlis"})[0] == 400)

# ---------------------------------------------------------------- payment link, receipts, bank import
c, du = call("/api/dues")
s = du["settings"]
base_settings = {"mode": s["mode"], "amount": s["amount"], "total": s["total"], "due_day": s["due_day"],
                 "late_pct": s["late_pct"], "iban": s["iban"], "account_name": s["account_name"]}
check("dues settings accepts payment link",
      call("/api/dues/settings", "admin", {**base_settings, "pay_url": "https://bank.example/pay"})[0] == 200)
c, my = call("/api/my", "resident")
check("resident sees payment link", my["dues"]["pay_url"] == "https://bank.example/pay", my["dues"].get("pay_url"))
check("settings rejects non-http payment link",
      call("/api/dues/settings", "admin", {**base_settings, "pay_url": "javascript:alert(1)"})[0] == 400)

check("record payment for receipt", call("/api/dues/pay", "admin", {"flat_ids": [1], "amount": "100", "date": "2026-11-18", "method": "nakit"})[0] == 200)
c, led = call("/api/dues/ledger?flat_id=1")
pay = next((x for x in led["ledger"] if x["type"] == "payment" and x["amount"] == 100), None)
c, rc = call("/api/dues/receipt", "admin", {"payment_id": pay["id"]})
check("receipt issued with serial", c == 200 and len(rc["serial"]) >= 7, rc)
c, page = call("/makbuz?id=" + str(pay["id"]), "admin")
check("receipt page renders", c == 200 and b"Makbuz" in page and rc["serial"].encode() in page, c)
check("resident can open own receipt", call("/makbuz?id=" + str(pay["id"]), "resident")[0] == 200)
check("staff cannot open receipts", call("/makbuz?id=" + str(pay["id"]), "staff")[0] == 403)
check("delete payment removes receipt", call("/api/dues/delete", "admin", {"type": "payment", "id": pay["id"]})[0] == 200)
c, led = call("/api/dues/ledger?flat_id=1")
check("receipt gone with payment", not any(x["type"] == "payment" and x["id"] == pay["id"] for x in led["ledger"]))

bank_csv = "Tarih;Tutar;Açıklama\n18.11.2026;1500,00;A BLOK 1 kasım aidat\n18.11.2026;500,00;07 XYZ 99 plaka\n"
data = "data:text/csv;base64," + base64.b64encode(bank_csv.encode("utf-8")).decode()
c, pv = call("/api/dues/bank/preview", "admin", {"data": data})
check("bank preview parses CSV", c == 200 and pv["count"] == 2, pv)
row0 = pv["rows"][0] if c == 200 and pv["rows"] else {}
check("bank preview matches flat and amount", row0.get("flat_id") == 1 and row0.get("amount") == 1500.0, row0)
c, ap = call("/api/dues/bank/apply", "admin", {"rows": [{"date": "2026-11-18", "amount": 1500, "desc": "A BLOK 1 kasım aidat", "flat_id": 1}]})
check("bank apply records payment", c == 200 and ap["count"] == 1, ap)
check("bank apply rejects empty rows", call("/api/dues/bank/apply", "admin", {"rows": []})[0] == 400)

# ---------------------------------------------------------------- işletme projesi and income/expense
check("staff cannot open budget", call("/api/budget?year=2099", "staff")[0] == 403)
check("budget created", call("/api/budget", "admin", {"year": 2099, "note": "test"})[0] == 200)
check("duplicate budget refused", call("/api/budget", "admin", {"year": 2099})[0] == 400)
c, bp = call("/api/budget?year=2099")
bid = bp["budget"]["id"]
check("budget line gelir", call("/api/budget/line", "admin", {"budget_id": bid, "kind": "gelir", "label": "Kira", "amount": "1000"})[0] == 200)
check("budget line gider", call("/api/budget/line", "admin", {"budget_id": bid, "kind": "gider", "label": "Temizlik", "amount": "6000"})[0] == 200)
c, bp = call("/api/budget?year=2099")
check("budget totals", bp["budget"]["gelir"] == 1000 and bp["budget"]["gider"] == 6000 and bp["budget"]["net"] == 5000, bp["budget"])
shares = sum(f["share"] for f in bp["flats"])
check("flat advances split by arsa", abs(shares - 5000) < 1, shares)
check("budget notify", call("/api/budget/status", "admin", {"id": bid, "action": "notify"})[0] == 200)
c, bp = call("/api/budget?year=2099")
check("objection date set", bp["budget"]["status"] == "bildirildi" and bool(bp["budget"]["objection_until"]), bp["budget"])
check("line locked after notify", call("/api/budget/line", "admin", {"budget_id": bid, "kind": "gider", "label": "x", "amount": 1})[0] == 400)
check("final blocked in objection window", call("/api/budget/status", "admin", {"id": bid, "action": "final"})[0] == 400)
check("final with force works", call("/api/budget/status", "admin", {"id": bid, "action": "final", "force": True})[0] == 200)

check("cash add gider", call("/api/cash", "admin", {"date": "2099-01-10", "kind": "gider", "category": "elektrik", "label": "Elektrik", "amount": "250"})[0] == 200)
check("cash add gelir", call("/api/cash", "admin", {"date": "2099-01-11", "kind": "gelir", "category": "bagis", "label": "Bağış", "amount": "100"})[0] == 200)
c, cash = call("/api/cash?year=2099")
check("cash totals", cash["gelir"] == 100 and cash["gider"] == 250 and cash["net"] == -150, cash)
e = cash["entries"][0]
check("cash delete", call("/api/cash/delete", "admin", {"id": e["id"]})[0] == 200)
c, csvd = call("/api/cash.csv?year=2099")
check("cash csv downloads", c == 200 and b"Tarih" in csvd, c)
check("resident cannot see cash", call("/api/cash?year=2099", "resident")[0] == 403)

# ---------------------------------------------------------------- reminder queue
check("staff cannot see reminders", call("/api/reminders", "staff")[0] == 403)
c, prep = call("/api/reminders/prepare", "admin", {})
check("reminders prepared for overdue flats", c == 200 and prep["count"] > 0, prep)
c, rq = call("/api/reminders")
pending = [x for x in rq["reminders"] if x["status"] == "bekliyor"]
check("queue lists pending with message", bool(pending) and bool(pending[0]["message"]), len(pending))
check("pending reminder has flat and phone field", bool(pending[0]["flat"]) and "phone" in pending[0], pending[0])
c, _ = call("/api/reminders/sent", "admin", {"id": pending[0]["id"]})
check("reminder marked sent", c == 200)
c, rq = call("/api/reminders")
check("sent reminder closed", next(x for x in rq["reminders"] if x["id"] == pending[0]["id"])["status"] == "gonderildi")
check("reminder settings saved", call("/api/reminders/settings", "admin", {"enabled": False, "day": 5})[0] == 200)

# ---------------------------------------------------------------- security: throttle, TOTP, PWA
for _ in range(5):
    call("/api/login", "none", {"username": "throttle.test", "password": "bad"})
c, _ = call("/api/login", "none", {"username": "throttle.test", "password": "bad"})
check("login throttled per user+IP", c == 429, c)
c, _ = call("/api/login", "none", {"username": "other.test", "password": "bad"})
check("throttle does not lock other users", c == 401, c)

c, man = call("/manifest.webmanifest", "none")
check("manifest served", c == 200 and b"standalone" in man, c)
c, sw = call("/sw.js", "none")
check("service worker served", c == 200 and b"adalya-v1" in sw, c)
c, icon = call("/icon.svg", "none")
check("app icon served", c == 200 and b"<svg" in icon, c)

try:
    import sys as _sys, os as _os, base64 as _b64, secrets as _sec
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    import adalya_panel as _ap
    _s = _b64.b32encode(_sec.token_bytes(20)).decode().rstrip("=")
    check("TOTP verifies its own code", _ap.totp_verify(_s, _ap.totp_code(_s)))
    check("TOTP rejects wrong code", not _ap.totp_verify(_s, "000000"))
except Exception as e:
    check("TOTP self-test", False, str(e))

# ---------------------------------------------------------------- registration, approval, invite, devices
c, page = call("/kayit", "none")
check("public registration page served", c == 200 and b"Kay" in page, c)
reg = {"name": "Yeni Sakin", "phone": "0532 999 88 77", "email": "", "blok": "A", "kat": "4", "daire": "7",
       "kind": "kiraci", "vehicles": [{"plate": "07 YEN 77", "model": "Fiat Egea"}], "device_count": 2, "consent": True}
c, rr = call("/api/register", "none", reg)
check("registration accepted with reference", c == 200 and rr.get("ref", "").startswith("K-"), rr)
c, rl = call("/api/registrations")
entry = next((x for x in rl["registrations"] if x["ref"] == rr["ref"]), None)
check("registration listed with kind and car", entry and entry["status"] == "bekliyor" and entry["kind"] == "kiraci"
      and entry["vehicles"][0]["plate"] == "07 YEN 77", entry)
c, ap = call("/api/registrations/approve", "admin", {"id": entry["id"]})
check("approval creates invite", c == 200 and ap["invite_url"].startswith("/davet?token="), ap)
c, us = call("/api/users")
first = next((u for u in us["users"] if u["username"] == ap["username"]), None)
check("new account invited", first and first["status"] == "davetli", first)
tok = ap["invite_url"].split("token=")[1]
c, page2 = call("/davet?token=" + tok, "none")
check("invite page served", c == 200 and b"ifre" in page2, c)
check("invite sets password", call("/api/invite/accept", "none", {"token": tok, "password": "yeni12345"})[0] == 200)
c, us = call("/api/users")
check("account active after invite", any(u["username"] == ap["username"] and u["status"] == "active" for u in us["users"]))
check("device reset", call("/api/users/devices/reset", "admin", {"username": ap["username"]})[0] == 200)

# a flat can hold two accounts; a third needs explicit force
for nm, ph in (("Ikinci Sakin", "0533 111 22 33"), ("Ucuncu Sakin", "0533 444 55 66")):
    c, r2 = call("/api/register", "none", {k: v for k, v in {**reg, "name": nm, "phone": ph, "vehicles": []}.items()})
    c, rl = call("/api/registrations")
    e2 = next(x for x in rl["registrations"] if x["ref"] == r2["ref"])
    if nm == "Ikinci Sakin":
        check("second account on flat allowed", call("/api/registrations/approve", "admin", {"id": e2["id"]})[0] == 200)
    else:
        check("third account on flat refused", call("/api/registrations/approve", "admin", {"id": e2["id"]})[0] == 400)
        check("third account with force allowed", call("/api/registrations/approve", "admin", {"id": e2["id"], "force": True})[0] == 200)

# audit trail
c, au = call("/api/audit")
acts = {a["action"] for a in au["audit"]}
check("audit logs plate lookups, imports, devices", {"Plaka sorgusu", "Sakin listesi içe aktarıldı", "Cihaz eklendi", "Cihaz silindi"} <= acts, acts)
check("audit logs calendar, contracts, documents", {"Takvim", "Sözleşme", "Belge"} <= acts, acts)
check("audit logs announcements, passes, packages", {"Duyuru", "Ziyaretçi izni", "Kargo"} <= acts, acts)

# restoring the backup taken above must leave a working panel
c, rr = call("/api/backups/restore", "admin", {"name": bk["name"], "confirm": "GERI YUKLE"})
check("backup restore succeeds", c == 200, rr)
check("panel still responds after restore", call("/api/status", "admin")[0] == 200)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)

