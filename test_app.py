#!/usr/bin/env python3
"""Acceptance tests for app.py: a real server on a random loopback port, driven
over HTTP. Covers import -> holds -> resolve, persistence across a restart,
refusals, recovery, read-only mode and request checks. Synthetic data only.

Test workspaces are kept, never deleted; run with TMPDIR pointing at a
scratch folder to keep them out of the system temp directory.
"""

import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.parse

import app
import workspace

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "demo_docs")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def workspace_journal(audit, record, meta):
    """A journal as the workspace writes it (with its checksum)."""
    return {"audit": audit, "record": record, "meta": meta,
            "sha256": workspace.journal_digest(audit, record, meta)}


def demo_import_payload():
    manifest = read(os.path.join(DOCS, "manifest.json"))
    sources = {e["file"]: read(os.path.join(DOCS, e["file"])) for e in json.loads(manifest)}
    return {"candidates_name": "demo-candidates.json",
            "candidates": read(os.path.join(HERE, "demo-candidates.json")),
            "manifest": manifest, "sources": sources, "batch": ""}


class Server:
    def __init__(self, ws):
        self.srv, self.app = app.make_server(ws, 0)
        self.port = self.app.port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def request(self, method, path, body=None, headers=None, origin=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or "127.0.0.1:%d" % self.port}
        if origin:
            h["Origin"] = "http://127.0.0.1:%d" % self.port
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read().decode("utf-8")
        conn.close()
        return r.status, dict(r.getheaders()), data

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def import_json(self, payload, token=None, **kw):
        return self.request("POST", "/api/import", json.dumps(payload).encode("utf-8"),
                            {"Content-Type": "application/json",
                             "X-Form-Token": self.app.token if token is None else token}, **kw)

    def resolve(self, hold_id, action, reviewer="analyst-a", note="", force=False, token=None):
        fields = {"token": self.app.token if token is None else token, "action": action,
                  "reviewer": reviewer, "note": note}
        if force:
            fields["force"] = "1"
        return self.request("POST", "/hold/%s" % hold_id,
                            urllib.parse.urlencode(fields).encode("utf-8"),
                            {"Content-Type": "application/x-www-form-urlencoded"})


class AppBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="app-test-")
        self.ws = os.path.join(self.tmp, "ws")
        workspace.init_workspace(self.ws, json.loads(read(os.path.join(HERE, "record.json"))),
                                 json.loads(read(os.path.join(HERE, "registry.json"))))
        self.server = Server(self.ws)

    def tearDown(self):
        self.server.stop()  # workspaces are kept

    def files(self):
        out = {}
        for name in ("record.json", "audit.jsonl", "workspace.json"):
            with open(os.path.join(self.ws, name), "rb") as f:
                out[name] = hashlib.sha256(f.read()).hexdigest()
        return out

    def record_value(self, entity, metric):
        rec = json.loads(read(os.path.join(self.ws, "record.json")))
        return rec["entities"][entity]["metrics"][metric]

    def do_import(self):
        status, _, body = self.server.import_json(demo_import_payload())
        self.assertEqual(status, 200, body)
        return json.loads(body)


class TestImportAndQueue(AppBase):
    def test_import_then_queue_lists_holds(self):
        res = self.do_import()
        self.assertEqual(res, {"ok": True, "redirect": "/?imported=0"})
        status, _, page = self.server.get(res["redirect"])
        self.assertEqual(status, 200)
        self.assertIn("Imported demo-candidates.json: 6 update(s) &middot; 2 applied, "
                      "2 held for review, 2 rejected.", page)
        self.assertIn("Awaiting review (2)", page)
        self.assertIn('href="/hold/D-2"', page)
        self.assertIn('href="/hold/D-3"', page)

    def test_reimport_refused_and_changes_nothing(self):
        self.do_import()
        before = self.files()
        status, _, body = self.server.import_json(demo_import_payload())
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["code"], "duplicate_file")
        self.assertEqual(self.files(), before)

    def test_reimport_without_manifest_still_reported_as_duplicate(self):
        self.do_import()
        payload = dict(demo_import_payload(), manifest=None, sources={})
        status, _, body = self.server.import_json(payload)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["code"], "duplicate_file")

    def test_manifest_source_missing_refused(self):
        payload = demo_import_payload()
        del payload["sources"]["demo2.txt"]
        before = self.files()
        status, _, body = self.server.import_json(payload)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "missing_source")
        self.assertEqual(self.files(), before)

    def test_bad_candidates_json_refused(self):
        payload = dict(demo_import_payload(), candidates="{nope")
        status, _, body = self.server.import_json(payload)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "bad_json")


