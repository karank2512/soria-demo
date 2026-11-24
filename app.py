#!/usr/bin/env python3
"""Reconciliation Workbench: a local browser app over a review workspace.

Standard library only. Serves one workspace (see workspace.py) on 127.0.0.1:

  /             review queue: open holds, counts, import history
  /hold/D-n     side-by-side comparison of a held update against the current
                record and its source text; accept or dismiss with a note
  /import       load a candidates file, an optional manifest and source texts
  /report       the full static review page (record, rejections, decision log)

The server keeps no state of its own between requests: every request opens
the workspace under its lock, so the files on disk are the only truth and a
restart (or a crash) loses nothing that was confirmed. An interrupted change
is completed when the workspace is next opened, and the queue says so.

Local single-user tool: it binds to loopback only and rejects requests whose
Host/Origin is not this server or that lack the per-process form token. There
are no accounts, no access control between local users, and no
tamper-proofing.

Usage:
  python3 app.py WORKSPACE [--port 8765] [--init]
"""

import argparse
import html
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import reconcile  # noqa: E402
import review_page  # noqa: E402
import workspace  # noqa: E402
from workspace import WorkspaceError  # noqa: E402

PRODUCT = "Reconciliation Workbench"
MAX_BODY = 5 * 1024 * 1024
REQUEST_TIMEOUT = 10  # seconds; a stalled client cannot hold the app longer

STATUS = {
    "locked": 409, "already_resolved": 409, "record_changed": 409,
    "duplicate_file": 409, "duplicate_batch": 409, "not_a_hold": 409,
    "incomplete": 409, "unknown_id": 404, "not_found_page": 404,
    "no_reviewer": 400, "bad_action": 400, "bad_json": 400, "not_list": 400,
    "unbatched": 400, "bad_manifest": 400, "missing_source": 400,
    "note_required": 400, "bad_request": 400,
    "forbidden": 403, "too_large": 413,
    "corrupt_audit": 503, "corrupt_journal": 503, "schema": 503, "timeout": 408, "not_workspace": 503,
    "not_found": 503, "unreadable": 503,
}

TITLE = {
    "locked": "Workspace busy",
    "already_resolved": "Already resolved",
    "record_changed": "Record changed since this hold",
    "duplicate_file": "Already imported",
    "duplicate_batch": "Batch already imported",
    "not_a_hold": "Not a hold",
    "unknown_id": "Not found",
    "not_found_page": "Not found",
    "no_reviewer": "Reviewer name needed",
    "note_required": "Note needed",
    "bad_json": "File is not valid JSON",
    "not_list": "Wrong file shape",
    "unbatched": "Batch id needed",
    "bad_manifest": "Manifest problem",
    "missing_source": "Source file missing",
    "forbidden": "Request refused",
    "too_large": "Upload too large",
    "corrupt_audit": "Workspace needs attention",
    "corrupt_journal": "Workspace needs attention",
    "timeout": "Request timed out",
    "schema": "Workspace needs attention",
    "not_workspace": "Workspace not found",
}

# Read-only mode: the workspace can be read but not safely written.
READ_ONLY_CODES = {"corrupt_audit", "corrupt_journal", "schema", "not_workspace", "not_found", "unreadable"}

