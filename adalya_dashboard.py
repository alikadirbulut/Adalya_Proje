"""Build / serve the infrastructure-audit centre.

  python adalya_dashboard.py build                 # results/audit -> infrastructure-audit
  python adalya_dashboard.py serve --port 8090     # static server + automatic rebuilds
  python adalya_dashboard.py status
  python adalya_dashboard.py export --pdf          # best-effort PDF via Edge/Chrome

The dashboard itself is plain HTML/CSS/JS with no external libraries: it opens from disk
(file://) as a static snapshot, and gains live refresh only when served by this script.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import audit_store

OUT = audit_store.ROOT
RESULTS = audit_store.RESULTS


def _newest_results_mtime():
    files = glob.glob(os.path.join(RESULTS, "*.json")) + glob.glob(os.path.join(RESULTS, "*.jsonl"))
    return max((os.path.getmtime(f) for f in files), default=0)


def _live_age():
    path = os.path.join(OUT, "dashboard", "live.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc.get("scan_id"), doc.get("generated_at")
    except (OSError, ValueError):
        return None


def cmd_build(args):
    res = audit_store.build(scan_id=args.scan_id, users=args.users)
    print(f"scan {res['scan_id']} · health {res['score']}/100 ({res['grade']}) · {res['alerts']} findings")
    print(f"dashboard: {res['dashboard']}")
    print(f"reports:   {os.path.join(audit_store.REPORTS, 'infrastructure-overview.html')}")
    return res


def cmd_status(args):
    live = _live_age()
    if not live:
        print("No build yet. Run: python adalya_dashboard.py build")
        return
    scan, generated = live
    newest = _newest_results_mtime()
    built = os.path.getmtime(os.path.join(OUT, "dashboard", "live.json"))
    print(f"latest build : {scan} · {generated}")
    print(f"collector    : {'newer data available' if newest > built else 'up to date'}")
    print(f"dashboard    : file:///{os.path.join(OUT, 'dashboard', 'index.html').replace(os.sep, '/')}")
    if newest > built:
        print("rebuild with : python adalya_dashboard.py build")


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        if self.path.endswith((".json", ".html", ".js", ".css")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


def cmd_serve(args):
    if not os.path.exists(os.path.join(OUT, "dashboard", "index.html")):
        cmd_build(args)
    os.chdir(OUT)
    handler = lambda *a, **kw: Handler(*a, directory=OUT, **kw)  # noqa: E731

    if args.every > 0:
        def loop():
            last = _newest_results_mtime()
            while True:
                time.sleep(args.every)
                newest = _newest_results_mtime()
                if newest > last:
                    try:
                        res = audit_store.build(users=args.users)
                        print(f"[{datetime.now():%H:%M:%S}] rebuilt {res['scan_id']} "
                              f"({res['score']}/100, {res['alerts']} findings)")
                        last = newest
                    except Exception as e:  # keep serving even if a build fails
                        print(f"[{datetime.now():%H:%M:%S}] build failed: {e}")
        threading.Thread(target=loop, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}/dashboard/index.html"
    print(f"Dashboard: {url}")
    print("Auto-refresh: " + (f"every {args.every}s" if args.every > 0 else "off") + "  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def _find_browser():
    for name in ("msedge", "chrome", "chromium"):
        path = shutil.which(name)
        if path:
            return path
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(path):
            return path
    return None


def cmd_export(args):
    src = os.path.join(audit_store.EXPORTS, "pdf", "print-report.html")
    if not os.path.exists(src):
        print("Run a build first.")
        return
    if not args.pdf:
        print(f"Print sheet: {src} (open it and use Print → Save as PDF)")
        return
    browser = _find_browser()
    if not browser:
        print("No Edge/Chrome found; open the print sheet and use Print → Save as PDF.")
        return
    scan = (_live_age() or ("scan",))[0]
    dest = os.path.join(audit_store.EXPORTS, "pdf", f"infrastructure-{scan}.pdf")
    profile = os.path.join(os.environ.get("TEMP", "."), "adalya-pdf-profile")
    try:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                        f"--user-data-dir={profile}", f"--print-to-pdf={dest}", f"file:///{src.replace(os.sep, '/')}"],
                       capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"PDF export failed ({type(e).__name__}); use the print sheet instead.")
        return
    if os.path.exists(dest):
        print(f"PDF: {dest}")
    else:
        print("Browser did not produce a PDF; use the print sheet instead.")


def main():
    p = argparse.ArgumentParser(description="Adalya infrastructure dashboard builder")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build processed data, reports and dashboard")
    b.add_argument("--scan-id", default=None)
    b.add_argument("--users", type=int, default=100)
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("serve", help="serve the dashboard and rebuild when new data arrives")
    s.add_argument("--port", type=int, default=8090)
    s.add_argument("--every", type=float, default=60, help="seconds between rebuild checks (0 = off)")
    s.add_argument("--users", type=int, default=100)
    s.set_defaults(func=cmd_serve)

    st = sub.add_parser("status", help="show the latest build and whether new data is waiting")
    st.set_defaults(func=cmd_status)

    e = sub.add_parser("export", help="print sheet or best-effort PDF")
    e.add_argument("--pdf", action="store_true")
    e.set_defaults(func=cmd_export)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
