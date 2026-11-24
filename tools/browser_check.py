#!/usr/bin/env python3
"""End-to-end check of app.py in a real (headless) Chrome. Standard library only.

Drives the browser over the DevTools protocol on a pipe (no network port):
import the demo files through the Import form's file inputs, review the
conflict, accept with a note, refuse a stale accept, override it explicitly,
refuse a duplicate import, restart the app server, and confirm everything
persisted. Captures desktop (1280px) and true mobile (390px) screenshots of
each step and fails on any unmet expectation.

Everything it writes goes under OUTDIR: the workspace, the Chrome profile,
screenshots and a JSON log. It deletes nothing.

  python3 tools/browser_check.py OUTDIR [--chrome PATH]

Needs a local Chrome/Chromium. Synthetic demo data only.
"""

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(REPO, "demo_docs")
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VIEWPORTS = {"desktop": (1280, 900, False), "mobile390": (390, 844, True)}


class CDP:
    """Minimal DevTools client over --remote-debugging-pipe (fds 3 and 4)."""

    def __init__(self, chrome, profile, env):
        to_chrome_r, self._to_chrome_w = os.pipe()
        self._from_chrome_r, from_chrome_w = os.pipe()

        def setup():  # runs in the child: Chrome reads fd 3, writes fd 4
            os.dup2(to_chrome_r, 3)
            os.dup2(from_chrome_w, 4)

        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--remote-debugging-pipe", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--disable-sync",
             "--disable-background-networking", "--disable-component-update",
             "--disable-default-apps", "--disable-features=Translate,OptimizationHints,"
             "MediaRouter,AutofillServerCommunication", "--metrics-recording-only",
             "--hide-scrollbars", "--user-data-dir=" + profile, "about:blank"],
            pass_fds=(3, 4), preexec_fn=setup, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._id = 0
        self._pending = {}
        self._events = []
        self._cv = threading.Condition()
        threading.Thread(target=self._reader, daemon=True).start()
        self.session = None

    def _reader(self):
        buf = b""
        while True:
            chunk = os.read(self._from_chrome_r, 1 << 16)
            if not chunk:
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                with self._cv:
                    if "id" in msg:
                        self._pending[msg["id"]] = msg
                    else:
                        self._events.append(msg)
                    self._cv.notify_all()

    def send(self, method, params=None, session=True, timeout=30):
        with self._cv:
            self._id += 1
            mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session and self.session:
            msg["sessionId"] = self.session
        os.write(self._to_chrome_w, json.dumps(msg).encode() + b"\0")
        deadline = time.time() + timeout
        with self._cv:
            while mid not in self._pending:
                if not self._cv.wait(deadline - time.time()):
                    raise RuntimeError("timeout waiting for %s" % method)
            res = self._pending.pop(mid)
        if "error" in res:
            raise RuntimeError("%s failed: %s" % (method, res["error"]))
        return res.get("result", {})

    def wait_event(self, method, timeout=30):
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for i, e in enumerate(self._events):
                    if e.get("method") == method:
                        return self._events.pop(i)
                if not self._cv.wait(deadline - time.time()):
                    raise RuntimeError("timeout waiting for event %s" % method)

    def clear_events(self):
        with self._cv:
            self._events.clear()

    def attach(self):
        targets = self.send("Target.getTargets", session=False)["targetInfos"]
        page = next(t for t in targets if t["type"] == "page")
        self.session = self.send("Target.attachToTarget",
                                 {"targetId": page["targetId"], "flatten": True},
                                 session=False)["sessionId"]
        self.send("Page.enable")
        self.send("Runtime.enable")

    def close(self):
        try:
            self.send("Browser.close", session=False, timeout=5)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()