APP_CSS = """
.wordmark a{color:inherit;text-decoration:none}
nav a.on{color:#e8ebe7}
.notice{border:1px solid var(--hairline);background:var(--surface);border-radius:3px;
  padding:10px 14px;font-size:13.5px;margin:0 0 16px}
.notice.ok{border-color:#b9d6c6;background:var(--applied-bg);color:var(--applied)}
.notice.warn{border-color:#e6d3a3;background:var(--held-bg);color:var(--held)}
.notice.err{border-color:#e3bdb7;background:var(--rejected-bg);color:var(--rejected)}
.group{border:1px solid var(--hairline);background:var(--surface);border-radius:3px;margin-bottom:14px}
.group-head{padding:12px 18px;border-bottom:1px solid var(--hairline);display:flex;
  justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.group-head h3{font-size:15px;font-weight:600}
.group-head span{font-size:12px;color:var(--ink2)}
.hrow{display:flex;justify-content:space-between;align-items:center;gap:14px;
  padding:11px 18px;border-bottom:1px solid var(--hairline);flex-wrap:wrap}
.hrow:last-child{border-bottom:none}
.hrow .code{min-width:34px}
.hrow-main{flex:1;min-width:180px}
.hrow-val{font-weight:600;font-variant-numeric:tabular-nums}
.btn{display:inline-block;font:inherit;font-size:13.5px;font-weight:600;border-radius:3px;
  padding:7px 14px;border:1px solid var(--ink);background:var(--ink);color:#f4f5f3;
  cursor:pointer;text-decoration:none;white-space:nowrap}
.btn.secondary{background:transparent;color:var(--ink)}
.btn.warn{background:var(--held);border-color:var(--held)}
.btn[disabled]{opacity:.4;cursor:not-allowed}
.btn:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid var(--applied);
  outline-offset:2px}
form.resolve{margin-top:14px;padding-top:14px;border-top:1px solid var(--hairline)}
label{display:block;font-size:12.5px;font-weight:600;color:var(--ink2);margin:10px 0 4px}
input[type=text],textarea,input[type=file]{width:100%;font:inherit;font-size:14px;
  border:1px solid var(--hairline);border-radius:3px;padding:7px 9px;background:#fff;color:var(--ink)}
textarea{min-height:60px;resize:vertical}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.check{display:flex;gap:8px;align-items:flex-start;font-size:13px;font-weight:500;
  color:var(--ink);margin:10px 0 0}
.check input{margin-top:3px}
fieldset{border:1px solid #e6d3a3;border-radius:3px;padding:10px 14px 14px;margin-top:14px;
  background:var(--held-bg)}
legend{font-size:12.5px;font-weight:600;color:var(--held);padding:0 4px}
.source{margin-top:12px}
.source pre{font-family:var(--mono);font-size:11.5px;line-height:1.5;background:#fff;
  border:1px solid var(--hairline);border-radius:3px;padding:10px;white-space:pre-wrap;
  word-break:break-word;max-height:260px;overflow:auto}
.source mark{background:#f4dc8a;color:inherit;padding:0 1px;border-radius:2px}
.status-line{margin-top:12px;font-size:13px}
table.hist{width:100%;border-collapse:collapse;font-size:13px}
table.hist th,table.hist td{text-align:left;padding:8px 10px 8px 0;border-bottom:1px solid var(--hairline);
  vertical-align:top}
table.hist th{font-size:12px;color:var(--ink2);font-weight:600}
.hist-wrap{overflow-x:auto}
.import-card{border:1px solid var(--hairline);background:var(--surface);border-radius:3px;
  padding:18px 20px;max-width:640px}
.hint{font-size:12.5px;color:var(--ink2);margin-top:3px}
#import-result{margin-top:14px}
@media (max-width:720px){
  .conflict-cols{flex-direction:column}
  .hrow{padding:10px 14px}
  .group-head{padding:10px 14px}
  .import-card{padding:14px}
}
"""

APP_JS = r"""
(function () {
  "use strict";
  var token = document.querySelector('meta[name="form-token"]');
  token = token ? token.getAttribute("content") : "";

  // Remember the reviewer name per browser (a convenience only).
  function getName() { try { return localStorage.getItem("reviewer") || ""; } catch (e) { return ""; } }
  function setName(v) { try { localStorage.setItem("reviewer", v); } catch (e) {} }
  document.querySelectorAll('input[name="reviewer"]').forEach(function (el) {
    if (!el.value) el.value = getName();
    el.addEventListener("change", function () { setName(el.value.trim()); });
  });

  var form = document.getElementById("import-form");
  if (!form) return;
  var out = document.getElementById("import-result");
  function show(cls, text) { out.className = "notice " + cls; out.textContent = text; }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var cand = form.querySelector('input[name="candidates"]').files[0];
    var man = form.querySelector('input[name="manifest"]').files[0];
    var srcs = Array.prototype.slice.call(form.querySelector('input[name="sources"]').files);
    var batch = form.querySelector('input[name="batch"]').value.trim();
    if (!cand) { show("err", "Choose a candidates file."); return; }
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    show("", "Importing…");
    var reads = [cand.text(), man ? man.text() : Promise.resolve(null)]
      .concat(srcs.map(function (f) { return f.text(); }));
    Promise.all(reads).then(function (texts) {
      var sources = {};
      srcs.forEach(function (f, i) { sources[f.name] = texts[2 + i]; });
      return fetch("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Form-Token": token },
        body: JSON.stringify({ candidates_name: cand.name, candidates: texts[0],
                               manifest: texts[1], sources: sources, batch: batch })
      });
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res.ok) { window.location.assign(res.redirect); return; }
      button.disabled = false;
      var m = String(res.message || "");
      show("err", "Not imported. " + m.charAt(0).toUpperCase() + m.slice(1));
    }).catch(function (e) {
      button.disabled = false;
      show("err", "Not imported. " + e);
    });
  });
})();
"""


