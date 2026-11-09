#!/usr/bin/env python3
"""Soria demo: render the analyst review surface as a self-contained HTML page.

Inputs: updated record.json + audit.jsonl (both produced by reconcile.py).
Output: review.html - record with per-value provenance, the hold-for-review
queue with reasons, rejections, and the decision log. No external resources.

All data is synthetic and labeled as such.
"""

import argparse
import html
import json

DECISION_LABEL = {"apply": "APPLIED", "hold": "HELD FOR REVIEW", "reject": "REJECTED"}


def esc(s):
    return html.escape(str(s))


def fmt_num(v):
    """Display 1312000.0 as 1312000; leave real decimals alone."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else str(f)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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

    p = []
    p.append("""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Meridian Research - source reconciliation review (SYNTHETIC DEMO)</title>
<style>
body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:32px;color:#1a1a1a;max-width:1080px}
.banner{background:#fff3cd;border:1px solid #e0c56b;padding:10px 14px;border-radius:6px;font-weight:600}
h1{font-size:22px;margin:18px 0 4px}h2{font-size:17px;margin:26px 0 8px;border-bottom:2px solid #ddd;padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border:1px solid #ccc;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f0f0f0}
.applied{background:#e8f5e9}.held{background:#fff8e1}.rejected{background:#ffebee}
.reason{font-family:Menlo,monospace;font-size:12.5px}
.flag{display:inline-block;background:#ede7f6;border-radius:4px;padding:1px 6px;font-size:12px;margin-right:4px}
.small{color:#555;font-size:12.5px}
</style></head><body>""")
    p.append('<div class="banner">SYNTHETIC DEMONSTRATION - all entities, sources and values on this page are fictional.</div>')
    p.append("<h1>Meridian Research - source reconciliation review</h1>")
    p.append('<div class="small">Every record value carries its source. Updates are applied, held, or rejected by the deterministic SOP; nothing is overwritten silently.</div>')

    # Record table
    p.append("<h2>Current record (with provenance)</h2><table>")
    p.append("<tr><th>Entity</th><th>Metric</th><th>Value</th><th>Period</th><th>As of</th><th>Source</th><th>Citation</th></tr>")
    for ent, edata in record["entities"].items():
        for metric, m in edata.get("metrics", {}).items():
            src = m.get("source", {})
            cit = (src.get("citation") or {}).get("locator", "")
            p.append("<tr><td>%s</td><td>%s</td><td>%s %s</td><td>%s</td><td>%s</td><td>%s (%s, pub %s)</td><td>%s</td></tr>" % (
                esc(ent), esc(metric), esc(fmt_num(m["value"])), esc(m.get("unit", "")),
                esc(m.get("period", "")), esc(m.get("as_of", "")),
                esc(src.get("type", "")), esc(src.get("source_id", "")),
                esc(src.get("pub_date", "")), esc(cit)))
    p.append("</table>")

    # Review queue and rejections
    holds = [a for a in audit if a["decision"] == "hold"]
    rejects = [a for a in audit if a["decision"] == "reject"]
    applies = [a for a in audit if a["decision"] == "apply"]

    def queue_rows(items):
        rows = []
        for a in items:
            flags = " ".join('<span class="flag">%s</span>' % esc(fl) for fl in a.get("flags", []))
            rows.append("<tr><td>%s</td><td>%s</td><td>%s %s</td><td>%s</td><td>%s (%s, pub %s)</td><td class='reason'>%s</td><td>%s</td></tr>" % (
                esc(a["entity"]), esc(a["metric"]), esc(fmt_num(a["claimed_value"])),
                esc(a.get("unit", "")), esc(a.get("period", "")),
                esc(a.get("source_type", "")), esc(a.get("source_id", "")),
                esc(a.get("pub_date", "")), esc(a["reason_code"]), flags))
        return rows

    p.append("<h2>Held for analyst review (%d)</h2>" % len(holds))
    if holds:
        p.append("<table><tr><th>Entity</th><th>Metric</th><th>Claimed</th><th>Period</th><th>Source</th><th>Reason</th><th>Flags</th></tr>")
        p.extend(queue_rows(holds))
        p.append("</table>")
    else:
        p.append('<div class="small">Nothing waiting for review.</div>')

    p.append("<h2>Rejected (%d)</h2>" % len(rejects))
    if rejects:
        p.append("<table><tr><th>Entity</th><th>Metric</th><th>Claimed</th><th>Period</th><th>Source</th><th>Reason</th><th>Flags</th></tr>")
        p.extend(queue_rows(rejects))
        p.append("</table>")
    else:
        p.append('<div class="small">No rejections.</div>')

    # Decision log
    p.append("<h2>Decision log (%d applied, %d held, %d rejected)</h2>" % (
        len(applies), len(holds), len(rejects)))
    p.append("<table><tr><th>Time</th><th>Entity</th><th>Metric</th><th>Claimed</th><th>Decision</th><th>Reason</th><th>Provenance</th></tr>")
    for a in audit:
        cls = {"apply": "applied", "hold": "held", "reject": "rejected"}[a["decision"]]
        prov = ""
        if a.get("revision"):
            r = a["revision"]
            prov = "revision: supersedes %s (%s, as of %s)" % (
                esc(fmt_num(r["supersedes_value"])), esc(r["prior_source_type"]), esc(r["prior_as_of"]))
        elif a["decision"] == "apply":
            prov = "new coverage"
        p.append("<tr class='%s'><td>%s</td><td>%s</td><td>%s</td><td>%s %s</td><td>%s</td><td class='reason'>%s</td><td>%s</td></tr>" % (
            cls, esc(a.get("ts", "")), esc(a["entity"]), esc(a["metric"]),
            esc(fmt_num(a["claimed_value"])), esc(a.get("unit", "")),
            DECISION_LABEL[a["decision"]], esc(a["reason_code"]), prov))
    p.append("</table>")
    p.append('<div class="small">Audit log is append-only within a run; it is not tamper-proof. Demo slice: plain-text and CSV-style source excerpts only - PDF parsing, real spreadsheets and live feeds are not implemented.</div>')
    p.append("</body></html>")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(p) + "\n")
    print("wrote %s (%d audit entries)" % (args.out, len(audit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
