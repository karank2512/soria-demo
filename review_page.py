#!/usr/bin/env python3
"""Soria demo: render the analyst review surface as a self-contained HTML page.

Inputs: updated record.json + audit.jsonl (both produced by reconcile.py).
Output: review.html - everything held for a person first (source conflicts
compared side by side against the current record, plus any other holds), then
the record with per-value provenance, rejections, and the decision log.
No external resources, no interactivity beyond section anchors.

All data is synthetic and labeled as such.
"""

import argparse
import html
import json

DECISION_LABEL = {"apply": "Applied", "hold": "Held for review", "reject": "Rejected"}

# Display labels for the demo's metric keys. Unknown keys fall back to a
# humanized form of the raw key; the raw key is always available in the data.
METRIC_LABEL = {
    "ma_enrollment": "Medicare Advantage enrollment",
    "mlr_pct": "Medical loss ratio",
    "premium_revenue_musd": "Premium revenue",
    "star_rating": "Star rating",
    "outpatient_visits_q": "Outpatient visits (quarter)",
    "ebitda_margin_pct": "EBITDA margin",
    "rd_spend_musd": "R&D spend",
}

SOURCE_TYPE_LABEL = {
    "government_data": "Government data",
    "regulatory_filing": "Regulatory filing",
    "company_filing": "Company filing",
    "news": "News",
    "analyst_note": "Analyst note",
}

FLAG_LABEL = {
    "low_authority": "low-authority source",
    "low_authority_override": "Lower-ranked source",
}


def esc(s):
    return html.escape(str(s))


def metric_label(key):
    if key in METRIC_LABEL:
        return METRIC_LABEL[key]
    return str(key).replace("_", " ")


