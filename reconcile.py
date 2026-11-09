#!/usr/bin/env python3
"""Soria demo: deterministic source-reconciliation engine (stdlib only, Python 3.9+).

Reads a record file and candidate updates (JSON), applies the Meridian Research
SOP (rule-pack.md) mechanically, and emits decisions, an audit log and an
updated record. The engine never invents values: it only decides on candidates
proposed by an extractor. Every decision carries a reason code; applied values
keep their citation. The audit log is append-only within a run; it is NOT
tamper-proof.

All data in this demo is synthetic and labeled as such.

CLI:
  python3 reconcile.py --record record.json --registry registry.json \
      --candidates candidates.json --audit audit.jsonl --record-out record_new.json \
      --report report.md

Library use (tests / corpora validation):
  from reconcile import decide, resolve_entity
  result = decide(registry, record_entry, candidate, batch_candidates)
"""

import argparse
import copy
import datetime
import json
import sys

AUTHORITY_RANK = {
    "government_data": 1,
    "regulatory_filing": 2,
    "company_filing": 3,
    "analyst_spreadsheet": 4,
    "news": 5,
}

VAGUE_LOCATORS = {
    "a recent filing", "a filing", "an article", "a recent article",
    "recent news", "a report", "online", "press release", "a press release",
}

CONFLICT_THRESHOLD = 0.02      # >2% apart in the same batch = conflict
LARGE_DELTA_THRESHOLD = 0.25   # >25% jump vs current value = hold


def _date(s):
    return datetime.date.fromisoformat(s)


CANDIDATE_REQUIRED = ["entity", "metric", "value", "unit", "period",
                      "pub_date", "source_type", "citation"]


def candidate_error(candidate):
    """Return a short error string for a malformed candidate, else None.

    Hardening only: well-formed candidates (every corpus case) never hit this.
    """
    if not isinstance(candidate, dict):
        return "candidate is not an object"
    for k in CANDIDATE_REQUIRED:
        if k not in candidate:
            return "missing key: %s" % k
    if candidate["source_type"] not in AUTHORITY_RANK:
        return "unknown source_type: %r" % candidate["source_type"]
    try:
        float(candidate["value"])
    except (TypeError, ValueError):
        return "value is not numeric: %r" % candidate["value"]
    try:
        _date(str(candidate["pub_date"]))
    except ValueError:
        return "pub_date is not ISO: %r" % candidate["pub_date"]
    return None


def resolve_entity(registry, name):
    """Return (canonical_name, known_bool). Case-insensitive; checks aliases."""
    if not name:
        return None, False
    n = str(name).strip().lower()
    for canon, info in registry.items():
        if n == canon.lower():
            return canon, True
        for alias in info.get("aliases", []):
            if n == alias.lower():
                return canon, True
    return None, False


def citation_ok(citation):
    if not isinstance(citation, dict):
        return False
    if not citation.get("present"):
        return False
    locator = str(citation.get("locator", "")).strip().lower()
    if not locator or locator in VAGUE_LOCATORS:
        return False
    return True


def _pct_diff(a, b):
    """Relative difference vs the smaller magnitude."""
    lo = min(abs(a), abs(b))
    if lo == 0:
        return 0.0 if a == b else float("inf")
    return abs(a - b) / lo


