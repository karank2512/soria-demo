#!/usr/bin/env python3
"""Soria demo: corpus harness. Runs scenarios through extract + reconcile and
scores real numbers.

Modes:
  --mode mock    uses expected_candidate as the extractor output; proves the
                 harness + reconciler end to end without any model call.
  --mode claude  calls extract_candidate.py (claude CLI) per scenario.

The extractor input is ONLY the scenario's 'document' text. expected_candidate
and expected stay inside this harness and the corpus file; nothing writes them
where the extractor can read them.

Usage:
  python3 run_holdout.py --corpus corpus-dev-12.json --mode mock --outdir out-dev
  python3 run_holdout.py --corpus corpus-frozen-33.json --mode claude \
      --outdir out-frozen [--ids F01,F02] [--limit 5]
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def record_entry(snapshot):
    if not snapshot["tracked"]:
        return None
    cur = snapshot["current"]
    return {"current_value": cur["value"], "unit": cur["unit"],
            "as_of": cur["as_of"], "source_type": cur["source_type"]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--mode", choices=["mock", "claude"], required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--ids", default=None, help="comma-separated scenario ids")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--claude-bin", default=None)
    args = ap.parse_args(argv)

    sys.path.insert(0, HERE)
    from reconcile import decide  # noqa

    with open(args.corpus, "r", encoding="utf-8") as f:
        corpus = json.load(f)
    scenarios = corpus["scenarios"]
    if args.ids:
        wanted = set(args.ids.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    if args.limit:
        scenarios = scenarios[: args.limit]

    registry_path = os.path.join(HERE, "registry.json")
    with open(registry_path, "r", encoding="utf-8") as f:
        registry = json.load(f)
    metrics = sorted({s["record"]["metric"] for s in corpus["scenarios"]})
    metrics_path = os.path.join(args.outdir, "metrics.json")
    docs_dir = os.path.join(args.outdir, "docs")
    cands_dir = os.path.join(args.outdir, "candidates")
    os.makedirs(docs_dir, exist_ok=True)
    os.makedirs(cands_dir, exist_ok=True)
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f)

    # Step 1: obtain a candidate per scenario.
    candidates = {}
    extract_failures = []
    for s in scenarios:
        if args.mode == "mock":
            candidates[s["id"]] = dict(s["expected_candidate"])
            continue
        doc_path = os.path.join(docs_dir, s["id"] + ".txt")
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write(s["document"])
        out_path = os.path.join(cands_dir, s["id"] + ".json")
        cmd = [sys.executable, os.path.join(HERE, "extract_candidate.py"),
               "--doc", doc_path, "--registry", registry_path,
               "--metrics", metrics_path, "--out", out_path]
        if args.claude_bin:
            cmd += ["--claude-bin", args.claude_bin]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0 or not os.path.exists(out_path):
            extract_failures.append({"id": s["id"], "stderr": proc.stderr[-400:]})
            continue
        with open(out_path, "r", encoding="utf-8") as f:
            candidates[s["id"]] = json.load(f)

    # Step 2: decide per batch (only scenarios with a candidate).
    by_batch = {}
    for s in scenarios:
        if s["id"] in candidates:
            by_batch.setdefault(s["batch"], []).append(s)

    rows = []
    for batch_id, group in by_batch.items():
        batch_cands = [candidates[s["id"]] for s in group]
        for s in group:
            got = decide(registry, record_entry(s["record"]),
                         candidates[s["id"]], batch_cands)
            exp = s["expected"]
            exp_cand = s["expected_candidate"]
            got_cand = candidates[s["id"]]
            decision_ok = (got["decision"] == exp["decision"]
                           and got["reason_code"] == exp["reason_code"])
            wrong_auto_apply = (
                got["decision"] == "apply"
                and (exp["decision"] != "apply"
                     or float(got_cand["value"]) != float(exp_cand["value"])))
            field_hits = {}
            for k in ["metric", "unit", "period", "pub_date", "source_type"]:
                field_hits[k] = str(got_cand.get(k)) == str(exp_cand.get(k))
            field_hits["value"] = float(got_cand.get("value", float("nan"))) == \
                float(exp_cand["value"])
            ecanon, _ = __import__("reconcile").resolve_entity(
                registry, got_cand.get("entity"))
            field_hits["entity"] = ecanon == exp_cand["entity"]
            rows.append({
                "id": s["id"], "batch": batch_id,
                "expected_decision": exp["decision"],
                "expected_reason": exp["reason_code"],
                "got_decision": got["decision"],
                "got_reason": got["reason_code"],
                "decision_ok": decision_ok,
                "wrong_auto_apply": wrong_auto_apply,
                "extraction_fields": field_hits,
            })

    n = len(rows)
    ok = sum(1 for r in rows if r["decision_ok"])
    wrong = [r["id"] for r in rows if r["wrong_auto_apply"]]
    field_totals = {}
    for r in rows:
        for k, v in r["extraction_fields"].items():
            field_totals.setdefault(k, [0, 0])
            field_totals[k][1] += 1
            field_totals[k][0] += 1 if v else 0
    summary = {
        "mode": args.mode,
        "corpus": os.path.basename(args.corpus),
        "scenarios_run": n,
        "extract_failures": extract_failures,
        "decisions_correct": ok,
        "decisions_wrong": n - ok,
        "wrong_auto_applies": wrong,
        "wrong_auto_apply_count": len(wrong),
        "extraction_field_accuracy": {
            k: "%d/%d" % tuple(v) for k, v in field_totals.items()},
        "rows": rows,
    }
    with open(os.path.join(args.outdir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