class Check:
    def __init__(self, outdir, chrome):
        self.outdir = outdir
        self.chrome = chrome
        self.log = []
        self.failures = []
        self.ws = os.path.join(outdir, "workspace")
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.env = dict(os.environ, TMPDIR=os.path.join(outdir, "tmp"),
                        PYTHONDONTWRITEBYTECODE="1")
        os.makedirs(self.env["TMPDIR"], exist_ok=True)
        os.makedirs(os.path.join(outdir, "shots"), exist_ok=True)
        self.server = None
        self.shot_n = 0

    # -- app server --
    def start_server(self, label):
        logf = open(os.path.join(self.outdir, "server-%s.log" % label), "w")
        self.server = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "app.py"), self.ws, "--port", str(self.port),
             "--init"], env=self.env, stdout=logf, stderr=logf)
        for _ in range(100):
            try:
                urllib.request.urlopen(self.base + "/app.css", timeout=1).read()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("app server did not start")

    def stop_server(self):
        self.server.terminate()
        self.server.wait(timeout=10)

    # -- browser helpers --
    def go(self, path):
        self.cdp.clear_events()
        self.cdp.send("Page.navigate", {"url": self.base + path})
        self.cdp.wait_event("Page.loadEventFired")

    def js(self, expr):
        r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                                "awaitPromise": True})
        if r.get("exceptionDetails"):
            raise RuntimeError("JS error in %r: %s" % (expr, r["exceptionDetails"]))
        return r["result"].get("value")

    def click_and_wait(self, selector):
        self.cdp.clear_events()
        self.js("document.querySelector(%s).click()" % json.dumps(selector))
        self.cdp.wait_event("Page.loadEventFired")

    def fill(self, selector, value):
        self.js("(function(){var e=document.querySelector(%s);e.value=%s;"
                "e.dispatchEvent(new Event('change'));})()"
                % (json.dumps(selector), json.dumps(value)))

    def set_files(self, selector, paths):
        doc = self.cdp.send("DOM.getDocument", {"depth": 0})
        node = self.cdp.send("DOM.querySelector", {"nodeId": doc["root"]["nodeId"],
                                                   "selector": selector})
        self.cdp.send("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": paths})

    def text(self):
        return self.js("document.body.innerText")

    def expect(self, step, cond, detail):
        self.log.append({"step": step, "ok": bool(cond), "detail": detail,
                         "url": self.js("location.href")})
        if not cond:
            self.failures.append("%s: %s" % (step, detail))

    def shoot(self, name):
        """Full-page screenshots at each viewport (true device metrics)."""
        self.shot_n += 1
        files = []
        for vp, (w, h, mobile) in VIEWPORTS.items():
            self.cdp.send("Emulation.setDeviceMetricsOverride",
                          {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": mobile})
            time.sleep(0.15)
            metrics = self.cdp.send("Page.getLayoutMetrics")["cssContentSize"]
            overflow = self.js("document.documentElement.scrollWidth > "
                               "document.documentElement.clientWidth")
            shot = self.cdp.send("Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": w,
                         "height": max(h, int(metrics["height"])), "scale": 1}})
            path = os.path.join(self.outdir, "shots",
                                "%02d-%s-%s.png" % (self.shot_n, name, vp))
            with open(path, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            files.append(path)
            self.expect("%s@%s" % (name, vp), not overflow,
                        "no horizontal overflow (scrollWidth <= clientWidth)")
        self.cdp.send("Emulation.setDeviceMetricsOverride",
                      {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        return files

    # -- the flow --
    def run(self):
        self.start_server("first")
        self.cdp = CDP(self.chrome, os.path.join(self.outdir, "chrome-profile"), self.env)
        try:
            self.cdp.attach()
            self.flow()
        finally:
            self.cdp.close()
            self.stop_server()
        with open(os.path.join(self.outdir, "browser-check.json"), "w") as f:
            json.dump({"base": self.base, "steps": self.log, "failures": self.failures},
                      f, indent=2)
        return not self.failures

    def flow(self):
        manifest = os.path.join(DOCS, "manifest.json")
        sources = [os.path.join(DOCS, e["file"]) for e in json.load(open(manifest))]
        cands = os.path.join(REPO, "demo-candidates.json")

        self.go("/")
        self.expect("empty queue", "Nothing waiting for review." in self.text(),
                    "fresh workspace has an empty queue")
        self.shoot("empty-queue")

        # 1. import through the real file inputs
        self.go("/import")
        self.set_files("#f-c", [cands])
        self.set_files("#f-m", [manifest])
        self.set_files("#f-s", sources)
        self.shoot("import-form")
        self.click_and_wait("#import-form button[type=submit]")
        t = self.text()
        self.expect("import", "2 applied, 2 held for review, 2 rejected" in t
                    and "Awaiting review (2)" in t, "queue shows the import result")
        self.shoot("queue-after-import")

        # 2. compare sources on the hold page
        self.go("/hold/D-2")
        t = self.text()
        self.expect("compare", "84.2%" in t and "85%" in t and "88.6%" in t,
                    "current record and both claims shown")
        marks = self.js("Array.from(document.querySelectorAll('mark')).map(m=>m.textContent)")
        self.expect("highlight", marks == ["85.0", "88.6"], "claimed values highlighted: %r"
                    % marks)
        self.shoot("hold-compare")

        # 3. accept D-2 with a note
        self.fill("#r-D-2", "analyst-a")
        self.fill("#n-D-2", "Government dataset outranks the news brief")
        self.click_and_wait("#D-2 button[value=accept]")
        t = self.text()
        self.expect("accept", "R-1 saved: D-2 accepted, record updated." in t,
                    "accept confirmed on the page")
        self.shoot("accepted")

        # 4. stale hold: plain accept disabled; unticked override refused
        self.go("/hold/D-3")
        disabled = self.js("document.querySelector('#D-3 button[value=accept]').disabled")
        self.expect("stale", disabled and "The record changed after this was held"
                    in self.text(), "accept disabled, change explained")
        self.shoot("stale-hold")
        self.fill("#r-D-3", "analyst-a")
        self.click_and_wait("#D-3 button[value=accept_force]")
        t = self.text()
        self.expect("override refused", "Tick the box to confirm" in t,
                    "unticked override refused with a reason")
        self.shoot("override-refused")

        # 5. explicit override with a note
        self.fill("#r-D-3", "analyst-a")
        self.js("document.querySelector('#D-3 input[name=force]').checked=true")
        self.fill("#n-D-3", "Checked the filing; the news figure is the restated one")
        self.click_and_wait("#D-3 button[value=accept_force]")
        t = self.text()
        self.expect("override", "R-2 saved: D-3 accepted" in t
                    and "forced over a newer value" in t, "forced accept saved and labelled")
        self.shoot("override-saved")

        # 6. duplicate import refused in the page
        self.go("/import")
        self.set_files("#f-c", [cands])
        self.js("document.querySelector('#import-form button[type=submit]').click()")
        for _ in range(50):
            msg = self.js("document.getElementById('import-result').textContent")
            if msg and msg.startswith("Not imported"):
                break
            time.sleep(0.1)
        self.expect("duplicate import", "already ingested" in (msg or ""),
                    "duplicate refused: %r" % msg)
        self.shoot("duplicate-refused")

        # 7. restart the app server; everything persists
        self.stop_server()
        self.start_server("second")
        self.go("/")
        t = self.text()
        self.expect("restart queue", "Nothing waiting for review." in t
                    and "demo-candidates.json" in t, "after restart: queue empty, import listed")
        self.shoot("after-restart-queue")
        self.go("/hold/D-3")
        t = self.text()
        self.expect("restart hold", "Accepted R-2 by analyst-a" in t
                    and "note: Checked the filing" in t, "resolution persisted with note")
        self.shoot("after-restart-hold")
        self.go("/report")
        t = self.text()
        self.expect("report", "Decision log" in t and "88.6%" in t, "report reflects record")
        self.shoot("report")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("outdir", help="new, empty folder for all output")
    ap.add_argument("--chrome", default=DEFAULT_CHROME)
    args = ap.parse_args(argv)
    if os.path.exists(args.outdir) and os.listdir(args.outdir):
        print("error: %s must be new or empty (nothing is overwritten)" % args.outdir,
              file=sys.stderr)
        return 2
    if not os.path.exists(args.chrome):
        print("error: Chrome not found at %s (use --chrome)" % args.chrome, file=sys.stderr)
        return 2
    os.makedirs(args.outdir, exist_ok=True)
    check = Check(os.path.abspath(args.outdir), args.chrome)
    ok = check.run()
    for s in check.log:
        print("%s %s - %s" % ("ok  " if s["ok"] else "FAIL", s["step"], s["detail"]))
    print("%d checks, %d failed" % (len(check.log), len(check.failures)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