def decide(registry, record_entry, candidate, batch):
    """Apply the SOP to one candidate.

    registry: entity registry dict (canonical -> {"aliases": [...]})
    record_entry: None when the metric is untracked, else
        {"current_value": float, "unit": str, "as_of": "YYYY-MM-DD",
         "source_type": one of AUTHORITY_RANK}
    candidate: extractor output
        {"entity", "metric", "value", "unit", "period", "pub_date",
         "source_type", "source_id", "citation": {"present", "locator"},
         "unit_or_definition_change": bool}
    batch: list of all candidates in the same batch (including this one)
    Returns {"decision", "reason_code", "flags", "canonical_entity"}.
    """
    flags = []
    err = candidate_error(candidate)
    if err:
        return {"decision": "reject", "reason_code": "invalid_candidate",
                "flags": flags, "canonical_entity": None, "error": err}
    canon, known = resolve_entity(registry, candidate.get("entity"))
    if not known:
        return {"decision": "reject", "reason_code": "unknown_entity",
                "flags": flags, "canonical_entity": None}

    # Rule 1: citation required, specific locator.
    if not citation_ok(candidate.get("citation")):
        return {"decision": "reject", "reason_code": "missing_citation",
                "flags": flags, "canonical_entity": canon}

    tracked = record_entry is not None
    rank = AUTHORITY_RANK.get(candidate.get("source_type"), 99)
    cur_rank = AUTHORITY_RANK.get(record_entry["source_type"], 99) if tracked else None
    diff_vs_current = None
    if tracked:
        cur = float(record_entry["current_value"])
        val = float(candidate["value"])
        diff_vs_current = 0.0 if cur == 0 else abs(val - cur) / abs(cur)
        if cur_rank is not None and rank > cur_rank and diff_vs_current > CONFLICT_THRESHOLD:
            flags.append("low_authority_override")

    # Rule 2: stale source.
    if tracked and _date(candidate["pub_date"]) <= _date(record_entry["as_of"]):
        return {"decision": "hold", "reason_code": "stale_source",
                "flags": flags, "canonical_entity": canon}

    # Rule 3: unit or definition change (flagged by extractor, or unit mismatch).
    if candidate.get("unit_or_definition_change"):
        return {"decision": "hold", "reason_code": "unit_or_definition_change",
                "flags": flags, "canonical_entity": canon}
    if tracked and str(candidate.get("unit", "")).strip().lower() != \
            str(record_entry.get("unit", "")).strip().lower():
        return {"decision": "hold", "reason_code": "unit_or_definition_change",
                "flags": flags, "canonical_entity": canon}

    # Rule 4: large delta.
    if tracked and diff_vs_current is not None and diff_vs_current > LARGE_DELTA_THRESHOLD:
        return {"decision": "hold", "reason_code": "large_delta",
                "flags": flags, "canonical_entity": canon}

    # Rule 5: same-batch duplicates / conflicts on entity+metric.
    peers = []
    for other in batch:
        if other is candidate:
            continue
        ocanon, oknown = resolve_entity(registry, other.get("entity"))
        if oknown and ocanon == canon and other.get("metric") == candidate.get("metric"):
            peers.append(other)
    if peers:
        values = [float(candidate["value"])] + [float(p["value"]) for p in peers]
        if _pct_diff(max(values), min(values)) > CONFLICT_THRESHOLD:
            return {"decision": "hold", "reason_code": "conflicting_sources",
                    "flags": flags, "canonical_entity": canon}
        # Agree within 2%: the best-authority (tie: latest pub_date) applies.
        group = [candidate] + peers
        def sort_key(c):
            return (AUTHORITY_RANK.get(c.get("source_type"), 99),
                    -_date(c["pub_date"]).toordinal())
        best = sorted(group, key=sort_key)[0]
        if best is not candidate:
            return {"decision": "reject", "reason_code": "superseded_duplicate",
                    "flags": flags, "canonical_entity": canon}
        # fall through to apply below

    # Rule 6: low-authority override / redundant confirmation.
    if tracked and cur_rank is not None and rank > cur_rank:
        if diff_vs_current > CONFLICT_THRESHOLD:
            return {"decision": "hold", "reason_code": "low_authority_override",
                    "flags": flags, "canonical_entity": canon}
        return {"decision": "reject", "reason_code": "redundant_confirmation",
                "flags": flags, "canonical_entity": canon}

    # Rule 7: apply.
    if not tracked and rank >= 4:
        flags.append("low_authority")
    reason = "authoritative_newer_consistent" if tracked else "new_coverage"
    return {"decision": "apply", "reason_code": reason,
            "flags": flags, "canonical_entity": canon}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic SOP reconciler (synthetic demo).")
    ap.add_argument("--record", required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--candidates", required=True,
                    help="JSON list of candidates; each carries a 'batch' field.")
    ap.add_argument("--audit", required=True, help="audit JSONL, appended to")
    ap.add_argument("--record-out", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args(argv)

    try:
        registry = load_json(args.registry)
        record = load_json(args.record)
        candidates = load_json(args.candidates)
    except (OSError, json.JSONDecodeError) as e:
        print("error loading inputs: %s" % e, file=sys.stderr)
        return 2
    if not isinstance(candidates, list):
        print("error: candidates file must be a JSON list", file=sys.stderr)
        return 2

    # Group candidates by batch, preserving order.
    batches = {}
    for c in candidates:
        batches.setdefault(c.get("batch", "default"), []).append(c)

    decisions = []
    for batch_id, group in batches.items():
        # Decide every candidate against the record state at batch start.
        snapshot = copy.deepcopy(record)
        for cand in group:
            canon, _known = resolve_entity(registry, cand.get("entity"))
            entry = None
            if canon:
                entry = snapshot["entities"].get(canon, {}).get("metrics", {}).get(
                    cand.get("metric"))
            rec_entry = None
            if entry:
                rec_entry = {
                    "current_value": entry["value"],
                    "unit": entry["unit"],
                    "as_of": entry["as_of"],
                    "source_type": entry["source"]["type"],
                }
            result = decide(registry, rec_entry, cand, group)
            decisions.append({"batch": batch_id, "candidate": cand, "result": result})

    # Apply decisions (each applied value becomes a revision in the record).
    applied = []
    for d in decisions:
        if d["result"]["decision"] != "apply":
            continue
        cand = d["candidate"]
        canon = d["result"]["canonical_entity"]
        ent = record["entities"].setdefault(canon, {"metrics": {}})
        prior = ent["metrics"].get(cand["metric"])
        ent["metrics"][cand["metric"]] = {
            "value": cand["value"],
            "unit": cand["unit"],
            "period": cand["period"],
            "as_of": cand["pub_date"],
            "source": {
                "type": cand["source_type"],
                "source_id": cand.get("source_id"),
                "pub_date": cand["pub_date"],
                "citation": cand.get("citation"),
            },
        }
        applied.append((d, prior))

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with open(args.audit, "a", encoding="utf-8") as f:
        for d in decisions:
            cand = d["candidate"]
            res = d["result"]
            line = {
                "ts": ts,
                "batch": d["batch"],
                "entity": res["canonical_entity"] or cand.get("entity"),
                "metric": cand.get("metric"),
                "claimed_value": cand.get("value"),
                "unit": cand.get("unit"),
                "period": cand.get("period"),
                "source_id": cand.get("source_id"),
                "source_type": cand.get("source_type"),
                "pub_date": cand.get("pub_date"),
                "decision": res["decision"],
                "reason_code": res["reason_code"],
                "flags": res["flags"],
                "citation": cand.get("citation"),
            }
            if res["decision"] == "apply":
                prior = next((p for dd, p in applied if dd is d), None)
                if prior:
                    line["revision"] = {
                        "supersedes_value": prior["value"],
                        "prior_source_type": prior["source"]["type"],
                        "prior_as_of": prior["as_of"],
                    }
            f.write(json.dumps(line) + "\n")

    with open(args.record_out, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")

    # Human-readable change report.
    lines = ["# Reconciliation report (synthetic demo)", ""]
    for d in decisions:
        cand = d["candidate"]
        res = d["result"]
        who = res["canonical_entity"] or cand.get("entity")
        verb = {"apply": "APPLIED", "hold": "HELD FOR REVIEW", "reject": "REJECTED"}[
            res["decision"]]
        lines.append("- %s | %s / %s = %s %s (%s, %s, pub %s) - %s"
                     % (verb, who, cand.get("metric"), cand.get("value"),
                        cand.get("unit"), cand.get("source_id"),
                        cand.get("source_type"), cand.get("pub_date"),
                        res["reason_code"]))
        if res["flags"]:
            lines.append("  flags: %s" % ", ".join(res["flags"]))
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    counts = {"apply": 0, "hold": 0, "reject": 0}
    for d in decisions:
        counts[d["result"]["decision"]] += 1
    print(json.dumps({"decisions": len(decisions), "counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