class TestReviewFlow(AppBase):
    def setUp(self):
        super().setUp()
        self.do_import()

    def test_hold_page_compares_and_highlights_source(self):
        status, _, page = self.server.get("/hold/D-2")
        self.assertEqual(status, 200)
        self.assertIn("84.2%", page)            # current record
        self.assertIn("85%", page)              # D-2 claim
        self.assertIn("88.6%", page)            # D-3 claim, same metric
        self.assertIn("<mark>85.0</mark>", page)  # value found in stored source text
        self.assertIn("<mark>88.6</mark>", page)

    def test_accept_with_note_then_persisted(self):
        status, headers, _ = self.server.resolve("D-2", "accept", note="gov data outranks news")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/hold/D-2?done=R-1")
        self.assertEqual(self.record_value("Halcyon Health", "mlr_pct")["value"], 85.0)
        _, _, page = self.server.get("/hold/D-2?done=R-1")
        self.assertIn("R-1 saved: D-2 accepted, record updated.", page)
        self.assertIn("note: gov data outranks news", page)
        _, _, queue = self.server.get("/")
        self.assertIn("Awaiting review (1)", queue)
        self.assertNotIn('href="/hold/D-2"', queue)

    def test_stale_hold_requires_explicit_override(self):
        self.server.resolve("D-2", "accept")
        _, _, page = self.server.get("/hold/D-3")
        self.assertIn("The record changed after this was held", page)
        self.assertIn('value="accept" disabled', page)
        self.assertIn("Accept anyway", page)
        before = self.files()
        status, _, page = self.server.resolve("D-3", "accept")
        self.assertEqual(status, 409)
        self.assertIn("Record changed since this hold", page)
        status, _, page = self.server.resolve("D-3", "accept_force")  # box not ticked
        self.assertEqual(status, 409)
        status, _, page = self.server.resolve("D-3", "accept_force", force=True)  # no note
        self.assertEqual(status, 400)
        self.assertIn("Note needed", page)
        self.assertEqual(self.files(), before)
        status, _, _ = self.server.resolve("D-3", "accept_force", note="checked filing",
                                           force=True)
        self.assertEqual(status, 303)
        self.assertEqual(self.record_value("Halcyon Health", "mlr_pct")["value"], 88.6)
        audit = workspace.Workspace(self.ws).audit()
        self.assertTrue(audit[-1]["forced_over_change"])
        _, _, page = self.server.get("/hold/D-3")
        self.assertIn("forced over a newer value", page)

    def test_dismiss_leaves_record(self):
        before = self.record_value("Halcyon Health", "mlr_pct")
        status, _, _ = self.server.resolve("D-3", "dismiss")
        self.assertEqual(status, 303)
        self.assertEqual(self.record_value("Halcyon Health", "mlr_pct"), before)

    def test_refusals_render_and_change_nothing(self):
        self.server.resolve("D-3", "dismiss")
        before = self.files()
        cases = [("D-3", "dismiss", "analyst-a", 409, "Already resolved"),
                 ("D-1", "accept", "analyst-a", 409, "Not a hold"),
                 ("D-99", "accept", "analyst-a", 404, "Not found"),
                 ("D-2", "accept", "  ", 400, "Reviewer name needed"),
                 ("D-2", "launch", "analyst-a", 400, "")]
        for hid, action, reviewer, code, text in cases:
            status, _, page = self.server.resolve(hid, action, reviewer=reviewer)
            self.assertEqual(status, code, (hid, action))
            self.assertIn(text, page)
            self.assertNotIn("Traceback", page)
        self.assertEqual(self.files(), before)

    def test_restart_keeps_everything(self):
        self.server.resolve("D-2", "accept", note="kept")
        self.server.stop()
        self.server = Server(self.ws)  # new process state, same workspace
        _, _, queue = self.server.get("/")
        self.assertIn("Awaiting review (1)", queue)
        _, _, page = self.server.get("/hold/D-2")
        self.assertIn("note: kept", page)
        status, _, _ = self.server.resolve("D-2", "dismiss")
        self.assertEqual(status, 409)

    def test_html_in_inputs_is_escaped(self):
        self.server.resolve("D-3", "dismiss", reviewer="<script>x</script>",
                            note="<img src=x onerror=1>")
        _, _, page = self.server.get("/hold/D-3")
        self.assertNotIn("<script>x", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page)

    def test_report_page_served(self):
        status, headers, page = self.server.get("/report")
        self.assertEqual(status, 200)
        self.assertIn("Decision log", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])