def fmt_num(v):
    """1,312,000 for whole numbers; real decimals keep their precision."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return "{:,}".format(int(f))
    return "{:,}".format(f)



def fmt_value(value, unit):
    """Value with its unit, e.g. 84.2% or $2,150M. HTML-escaped."""
    v = esc(fmt_num(value))
    if unit == "percent":
        return v + "%"
    if unit == "musd":
        return "$" + v + "M"
    if unit:
        return v + " " + esc(unit)
    return v


def source_line(src):
    """One quiet line of provenance: type, id, publication date, citation."""
    parts = []
    st = src.get("type", "")
    if st:
        parts.append(SOURCE_TYPE_LABEL.get(st, st.replace("_", " ")))
    if src.get("source_id"):
        parts.append(src["source_id"])
    if src.get("pub_date"):
        parts.append("published " + src["pub_date"])
    cit = (src.get("citation") or {}).get("locator", "")
    if cit:
        parts.append(cit)
    return " &middot; ".join(esc(p) for p in parts)


def candidate_source(a):
    return {"type": a.get("source_type", ""), "source_id": a.get("source_id", ""),
            "pub_date": a.get("pub_date", ""),
            "citation": {"locator": (a.get("citation") or {}).get("locator", "")}}


def render_flags(a):
    """Human flag chip with the raw flag code retained beside it."""
    out = []
    for fl in a.get("flags", []):
        out.append('<span class="flagchip">%s</span> <span class="code">%s</span>'
                   % (esc(FLAG_LABEL.get(fl, fl.replace("_", " "))), esc(fl)))
    return "".join(out)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


CSS = """
:root{
  --bg:#f2f3f1; --surface:#fbfcfb; --ink:#181c19; --ink2:#5f655f;
  --hairline:#dde1dc; --band:#141815; --band2:#9aa39b;
  --mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
  --applied:#145c42; --applied-bg:#e3efe8;
  --held:#7d5800; --held-bg:#f7edd4;
  --rejected:#8f352c; --rejected-bg:#f6e7e4;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased}

/* header band */
.band{background:var(--band);color:#e8ebe7;padding:0 40px}
.band-inner{max-width:1040px;margin:0 auto;display:flex;justify-content:space-between;
  align-items:center;gap:24px;padding:17px 0;flex-wrap:wrap}
.wordmark{font-size:15px;font-weight:650;letter-spacing:-.01em;color:#e8ebe7}
.wordmark span{color:var(--band2);font-weight:500}
nav{display:flex;gap:22px;flex-wrap:wrap}
nav a{font-size:13px;color:var(--band2);text-decoration:none}
nav a:hover{color:#e8ebe7}
.band-sub{max-width:1040px;margin:0 auto;padding:0 0 18px;font-size:13px;color:var(--band2)}
.band-sub b{color:#e8ebe7;font-weight:600}

.page{max-width:1040px;margin:0 auto;padding:32px 40px 64px}
.banner{border:1px solid var(--ink);border-radius:3px;padding:10px 16px;
  font-size:13px;display:flex;gap:10px;align-items:baseline}
.banner b{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;
  text-transform:uppercase;background:var(--ink);color:#f4f5f3;border-radius:2px;
  padding:3px 8px;white-space:nowrap}

.stats{display:flex;flex-wrap:wrap;margin:28px 0 0;border-top:1px solid var(--hairline);
  border-bottom:1px solid var(--hairline)}
.stat{padding:14px 28px 14px 0;margin-right:28px;border-right:1px solid var(--hairline)}
.stat:last-child{border-right:none}
.stat b{display:block;font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.stat span{font-size:12.5px;color:var(--ink2)}

section{margin-top:48px}
h2{font-size:19px;font-weight:600;margin:0 0 6px;letter-spacing:-.005em}
.sec-note{color:var(--ink2);font-size:13.5px;margin-bottom:18px;max-width:640px}

.entity{margin-bottom:28px}
.entity-head{display:flex;justify-content:space-between;align-items:baseline;
  gap:12px;flex-wrap:wrap;border-bottom:2px solid var(--ink);padding-bottom:8px}
.entity-head h3{font-size:16px;font-weight:600}
.entity-head span{font-size:12px;color:var(--ink2)}
.metric{display:flex;justify-content:space-between;gap:20px;padding:13px 0;
  border-bottom:1px solid var(--hairline)}
.metric-name{font-weight:550}
.metric-meta{font-size:12.5px;color:var(--ink2);margin-top:1px}
.metric-src{font-family:var(--mono);font-size:11px;color:var(--ink2);margin-top:5px}
.metric-value{font-size:16.5px;font-weight:600;font-variant-numeric:tabular-nums;
  white-space:nowrap;text-align:right;padding-top:1px}

/* conflict comparison */
.conflict{border:1px solid var(--hairline);background:var(--surface);
  border-radius:3px;margin-bottom:20px}
.conflict-head{padding:14px 20px;border-bottom:1px solid var(--hairline);
  display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.conflict-head h3{font-size:15.5px;font-weight:600}
.conflict-head span{font-size:12px;color:var(--ink2)}
.conflict-cols{display:flex;align-items:stretch}
.col{flex:1;padding:16px 20px;border-right:1px solid var(--hairline);min-width:0}
.col:last-child{border-right:none}
.col-role{font-size:12px;font-weight:600;color:var(--ink2);margin-bottom:8px;display:block}
.col.current .col-role{color:var(--applied)}
.col-value{font-size:23px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.col-period{font-size:12.5px;font-weight:600;margin-top:2px}
.col-src{font-family:var(--mono);font-size:11px;color:var(--ink2);margin-top:8px}
.col-decision{margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tag{font-size:11.5px;font-weight:600;border-radius:3px;padding:2px 8px;white-space:nowrap}
.tag.held{color:var(--held);background:var(--held-bg)}
.tag.rejected{color:var(--rejected);background:var(--rejected-bg)}
.tag.applied{color:var(--applied);background:var(--applied-bg)}
.code{font-family:var(--mono);font-size:11px;color:var(--ink2)}
.flagchip{font-size:11px;border:1px solid var(--hairline);border-radius:3px;
  padding:1px 7px;color:var(--ink2);margin-right:2px}
h3.sub{font-size:13px;font-weight:600;color:var(--ink2);margin:24px 0 2px}
.col-none{color:var(--ink2);font-size:13px}

.qrow{padding:14px 0;border-bottom:1px solid var(--hairline)}
.qrow:last-child{border-bottom:none}
.qrow-top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.qrow-what{font-weight:550}
.qrow-what .ent{color:var(--ink2);font-weight:400}
.qrow-val{font-size:14.5px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.qrow-why{display:flex;align-items:center;gap:8px;margin-top:7px;flex-wrap:wrap}
.qrow-src{font-family:var(--mono);font-size:11px;color:var(--ink2);margin-top:5px}

.logrow{display:flex;gap:16px;align-items:baseline;padding:10px 0;
  border-bottom:1px solid var(--hairline);font-size:13.5px}
.logrow:last-child{border-bottom:none}
.logrow time{font-family:var(--mono);font-size:11px;color:var(--ink2);
  min-width:132px;flex-shrink:0}
.logrow .what{flex:1;min-width:0}
.logrow .what .ent{color:var(--ink2)}
.logrow .what .val{font-family:var(--mono);font-size:12.5px}
.logrow .prov{font-size:12px;color:var(--ink2)}
.empty{padding:6px 0;color:var(--ink2);font-size:13.5px}
.foot{margin-top:40px;font-size:12.5px;color:var(--ink2);max-width:640px}

@media (max-width:720px){
  .band{padding:0 18px}
  .band-inner{padding:12px 0;gap:12px}
  nav{gap:14px}
  nav a{font-size:12.5px}
  .band-sub{padding:0 0 12px;font-size:12px}
  .page{padding:20px 18px 48px}
  .banner{padding:8px 12px;font-size:12.5px}
  .stats{margin-top:20px}
  .stat{padding:10px 12px 10px 0;margin-right:12px}
  .stat b{font-size:18px}
  .stat span{font-size:11.5px}
  section{margin-top:36px}
  .metric{flex-direction:column;gap:4px}
  .metric-value{text-align:left}
  .conflict-cols{flex-direction:column}
  .col{border-right:none;border-bottom:1px solid var(--hairline);padding:14px 20px}
  .col:last-child{border-bottom:none}
  .logrow{flex-wrap:wrap;gap:4px 12px}
  .logrow time{min-width:0;flex-basis:100%}
}
"""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    record = load_json(args.record)
    audit = []
    with open(args.audit, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                audit.append(json.loads(line))

    holds = [a for a in audit if a["decision"] == "hold"]
    rejects = [a for a in audit if a["decision"] == "reject"]
    applies = [a for a in audit if a["decision"] == "apply"]

    # Source conflicts: held candidates grouped by (entity, metric), each shown
    # against the value the record currently holds for that pair.
    conflicts = {}
    for a in holds:
        if a["reason_code"] == "conflicting_sources":
            conflicts.setdefault((a["entity"], a["metric"]), []).append(a)

    nav = ['<a href="#review">Review</a>',
           '<a href="#record">Record</a>',
           '<a href="#rejected">Rejected</a>',
           '<a href="#log">Decision log</a>']

    p = []
    p.append("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meridian Research - source reconciliation review (synthetic demo)</title>
<style>%s</style></head><body>""" % CSS)

    p.append('<div class="band"><div class="band-inner">'
             '<div class="wordmark">Meridian <span>Research</span></div>'
             "<nav>%s</nav></div>"
             '<div class="band-sub"><b>Source reconciliation review</b> &middot; '
             'analyst workspace &middot; synthetic demo</div></div>' % "".join(nav))

    p.append('<div class="page">')
    p.append('<div class="banner"><b>Synthetic demonstration</b>'
             '<span>Every entity, source and value on this page is fictional.</span></div>')

    p.append('<div class="stats">'
             '<div class="stat"><b>%d</b><span>processed</span></div>'
             '<div class="stat"><b>%d</b><span>applied</span></div>'
             '<div class="stat"><b>%d</b><span>held</span></div>'
             '<div class="stat"><b>%d</b><span>rejected</span></div></div>'
             % (len(audit), len(applies), len(holds), len(rejects)))

    # ---- Review: everything held for a person, first --------------------
    other_holds = [a for a in holds if a["reason_code"] != "conflicting_sources"]
    p.append('<section id="review">')
    p.append('<h2>Held for review (%d)</h2>' % len(holds))
    p.append('<div class="sec-note">Every hold waits for an analyst; the current record stands '
             'until a person rules. Sources that disagree about the same metric are compared '
             'side by side below.</div>')
    if not holds:
        p.append('<div class="empty">Nothing waiting for review.</div>')

    # Conflict panels: each (entity, metric) with held candidates, compared
    # side by side against the value the record currently holds.
    if conflicts:
        for (ent, metric), cands in conflicts.items():
            cur = record.get("entities", {}).get(ent, {}).get("metrics", {}).get(metric)
            p.append('<div class="conflict">')
            p.append('<div class="conflict-head"><h3>%s</h3><span>%s &middot; %d held</span></div>'
                     % (esc(metric_label(metric)), esc(ent), len(cands)))
            p.append('<div class="conflict-cols">')
            # current record column
            if cur:
                p.append('<div class="col current">'
                         '<span class="col-role">Current record</span>'
                         '<div class="col-value">%s</div>'
                         '<div class="col-period">%s &middot; as of %s</div>'
                         '<div class="col-src">%s</div></div>' % (
                             fmt_value(cur["value"], cur.get("unit", "")),
                             esc(cur.get("period", "")), esc(cur.get("as_of", "")),
                             source_line(cur.get("source", {}))))
            else:
                p.append('<div class="col current"><span class="col-role">Current record</span>'
                         '<div class="col-none">No current value for this metric.</div></div>')
            # candidate columns, audit order
            for i, a in enumerate(cands, 1):
                flags = render_flags(a)
                p.append('<div class="col">'
                         '<span class="col-role">Candidate %d &middot; held</span>'
                         '<div class="col-value">%s</div>'
                         '<div class="col-period">claimed for %s</div>'
                         '<div class="col-src">%s</div>'
                         '<div class="col-decision"><span class="tag held">Held</span>'
                         '<span class="code">%s</span>%s</div></div>' % (
                             i, fmt_value(a["claimed_value"], a.get("unit", "")),
                             esc(a.get("period", "")),
                             source_line(candidate_source(a)),
                             esc(a["reason_code"]), flags))
            p.append('</div></div>')

    # Other (non-conflict) holds as rows in the same review section.
    if conflicts and other_holds:
        p.append('<h3 class="sub">Other holds</h3>')
    for a in other_holds:
        flags = render_flags(a)
        p.append('<div class="qrow">'
                 '<div class="qrow-top"><div class="qrow-what">%s '
                 '<span class="ent">&middot; %s</span></div>'
                 '<div class="qrow-val">%s</div></div>'
                 '<div class="qrow-why"><span class="tag held">Held</span>'
                 '<span class="code">%s</span>%s</div>'
                 '<div class="qrow-src">%s &middot; claimed for %s</div></div>' % (
                     esc(metric_label(a["metric"])), esc(a["entity"]),
                     fmt_value(a["claimed_value"], a.get("unit", "")),
                     esc(a["reason_code"]), flags,
                     source_line(candidate_source(a)), esc(a.get("period", ""))))
    p.append("</section>")

    # ---- Record ----------------------------------------------------------
    p.append('<section id="record">')
    p.append('<h2>Current record values, with provenance</h2>')
    p.append('<div class="sec-note">Every record value carries its source. Updates are applied, '
             'held, or rejected by the deterministic SOP; nothing is overwritten silently.</div>')
    for ent, edata in record["entities"].items():
        metrics = edata.get("metrics", {})
        p.append('<div class="entity">')
        p.append('<div class="entity-head"><h3>%s</h3><span>%d metrics tracked</span></div>'
                 % (esc(ent), len(metrics)))
        for metric, m in metrics.items():
            src = m.get("source", {})
            p.append('<div class="metric"><div>'
                     '<div class="metric-name">%s</div>'
                     '<div class="metric-meta">%s &middot; as of %s</div>'
                     '<div class="metric-src">%s</div>'
                     '</div><div class="metric-value">%s</div></div>' % (
                         esc(metric_label(metric)),
                         esc(m.get("period", "")), esc(m.get("as_of", "")),
                         source_line(src),
                         fmt_value(m["value"], m.get("unit", ""))))
        p.append("</div>")
    p.append("</section>")

    # ---- Rejected ----------------------------------------------------------
    p.append('<section id="rejected">')
    p.append('<h2>Rejected outright (%d)</h2>' % len(rejects))
    p.append('<div class="sec-note">Rejected updates never touch the record; they stay visible here with their reason.</div>')
    if not rejects:
        p.append('<div class="empty">No rejections.</div>')
    for a in rejects:
        flags = render_flags(a)
        p.append('<div class="qrow">'
                 '<div class="qrow-top"><div class="qrow-what">%s '
                 '<span class="ent">&middot; %s</span></div>'
                 '<div class="qrow-val">%s</div></div>'
                 '<div class="qrow-why"><span class="tag rejected">Rejected</span>'
                 '<span class="code">%s</span>%s</div>'
                 '<div class="qrow-src">%s &middot; claimed for %s</div></div>' % (
                     esc(metric_label(a["metric"])), esc(a["entity"]),
                     fmt_value(a["claimed_value"], a.get("unit", "")),
                     esc(a["reason_code"]), flags,
                     source_line(candidate_source(a)), esc(a.get("period", ""))))
    p.append("</section>")

    # ---- Decision log ------------------------------------------------------
    p.append('<section id="log">')
    p.append('<h2>Decision log (%d applied &middot; %d held &middot; %d rejected)</h2>'
             % (len(applies), len(holds), len(rejects)))
    p.append('<div class="sec-note">Append-only within a run. Every decision keeps its reason; applied values record what they supersede.</div>')
    for a in audit:
        cls = {"apply": "applied", "hold": "held", "reject": "rejected"}[a["decision"]]
        prov = ""
        if a.get("revision"):
            r = a["revision"]
            prov = '<div class="prov">supersedes %s (%s, as of %s)</div>' % (
                esc(fmt_num(r["supersedes_value"])), esc(r["prior_source_type"]),
                esc(r["prior_as_of"]))
        elif a["decision"] == "apply":
            prov = '<div class="prov">new coverage</div>'
        p.append('<div class="logrow"><time>%s</time>'
                 '<div class="what">%s <span class="ent">&middot; %s</span> '
                 '<span class="val">%s</span>%s</div>'
                 '<span class="tag %s">%s</span></div>' % (
                     esc(a.get("ts", "")), esc(metric_label(a["metric"])),
                     esc(a["entity"]),
                     fmt_value(a["claimed_value"], a.get("unit", "")),
                     prov, cls, esc(DECISION_LABEL[a["decision"]])))
    p.append("</section>")

    p.append('<p class="foot">Audit log is append-only within a run; it is not tamper-proof. '
             'Demo slice: plain-text and CSV-style source excerpts only - PDF parsing, '
             'real spreadsheets and live feeds are not implemented.</p>')
    p.append("</div></body></html>")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(p) + "\n")
    print("wrote %s (%d audit entries)" % (args.out, len(audit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
