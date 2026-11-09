#!/usr/bin/env python3
"""Soria demo: run the visible end-to-end artifact.

Extracts candidates from the curated demo documents (claude CLI, or the
prebaked mock candidates), reconciles them against the seed record, and
renders the review surface.

Usage:
  python3 run_demo.py --mode mock   --outdir out-demo
  python3 run_demo.py --mode claude --outdir out-demo [--claude-bin PATH]
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "claude"], required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--claude-bin", default=None)
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    with open(os.path.join(HERE, "demo_docs", "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)

    candidates = []
    failures = []
    if args.mode == "mock":
        with open(os.path.join(HERE, "demo-candidates.json"), "r", encoding="utf-8") as f:
            prebaked = {c["source_id"]: c for c in json.load(f)}
        for entry in manifest:
            c = dict(prebaked[entry["source_id"]])
            c["batch"] = entry["batch"]
            c["source_id"] = entry["source_id"]
            candidates.append(c)
    else:
        metrics_path = os.path.join(args.outdir, "metrics.json")
        known_metrics = ["ma_enrollment", "mlr_pct", "premium_revenue_musd",
                         "star_rating", "ebitda_margin_pct", "rd_spend_musd",
                         "avg_reimb_rate_pct", "medicaid_enrollment",
                         "outpatient_visits_q"]
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(known_metrics, f)
        for entry in manifest:
            out_path = os.path.join(args.outdir, entry["id"] + ".candidate.json")
            cmd = [sys.executable, os.path.join(HERE, "extract_candidate.py"),
                   "--doc", os.path.join(HERE, "demo_docs", entry["file"]),
                   "--registry", os.path.join(HERE, "registry.json"),
                   "--metrics", metrics_path, "--out", out_path]
            if args.claude_bin:
                cmd += ["--claude-bin", args.claude_bin]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if proc.returncode != 0 or not os.path.exists(out_path):
                failures.append({"id": entry["id"], "stderr": proc.stderr[-400:]})
                continue
            with open(out_path, "r", encoding="utf-8") as f:
                c = json.load(f)
            c["batch"] = entry["batch"]
            c["source_id"] = entry["source_id"]
            candidates.append(c)

    cand_path = os.path.join(args.outdir, "candidates.json")
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2)
        f.write("\n")

    sys.path.insert(0, HERE)
    import reconcile
    import review_page

    audit_path = os.path.join(args.outdir, "audit.jsonl")
    record_out = os.path.join(args.outdir, "record-updated.json")
    report_path = os.path.join(args.outdir, "report.md")
    rc = reconcile.main([
        "--record", os.path.join(HERE, "record.json"),
        "--registry", os.path.join(HERE, "registry.json"),
        "--candidates", cand_path,
        "--audit", audit_path,
        "--record-out", record_out,
        "--report", report_path,
    ])
    if rc != 0:
        return rc
    review_page.main(["--record", record_out, "--audit", audit_path,
                      "--out", os.path.join(args.outdir, "review.html")])
    print(json.dumps({"candidates": len(candidates), "extract_failures": failures}))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