class TestRequestChecks(AppBase):
    def test_missing_or_wrong_token_refused(self):
        self.do_import()
        before = self.files()
        status, _, _ = self.server.resolve("D-2", "accept", token="")
        self.assertEqual(status, 403)
        status, _, body = self.server.import_json(demo_import_payload(), token="wrong")
        self.assertEqual(status, 403)
        self.assertEqual(self.files(), before)

    def test_cross_origin_and_foreign_host_refused(self):
        self.do_import()
        before = self.files()
        status, _, _ = self.server.request(
            "POST", "/hold/D-2", urllib.parse.urlencode(
                {"token": self.server.app.token, "action": "accept",
                 "reviewer": "a"}).encode(),
            {"Content-Type": "application/x-www-form-urlencoded",
             "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.server.resolve("D-2", "accept")  # sanity: same form is fine
        self.assertEqual(status, 303)
        status, _, _ = self.server.get("/", host="evil.example")
        self.assertEqual(status, 403)
        self.assertNotEqual(self.files(), before)

    def test_oversized_body_refused_before_reading(self):
        # Announce a body over the cap and send none: the server must answer
        # 413 from the header alone instead of reading (or waiting for) it.
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        conn.putrequest("POST", "/api/import", skip_host=True)
        conn.putheader("Host", "127.0.0.1:%d" % self.server.port)
        conn.putheader("Origin", "http://127.0.0.1:%d" % self.server.port)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("X-Form-Token", self.server.app.token)
        conn.putheader("Content-Length", str(app.MAX_BODY + 1))
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual(r.status, 413)
        self.assertEqual(json.loads(r.read())["code"], "too_large")
        conn.close()

    def test_security_headers(self):
        _, headers, _ = self.server.get("/")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        # Browsers send "Origin: null" on form posts under no-referrer, which
        # the Origin check refuses; found by tools/browser_check.py.
        self.assertEqual(headers["Referrer-Policy"], "same-origin")

    def test_null_missing_origin_refused_localhost_host_accepted(self):
        self.do_import()
        form = urllib.parse.urlencode({"token": self.server.app.token, "action": "dismiss",
                                       "reviewer": "a"}).encode()
        ctype = {"Content-Type": "application/x-www-form-urlencoded"}
        status, _, _ = self.server.request("POST", "/hold/D-2", form,
                                           dict(ctype, Origin="null"))
        self.assertEqual(status, 403)
        status, _, _ = self.server.request("POST", "/hold/D-2", form, ctype, origin=False)
        self.assertEqual(status, 403)
        host = "localhost:%d" % self.server.port
        status, _, _ = self.server.request("POST", "/hold/D-2", form,
                                           dict(ctype, Origin="http://" + host), host=host,
                                           origin=False)
        self.assertEqual(status, 303)

    def test_odd_tokens_and_queries_get_clean_responses(self):
        self.do_import()
        before = self.files()
        status, _, page = self.server.resolve("D-2", "accept", token="é")
        self.assertEqual(status, 403)
        status, _, body = self.server.import_json(demo_import_payload(), token="é")
        self.assertEqual(status, 403)
        q = "&".join("q%d=1" % i for i in range(12))
        status, _, page = self.server.get("/?" + q)
        self.assertEqual(status, 400)
        for bad in ("%C2%B2", "9" * 5000):       # superscript two; huge number
            status, _, page = self.server.get("/?imported=" + bad)
            self.assertEqual(status, 200)
            self.assertNotIn("Imported ", page)
        self.assertEqual(self.files(), before)

    def test_lone_surrogates_refused_and_never_break_pages(self):
        for field in ("candidates", "batch", "candidates_name"):
            payload = demo_import_payload()
            payload[field] = "\ud800" if field != "candidates" else "[\"\ud800\"]"
            body = json.dumps(payload).encode("utf-8")  # JSON escapes the surrogate
            status, _, resp = self.server.request(
                "POST", "/api/import", body,
                {"Content-Type": "application/json", "X-Form-Token": self.server.app.token})
            self.assertEqual(status, 400, field)
        # A surrogate inside a candidate's own fields is stored (JSON-escaped)
        # and every page still renders.
        cands = json.loads(demo_import_payload()["candidates"])
        cands[5]["entity"] = "Merritt \ud800"
        payload = dict(demo_import_payload(), candidates=json.dumps(cands))
        status, _, _ = self.server.import_json(payload)
        self.assertEqual(status, 200)
        for path in ("/", "/report", "/hold/D-2"):
            status, _, _ = self.server.get(path)
            self.assertEqual(status, 200, path)

    def test_stalled_upload_times_out_without_blocking_forever(self):
        import socket
        old = app.REQUEST_TIMEOUT
        self.server.stop()
        try:
            app.REQUEST_TIMEOUT = 1
            self.server = Server(self.ws)
        finally:
            app.REQUEST_TIMEOUT = old
        s = socket.create_connection(("127.0.0.1", self.server.port))
        s.sendall(("POST /api/import HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: "
                   "http://127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                   "Content-Length: 100\r\n\r\n" % (self.server.port, self.server.port)
                   ).encode())
        status, _, _ = self.server.get("/app.css")  # served once the stall times out
        self.assertEqual(status, 200)
        s.close()

    def test_trickled_upload_abandoned_after_total_deadline(self):
        import socket
        import time as _time
        old = app.REQUEST_TIMEOUT
        self.server.stop()
        try:
            app.REQUEST_TIMEOUT = 1
            self.server = Server(self.ws)
        finally:
            app.REQUEST_TIMEOUT = old
        s = socket.create_connection(("127.0.0.1", self.server.port))
        s.sendall(("POST /api/import HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: "
                   "http://127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                   "Content-Length: 100\r\n\r\n" % (self.server.port, self.server.port)
                   ).encode())
        start = _time.monotonic()
        reply = b""
        s.settimeout(0.3)
        while _time.monotonic() - start < 4 and b"\r\n\r\n" not in reply:
            try:
                s.sendall(b"x")            # one byte every 0.3 s: each read succeeds
            except OSError:
                break
            try:
                reply += s.recv(4096)
            except OSError:
                pass
        elapsed = _time.monotonic() - start
        s.close()
        self.assertIn(b" 408 ", reply.split(b"\r\n")[0])
        self.assertLess(elapsed, 3)

    def test_surrogates_in_manifest_batch_and_source_names_refused(self):
        p1 = demo_import_payload()
        man = json.loads(p1["manifest"])
        man[0]["batch"] = "\ud800"
        p1["manifest"] = json.dumps(man)
        p2 = demo_import_payload()
        p2["sources"]["\ud800.txt"] = "x"
        for payload in (p1, p2):
            status, _, _ = self.server.import_json(payload)
            self.assertEqual(status, 400)

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.srv.server_address[0], "127.0.0.1")


class TestConnectionHandling(AppBase):
    def restart_with_timeout(self, seconds):
        old = app.REQUEST_TIMEOUT
        self.server.stop()
        try:
            app.REQUEST_TIMEOUT = seconds
            self.server = Server(self.ws)
        finally:
            app.REQUEST_TIMEOUT = old

    def timed_get(self):
        start = time.monotonic()
        status, _, _ = self.server.get("/app.css")
        return status, time.monotonic() - start

    def test_trickled_headers_cannot_stall_the_app(self):
        self.restart_with_timeout(1)
        s = socket.create_connection(("127.0.0.1", self.server.port))
        stop = threading.Event()

        def trickle():
            # Request line at once, then one header byte at a time: the header
            # phase itself must be bounded in total, not per line or per read.
            try:
                s.sendall(b"GET / HTTP/1.1\r\n")
            except OSError:
                return
            for b in b"Host: 127.0.0.1\r\nX-Slow: " + b"a" * 200:
                if stop.is_set():
                    return
                try:
                    s.sendall(bytes([b]))
                except OSError:
                    return
                time.sleep(0.05)
        t = threading.Thread(target=trickle, daemon=True)
        t.start()
        time.sleep(0.3)
        status, waited = self.timed_get()   # queued behind the trickler
        stop.set()
        s.close()
        self.assertEqual(status, 200)
        self.assertLess(waited, 2.5)

    def test_idle_connection_cannot_stall_the_app(self):
        self.restart_with_timeout(1)
        s = socket.create_connection(("127.0.0.1", self.server.port))  # sends nothing
        time.sleep(0.2)
        status, waited = self.timed_get()
        s.close()
        self.assertEqual(status, 200)
        self.assertLess(waited, 2.5)

    def test_reset_mid_upload_is_labelled_and_logged_without_traceback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = socket.create_connection(("127.0.0.1", self.server.port))
            s.sendall(("POST /api/import HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: "
                       "http://127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                       "Content-Length: 1000\r\n\r\n{\"x\": " % (self.server.port,
                                                               self.server.port)).encode())
            time.sleep(0.2)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()                       # RST: the client vanished mid-body
            time.sleep(0.5)
            status, _, _ = self.server.get("/")
        self.assertEqual(status, 200)
        log = err.getvalue()
        self.assertNotIn("Traceback", log)
        self.assertNotIn(" 408 ", log)     # not mislabelled as a timeout
        self.assertNotIn(" 500 ", log)
        self.assertIn("connection dropped by client", log)

    def test_client_vanishing_mid_reply_logs_one_line(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(20):
                s = socket.create_connection(("127.0.0.1", self.server.port))
                try:
                    s.sendall(("GET /report HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                               % self.server.port).encode())
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                 struct.pack("ii", 1, 0))
                except OSError:
                    pass  # the client side of a burst may itself be reset; not under test
                s.close()
                time.sleep(0.02)
            time.sleep(0.5)
            status, _, _ = self.server.get("/")
        self.assertEqual(status, 200)
        log = err.getvalue()
        self.assertNotIn("Traceback", log)
        self.assertNotIn("unexpected error", log)
        self.assertNotIn(" 500 ", log)

    def test_recovery_listeners_do_not_accumulate(self):
        n = len(workspace.RECOVERY_LISTENERS)
        for _ in range(3):
            extra = Server(self.ws)
            extra.stop()
        self.assertEqual(len(workspace.RECOVERY_LISTENERS), n)


class TestRecoveryAndReadOnly(AppBase):
    def test_interrupted_change_completed_and_announced_once(self):
        self.do_import()
        self.server.stop()
        ws = workspace.Workspace(self.ws)
        meta = ws.meta()
        record = ws.record()
        record["entities"]["Halcyon Health"]["metrics"]["mlr_pct"]["value"] = 85.0
        entry = {"id": "R-1", "type": "resolution", "resolves": "D-2", "action": "accept",
                 "reviewer": "a", "note": "", "entity": "Halcyon Health",
                 "metric": "mlr_pct", "claimed_value": 85.0, "unit": "percent",
                 "ts": "2026-11-25T12:00:00"}
        meta["next_resolution"] = 2
        with open(os.path.join(self.ws, "txn.json"), "w") as f:
            json.dump(workspace_journal([entry], record, meta), f)
        self.server = Server(self.ws)
        _, _, first = self.server.get("/")
        _, _, second = self.server.get("/")
        self.assertIn("was interrupted; it was completed", first)
        self.assertNotIn("was interrupted", second)
        self.assertIn("Awaiting review (1)", first)
        self.assertEqual(self.record_value("Halcyon Health", "mlr_pct")["value"], 85.0)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "txn.json")))

    def plant_journal(self):
        ws = workspace.Workspace(self.ws)
        meta = ws.meta()
        record = ws.record()
        record["entities"]["Halcyon Health"]["metrics"]["mlr_pct"]["value"] = 85.0
        entry = {"id": "R-1", "type": "resolution", "resolves": "D-2", "action": "accept",
                 "reviewer": "a", "note": "", "entity": "Halcyon Health",
                 "metric": "mlr_pct", "claimed_value": 85.0, "unit": "percent",
                 "ts": "2026-11-25T12:00:00"}
        meta["next_resolution"] = 2
        with open(os.path.join(self.ws, "txn.json"), "w") as f:
            json.dump(workspace_journal([entry], record, meta), f)

    def test_recovery_notice_survives_post_and_head(self):
        self.do_import()
        self.plant_journal()
        status, _, _ = self.server.resolve("D-3", "dismiss")   # first request is a POST
        self.assertEqual(status, 303)
        status, _, _ = self.server.request("HEAD", "/")
        self.assertEqual(status, 200)
        _, _, page = self.server.get("/")
        self.assertIn("was interrupted; it was completed", page)

    def test_recovery_notice_survives_head_first(self):
        self.do_import()
        self.plant_journal()
        status, _, _ = self.server.request("HEAD", "/")   # HEAD triggers the recovery
        self.assertEqual(status, 200)
        _, _, page = self.server.get("/")
        self.assertIn("was interrupted; it was completed", page)

    def test_recovery_notice_survives_refused_post_first(self):
        self.do_import()
        self.plant_journal()                               # the journal resolves D-2
        status, _, page = self.server.resolve("D-2", "dismiss")  # refused: already resolved
        self.assertEqual(status, 409)
        # The refusal page is a full hold page, so the notice is shown right there.
        self.assertIn("was interrupted; it was completed", page)
        self.assertIn("Already resolved", page)

    def test_recovery_notice_survives_refused_import_first(self):
        self.do_import()
        self.plant_journal()
        status, _, _ = self.server.import_json(demo_import_payload())  # 409 duplicate (JSON)
        self.assertEqual(status, 409)
        _, _, page = self.server.get("/")   # a JSON reply has no page, so it waits
        self.assertIn("was interrupted; it was completed", page)

    def test_wrong_shape_journal_is_read_only(self):
        self.do_import()
        for content in ("[]", "{}", "null", '{"audit": 1, "meta": {}}'):
            with open(os.path.join(self.ws, "txn.json"), "w") as f:
                f.write(content)
            before = self.files()
            status, _, page = self.server.get("/")
            self.assertEqual(status, 503, content)
            self.assertIn("change journal", page)
            self.assertEqual(self.files(), before)

    def test_torn_journal_is_read_only_not_an_upload_error(self):
        self.do_import()
        with open(os.path.join(self.ws, "txn.json"), "w") as f:
            f.write("{torn")
        before = self.files()
        status, _, page = self.server.get("/")
        self.assertEqual(status, 503)
        self.assertIn("change journal", page)
        self.assertIn("read-only until this is fixed", page)
        self.assertEqual(self.files(), before)
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".lock")))

    def test_corrupt_audit_is_read_only(self):
        self.do_import()
        with open(os.path.join(self.ws, "audit.jsonl"), "a") as f:
            f.write("{torn\n")
        before = self.files()
        status, _, page = self.server.get("/")
        self.assertEqual(status, 503)
        self.assertIn("read-only until this is fixed", page)
        status, _, _ = self.server.resolve("D-2", "accept")
        self.assertEqual(status, 503)
        self.assertEqual(self.files(), before)

    def test_locked_workspace_refuses_politely(self):
        self.do_import()
        before = self.files()
        with workspace.Workspace(self.ws):
            status, _, page = self.server.resolve("D-2", "accept")
            self.assertEqual(status, 409)
            self.assertIn("Workspace busy", page)
            status, _, page = self.server.get("/")
            self.assertEqual(status, 409)
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