def esc(s):
    return html.escape(str(s))


def fmt_value(value, unit):
    return review_page.fmt_value(value, unit)


class Refusal(Exception):
    """An HTTP-level refusal that is not a workspace error."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class App:
    def __init__(self, ws_path, port):
        self.ws_path = os.path.abspath(ws_path)
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.recovered_notice = False
        self.rendering_head = False  # a HEAD response has no body to show notices in
        workspace.RECOVERY_LISTENERS.append(self._on_recover)

    def _on_recover(self, path):
        if os.path.abspath(path) == self.ws_path:
            self.recovered_notice = True

    def hosts(self):
        return {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}

    def read(self):
        """Everything a page needs, read under the workspace lock."""
        with workspace.Workspace(self.ws_path) as ws:
            meta = ws.meta()
            audit = ws.audit()
            record = ws.record()
            holds = ws.open_holds(audit)
            texts = {}
            for e in audit:
                d = e.get("source_sha256")
                if d and d not in texts:
                    texts[d] = ws.source_text(d)
        return {"meta": meta, "audit": audit, "record": record, "holds": holds,
                "texts": texts}


# ---- page rendering --------------------------------------------------------

def layout(app, title, body, active="", dataset=""):
    nav = []
    for href, label, key in (("/", "Queue", "queue"), ("/import", "Import", "import"),
                             ("/report", "Full report", "report")):
        nav.append('<a href="%s"%s>%s</a>' % (href, ' class="on"' if key == active else "",
                                               label))
    sub = '<b>Local review workspace</b> &middot; %s' % esc(os.path.basename(app.ws_path))
    if dataset:
        sub += ' &middot; dataset: %s' % esc(dataset)
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"form-token\" content=\"%s\">"
            "<title>%s - %s</title><link rel=\"stylesheet\" href=\"/app.css\"></head><body>"
            "<div class=\"band\"><div class=\"band-inner\"><div class=\"wordmark\">"
            "<a href=\"/\">%s</a></div><nav>%s</nav></div>"
            "<div class=\"band-sub\">%s</div></div>"
            "<div class=\"page\">%s"
            "<p class=\"foot\">Runs locally on this computer (127.0.0.1). Single user; no "
            "accounts. Every change is written to the workspace's audit log before the "
            "page confirms it.</p></div>"
            "<script src=\"/app.js\"></script></body></html>\n"
            % (esc(app.token), esc(title), PRODUCT, PRODUCT, "".join(nav), sub, body))


def dataset_name(record):
    meta = record.get("meta") if isinstance(record, dict) else None
    return meta.get("name", "") if isinstance(meta, dict) else ""


def notices(app, extra=""):
    out = []
    if app.recovered_notice:
        out.append('<div class="notice warn">The last change to this workspace was '
                   'interrupted; it was completed when the workspace was opened. '
                   'Nothing was lost.</div>')
        if not app.rendering_head:
            app.recovered_notice = False
    return "".join(out) + extra


def page_queue(app, state, query):
    meta, audit, holds = state["meta"], state["audit"], state["holds"]
    s = workspace.summary(meta, audit, state["record"], holds)
    body = [notices(app)]
    imported = query.get("imported", [""])[0]
    if (imported.isascii() and imported.isdigit() and len(imported) <= 9
            and int(imported) < len(meta["ingests"])):
        ing = meta["ingests"][int(imported)]
        c = ing.get("counts") or {}
        body.append('<div class="notice ok">Imported %s: %d update(s) &middot; %d applied, '
                    '%d held for review, %d rejected.</div>'
                    % (esc(ing.get("file")), ing.get("decisions", 0), c.get("apply", 0),
                       c.get("hold", 0), c.get("reject", 0)))
    d = s["decisions"]
    body.append('<div class="stats">'
                '<div class="stat"><b>%d</b><span>awaiting review</span></div>'
                '<div class="stat"><b>%d</b><span>applied</span></div>'
                '<div class="stat"><b>%d</b><span>held</span></div>'
                '<div class="stat"><b>%d</b><span>rejected</span></div>'
                '<div class="stat"><b>%d</b><span>resolved by analyst</span></div></div>'
                % (len(holds), d["apply"], d["hold"], d["reject"],
                   s["resolutions"]["accepted"] + s["resolutions"]["dismissed"]))
    body.append('<section id="queue"><h2>Awaiting review (%d)</h2>' % len(holds))
    body.append('<div class="sec-note">Each hold keeps the current record value until '
                'someone accepts or dismisses it. Holds on the same metric are compared '
                'together.</div>')
    if not holds:
        body.append('<div class="empty">Nothing waiting for review. '
                    '<a href="/import">Import updates</a></div>')
    groups = {}
    for h in holds:
        groups.setdefault((h.get("entity"), h.get("metric")), []).append(h)
    for (ent, metric), hs in groups.items():
        body.append('<div class="group"><div class="group-head"><h3>%s</h3>'
                    '<span>%s &middot; %d open</span></div>'
                    % (esc(review_page.metric_label(metric)), esc(ent), len(hs)))
        for h in hs:
            body.append('<div class="hrow"><span class="code">%s</span>'
                        '<div class="hrow-main"><span class="hrow-val">%s</span> '
                        '<span class="code">%s</span><div class="qrow-src">%s</div></div>'
                        '<a class="btn secondary" href="/hold/%s">Review</a></div>'
                        % (esc(h["id"]), fmt_value(h.get("claimed_value"), h.get("unit")),
                           esc(h.get("reason_code")),
                           review_page.source_line(review_page.candidate_source(h)),
                           urllib.parse.quote(h["id"])))
        body.append("</div>")
    body.append("</section>")

    body.append('<section id="imports"><h2>Imports (%d)</h2>' % len(meta["ingests"]))
    if not meta["ingests"]:
        body.append('<div class="empty">No updates imported yet.</div>')
    else:
        body.append('<div class="hist-wrap"><table class="hist"><tr><th>When</th><th>File</th>'
                    '<th>Batches</th><th>Updates</th></tr>')
        for ing in reversed(meta["ingests"]):
            body.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td></tr>"
                        % (esc(ing.get("ts")), esc(ing.get("file")),
                           esc(", ".join(ing.get("batches", []))), ing.get("decisions", 0)))
        body.append("</table></div>")
    body.append("</section>")
    return layout(app, "Queue", "".join(body), "queue", dataset_name(state["record"]))


def highlight(text, value):
    spans = reconcile.value_spans(value, text) if reconcile.is_finite_number(value) else []
    out, pos = [], 0
    for a, b in spans:
        out.append(esc(text[pos:a]))
        out.append("<mark>%s</mark>" % esc(text[a:b]))
        pos = b
    out.append(esc(text[pos:]))
    return "".join(out), len(spans)


def page_hold(app, state, hold_id, query, error=None):
    audit, record = state["audit"], state["record"]
    target = next((e for e in audit if e.get("id") == hold_id
                   and e.get("type") != "resolution"), None)
    if target is None:
        raise Refusal("There is no decision with id %s." % hold_id, "unknown_id")
    if target.get("decision") != "hold":
        raise Refusal("%s was %s by the rules, not held, so there is nothing to review."
                      % (hold_id, review_page.DECISION_LABEL.get(target.get("decision"),
                                                                  target.get("decision")).lower()),
                      "not_a_hold")
    ent, metric = target.get("entity"), target.get("metric")
    resolutions = {e.get("resolves"): e for e in audit if e.get("type") == "resolution"}
    related = [e for e in audit if e.get("decision") == "hold" and e.get("type") != "resolution"
               and e.get("entity") == ent and e.get("metric") == metric]

    body = [notices(app)]
    done = query.get("done", [""])[0]
    res = next((e for e in audit if e.get("id") == done and e.get("type") == "resolution"
                and e.get("resolves") == hold_id), None)
    if res is not None:
        body.append('<div class="notice ok">%s saved: %s %s. %s</div>' % (
            esc(res["id"]), esc(hold_id),
            "accepted, record updated" if res["action"] == "accept"
            else "dismissed, record unchanged",
            '<a href="/">Back to the queue</a>'))
    if error is not None:
        body.append('<div class="notice err"><b>%s.</b> %s</div>'
                    % (esc(TITLE.get(error.code, "Not saved")), esc(error)))

    cur = record.get("entities", {}).get(ent, {}).get("metrics", {}).get(metric)
    body.append('<section id="compare"><h2>%s &middot; %s</h2>'
                % (esc(review_page.metric_label(metric)), esc(ent)))
    body.append('<div class="sec-note">The current record value stands until someone rules. '
                'Each held update is shown with its source, and the claimed number is '
                'highlighted where it appears in the source text.</div>')
    body.append('<div class="conflict"><div class="conflict-head"><h3>Current record and '
                'held updates</h3><span>%d held &middot; %d open</span></div>'
                '<div class="conflict-cols">'
                % (len(related), sum(1 for e in related if e["id"] not in resolutions)))
    if cur:
        body.append('<div class="col current"><span class="col-role">Current record</span>'
                    '<div class="col-value">%s</div><div class="col-period">%s &middot; as of %s'
                    '</div><div class="col-src">%s</div></div>'
                    % (fmt_value(cur.get("value"), cur.get("unit", "")), esc(cur.get("period", "")),
                       esc(cur.get("as_of", "")), review_page.source_line(cur.get("source", {}))))
    else:
        body.append('<div class="col current"><span class="col-role">Current record</span>'
                    '<div class="col-none">No current value for this metric.</div></div>')
    for e in related:
        body.append(hold_column(app, state, e, resolutions.get(e["id"]), e["id"] == hold_id))
    body.append("</div></div></section>")
    return layout(app, "Review %s" % hold_id, "".join(body), "", dataset_name(record))


def hold_column(app, state, e, resolution, focused):
    record = state["record"]
    hid = e["id"]
    parts = ['<div class="col" id="%s">' % esc(hid)]
    parts.append('<span class="col-role">%s &middot; %s%s</span>'
                 % (esc(hid), "open" if resolution is None else "resolved",
                    " &middot; this hold" if focused else ""))
    parts.append('<div class="col-value">%s</div><div class="col-period">claimed for %s</div>'
                 '<div class="col-src">%s</div>'
                 % (fmt_value(e.get("claimed_value"), e.get("unit", "")), esc(e.get("period", "")),
                    review_page.source_line(review_page.candidate_source(e))))
    parts.append('<div class="col-decision"><span class="tag held">Held</span>'
                 '<span class="code">%s</span>%s</div>'
                 % (esc(e.get("reason_code")), review_page.render_flags(e)))

    text = state["texts"].get(e.get("source_sha256")) if e.get("source_sha256") else None
    if text is not None:
        marked, n = highlight(text, e.get("claimed_value"))
        parts.append('<div class="source"><label>Source text%s</label><pre>%s</pre></div>'
                     % ("" if n else " (claimed value not found)", marked))
    else:
        parts.append('<div class="hint">Source text was not stored for this update '
                     '(imported without its source document).</div>')

    if resolution is not None:
        parts.append('<div class="status-line"><span class="tag %s">%s</span> %s by %s '
                     '&middot; %s%s%s</div>' % (
                         "applied" if resolution["action"] == "accept" else "rejected",
                         "Accepted" if resolution["action"] == "accept" else "Dismissed",
                         esc(resolution["id"]), esc(resolution.get("reviewer")),
                         esc(resolution.get("ts")),
                         " &middot; forced over a newer value"
                         if resolution.get("forced_over_change") else "",
                         ('<div class="hint">note: %s</div>' % esc(resolution["note"]))
                         if resolution.get("note") else ""))
        parts.append("</div>")
        return "".join(parts)

    then, now = workspace.hold_staleness(record, e)
    stale = then != now
    action = "/hold/%s" % urllib.parse.quote(hid)
    tok = '<input type="hidden" name="token" value="%s">' % esc(app.token)
    if stale:
        parts.append('<div class="notice warn" style="margin-top:12px">The record changed '
                     'after this was held: it was %s, it is now %s. A normal accept would '
                     'overwrite the newer value.</div>'
                     % (esc(workspace._short(then)), esc(workspace._short(now))))
    parts.append('<form class="resolve" method="post" action="%s">%s'
                 '<label for="r-%s">Reviewer</label>'
                 '<input type="text" id="r-%s" name="reviewer" required maxlength="80" '
                 'autocomplete="name">'
                 '<label for="n-%s">Note</label><textarea id="n-%s" name="note" maxlength="2000">'
                 '</textarea><div class="actions">'
                 '<button class="btn" type="submit" name="action" value="accept"%s>Accept</button>'
                 '<button class="btn secondary" type="submit" name="action" value="dismiss">'
                 'Dismiss</button></div>'
                 % (action, tok, esc(hid), esc(hid), esc(hid), esc(hid),
                    " disabled" if stale else ""))
    if stale:
        parts.append('<fieldset><legend>Accept anyway</legend>'
                     '<label class="check"><input type="checkbox" name="force" value="1"> '
                     'I have checked the newer value and want to replace it with this one.'
                     '</label><div class="hint">A note is required. The decision log marks '
                     'this accept as forced.</div><div class="actions">'
                     '<button class="btn warn" type="submit" name="action" value="accept_force">'
                     'Accept anyway</button></div></fieldset>')
    parts.append("</form></div>")
    return "".join(parts)


def page_import(app, state):
    body = [notices(app)]
    body.append('<section id="import"><h2>Import updates</h2>'
                '<div class="sec-note">Load a candidates file (a JSON list of extracted '
                'updates). Add the manifest and the source text files so each value can be '
                'checked against its document and shown beside it during review. The files '
                'are read by this page and sent only to this local app.</div>'
                '<div class="import-card"><form id="import-form">'
                '<label for="f-c">Candidates file (.json)</label>'
                '<input type="file" id="f-c" name="candidates" accept=".json,application/json" '
                'required>'
                '<label for="f-m">Manifest (.json, optional)</label>'
                '<input type="file" id="f-m" name="manifest" accept=".json,application/json">'
                '<div class="hint">Maps each source_id to its source file and batch.</div>'
                '<label for="f-s">Source documents (.txt, optional, several allowed)</label>'
                '<input type="file" id="f-s" name="sources" accept=".txt,text/plain" multiple>'
                '<label for="f-b">Batch id (optional)</label>'
                '<input type="text" id="f-b" name="batch" maxlength="80">'
                '<div class="hint">Used for updates that have no batch id of their own.</div>'
                '<div class="actions"><button class="btn" type="submit">Import</button></div>'
                '</form><div id="import-result" role="status" aria-live="polite"></div></div>'
                '</section>')
    return layout(app, "Import", "".join(body), "import", dataset_name(state["record"]))


def page_error(app, err, back="/"):
    code = getattr(err, "code", "error")
    body = ['<section><h2>%s</h2><div class="notice err">%s</div>'
            % (esc(TITLE.get(code, "Something went wrong")), esc(err))]
    if code in READ_ONLY_CODES:
        body.append('<div class="sec-note">The app is read-only until this is fixed. No '
                    'change was made. Files in the workspace folder were left as they are.'
                    '</div>')
    elif code == "locked":
        body.append('<div class="sec-note">Another command is using this workspace. Nothing '
                    'was changed; try again in a moment.</div>')
    else:
        body.append('<div class="sec-note">Nothing was changed.</div>')
    body.append('<p><a class="btn secondary" href="%s">Back</a></p></section>' % esc(back))
    return layout(app, TITLE.get(code, "Error"), "".join(body))


# ---- HTTP handling ---------------------------------------------------------

def make_handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ReconciliationWorkbench"
        timeout = REQUEST_TIMEOUT  # socket timeout for every read
        sys_version = ""

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

        # -- responses --
        def send(self, status, body, ctype="text/html; charset=utf-8", extra=None,
                 csp=None):
            # "replace": a stored lone surrogate (legal in JSON escapes) must
            # never make a page impossible to render.
            data = body.encode("utf-8", "replace") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            # same-origin, not no-referrer: with no-referrer Chrome sends
            # "Origin: null" on form posts, which the Origin check must refuse.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", csp or (
                "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                "base-uri 'none'"))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def fail(self, err, back="/"):
            code = getattr(err, "code", "error")
            self.send(STATUS.get(code, 500), page_error(app, err, back))

        def fail_json(self, err):
            code = getattr(err, "code", "error")
            self.send(STATUS.get(code, 500),
                      json.dumps({"ok": False, "code": code, "message": str(err)}),
                      "application/json")

        def host_ok(self):
            return self.headers.get("Host", "") in app.hosts()

        # -- GET --
        def do_GET(self):
            if not self.host_ok():
                return self.fail(Refusal("Unexpected Host header.", "forbidden"))
            url = urllib.parse.urlsplit(self.path)
            try:
                query = urllib.parse.parse_qs(url.query, max_num_fields=10)
            except ValueError:
                return self.fail(Refusal("Too many query parameters.", "bad_request"))
            path = url.path
            if path == "/app.css":
                return self.send(200, review_page.CSS + APP_CSS, "text/css; charset=utf-8")
            if path == "/app.js":
                return self.send(200, APP_JS, "text/javascript; charset=utf-8")
            if path == "/favicon.ico":
                return self.send(204, b"", "image/x-icon")
            try:
                state = app.read()
                if path == "/":
                    return self.send(200, page_queue(app, state, query))
                if path == "/import":
                    return self.send(200, page_import(app, state))
                if path == "/report":
                    return self.send(200, review_page.render_html(state["record"],
                                                                  state["audit"]),
                                     csp="default-src 'none'; style-src 'unsafe-inline'; "
                                         "frame-ancestors 'none'; base-uri 'none'")
                if path.startswith("/hold/"):
                    hid = urllib.parse.unquote(path[len("/hold/"):])
                    return self.send(200, page_hold(app, state, hid, query))
                raise Refusal("There is no page at %s." % path, "not_found_page")
            except (WorkspaceError, Refusal) as e:
                return self.fail(e)
            except Exception as e:  # never show a traceback to the browser
                self.log_message("unexpected error: %r", e)
                return self.fail(Refusal("Unexpected error; nothing was changed.", "error"))

        def do_HEAD(self):
            app.rendering_head = True  # the notice waits for a page that shows it
            try:
                self.do_GET()
            finally:
                app.rendering_head = False

        # -- POST --
        def read_body(self, as_json):
            if not self.host_ok():
                raise Refusal("Unexpected Host header.", "forbidden")
            origin = self.headers.get("Origin")
            if origin is None or origin not in {"http://" + h for h in app.hosts()}:
                raise Refusal("This form must be sent from the app's own page.", "forbidden")
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise Refusal("Missing Content-Length.", "bad_request")
            if n < 0 or n > MAX_BODY:
                self.close_connection = True
                raise Refusal("The upload is larger than %d MB." % (MAX_BODY // 2 ** 20),
                              "too_large")
            raw = self.read_exactly(n)
            if as_json:
                token = self.headers.get("X-Form-Token", "")
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (ValueError, RecursionError):
                    raise Refusal("The request was not valid JSON.", "bad_request")
                if not isinstance(data, dict):
                    raise Refusal("The request was not a JSON object.", "bad_request")
            else:
                try:
                    data = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True,
                                                 max_num_fields=20)
                except (ValueError, UnicodeDecodeError):
                    raise Refusal("The form could not be read.", "bad_request")
                data = {k: v[0] for k, v in data.items()}
                token = data.get("token", "")
            given = str(token).encode("utf-8", "surrogateescape")
            if not secrets.compare_digest(given, app.token.encode("ascii")):
                raise Refusal("The form token is missing or out of date. Reload the page "
                              "and try again.", "forbidden")
            return data

        def read_exactly(self, n):
            """Read the body within REQUEST_TIMEOUT in total, however it trickles in."""
            deadline = time.monotonic() + self.timeout
            parts, got = [], 0
            try:
                while got < n:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError
                    self.connection.settimeout(left)
                    chunk = self.rfile.read1(min(65536, n - got))
                    if not chunk:
                        self.close_connection = True
                        raise Refusal("The upload was cut short; nothing was changed.",
                                      "bad_request")
                    parts.append(chunk)
                    got += len(chunk)
            except (TimeoutError, OSError):
                self.close_connection = True
                raise Refusal("The upload took longer than %d seconds and was abandoned; "
                              "nothing was changed." % self.timeout, "timeout")
            finally:
                try:
                    self.connection.settimeout(self.timeout)
                except OSError:
                    pass
            return b"".join(parts)

        def do_POST(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/api/import":
                try:
                    data = self.read_body(as_json=True)
                    result = do_import(app, data)
                except (WorkspaceError, Refusal) as e:
                    return self.fail_json(e)
                except Exception as e:
                    self.log_message("unexpected error: %r", e)
                    return self.fail_json(Refusal("Unexpected error; nothing was changed.",
                                                  "error"))
                return self.send(200, json.dumps(result), "application/json")
            if url.path.startswith("/hold/"):
                hid = urllib.parse.unquote(url.path[len("/hold/"):])
                back = "/hold/%s" % urllib.parse.quote(hid)
                try:
                    data = self.read_body(as_json=False)
                except Refusal as e:
                    return self.fail(e, back)
                try:
                    res = do_resolve(app, hid, data)
                except (WorkspaceError, Refusal) as e:
                    # Show the refusal on the hold page itself when it can be read.
                    try:
                        state = app.read()
                        return self.send(STATUS.get(e.code, 500),
                                         page_hold(app, state, hid, {}, error=e))
                    except (WorkspaceError, Refusal):
                        return self.fail(e, back)
                except Exception as e:
                    self.log_message("unexpected error: %r", e)
                    return self.fail(Refusal("Unexpected error; nothing was changed.",
                                             "error"), back)
                return self.send(303, "", extra={
                    "Location": "%s?done=%s" % (back, urllib.parse.quote(res["entry"]["id"]))})
            return self.fail(Refusal("There is no form at %s." % url.path, "not_found_page"))

    return Handler


def do_import(app, data):
    cand = data.get("candidates")
    name = data.get("candidates_name") or "candidates.json"
    if not isinstance(cand, str) or not isinstance(name, str):
        raise Refusal("Choose a candidates file.", "bad_request")
    sources = data.get("sources") or {}
    if not isinstance(sources, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in sources.items()):
        raise Refusal("Source files must be text.", "bad_request")
    batch = data.get("batch") or None
    if batch is not None and not isinstance(batch, str):
        raise Refusal("Batch id must be text.", "bad_request")
    texts = batches = None
    manifest = data.get("manifest")
    if manifest is not None:
        if not isinstance(manifest, str):
            raise Refusal("Manifest must be a JSON file.", "bad_request")
        try:
            parsed = json.loads(manifest)
        except (ValueError, RecursionError) as e:
            raise WorkspaceError("manifest is not valid JSON: %s" % e, "bad_manifest")

        def read_file(fname):
            base = os.path.basename(fname)
            if base not in sources:
                raise WorkspaceError("the manifest lists %s, but that source file was not "
                                     "chosen. Add it under Source documents." % base,
                                     "missing_source")
            return sources[base]
        texts, batches = workspace.parse_manifest(parsed, read_file)
    raw = _utf8(cand, "candidates file")
    _utf8(name, "file name")
    if batch is not None:
        _utf8(batch, "batch id")
    workspace.parse_candidates(raw)  # clean JSON error first
    for k, v in sources.items():
        _utf8(k, "source file name")
        _utf8(v, "source file")
    for v in (batches or {}).values():
        _utf8(v, "manifest batch id")
    workspace.ingest(app.ws_path, raw, name, texts=texts,
                           batches=batches, batch_default=batch)
    state = app.read()
    return {"ok": True, "redirect": "/?imported=%d" % (len(state["meta"]["ingests"]) - 1)}


def do_resolve(app, hold_id, data):
    action = data.get("action", "")
    force = False
    if action == "accept_force":
        if data.get("force") != "1":
            raise Refusal("Tick the box to confirm replacing the newer value.",
                          "record_changed")
        if not data.get("note", "").strip():
            raise Refusal("Add a note saying why the older value should replace the "
                          "newer one.", "note_required")
        action, force = "accept", True
    if action not in ("accept", "dismiss"):
        raise Refusal("Choose Accept or Dismiss.", "bad_action")
    res = workspace.resolve(app.ws_path, hold_id, action, data.get("reviewer", ""),
                            data.get("note", "").strip(), force)
    return res


def _utf8(text, what):
    """Text as UTF-8 bytes; lone surrogates (possible via JSON escapes) refused."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise Refusal("The %s contains characters that are not valid text." % what,
                      "bad_request")


def make_server(ws_path, port=0):
    """Bind to loopback; returns (server, app). port=0 picks a free port."""
    srv = http.server.HTTPServer(("127.0.0.1", port), None)
    app = App(ws_path, srv.server_address[1])
    srv.RequestHandlerClass = make_handler(app)
    return srv, app


def main(argv=None):
    ap = argparse.ArgumentParser(description="%s: local review app." % PRODUCT)
    ap.add_argument("workspace")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--init", action="store_true",
                    help="create the workspace from the bundled synthetic seed if missing")
    args = ap.parse_args(argv)
    if args.init and not os.path.exists(os.path.join(args.workspace, workspace.META)):
        try:
            workspace.init_workspace(args.workspace,
                                     workspace._load_json(os.path.join(HERE, "record.json"),
                                                          "seed record"),
                                     workspace._load_json(os.path.join(HERE, "registry.json"),
                                                          "registry"))
        except WorkspaceError as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
    srv, app = make_server(args.workspace, args.port)
    try:
        app.read()  # completes an interrupted change now, so the queue can say so
    except WorkspaceError as e:
        print("warning: %s (the app will open read-only)" % e, file=sys.stderr)
    print("%s: http://127.0.0.1:%d/  (workspace %s; Ctrl+C to stop)"
          % (PRODUCT, app.port, app.ws_path))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
