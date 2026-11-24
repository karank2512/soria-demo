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
import math
import re
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


def is_finite_number(v):
    """True for int/float values that are finite. bool is not a number here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:  # an int too large to be a float, e.g. 10**400
        return False


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def candidate_error(candidate):
    """Return a short error string for a malformed candidate, else None.

    Hardening only: well-formed candidates (every corpus case) never hit this.
    Types are checked strictly so a malformed field can never reach the record:
    value must be a finite JSON number (not a string, bool, NaN or infinity),
    text fields must be non-empty strings, citation.present must be a boolean.
    """
    if not isinstance(candidate, dict):
        return "candidate is not an object"
    for k in CANDIDATE_REQUIRED:
        if k not in candidate:
            return "missing key: %s" % k
    for k in ("entity", "metric", "unit", "period", "source_type", "pub_date"):
        if not isinstance(candidate[k], str) or not candidate[k].strip():
            return "%s must be a non-empty string: %r" % (k, candidate[k])
    if candidate["metric"] != candidate["metric"].strip():
        # "ma_enrollment " would otherwise bypass every rule as new coverage.
        return "metric has surrounding whitespace: %r" % candidate["metric"]
    if candidate["source_type"] not in AUTHORITY_RANK:
        return "unknown source_type: %r" % candidate["source_type"]
    if not is_finite_number(candidate["value"]):
        return "value is not a finite number: %r" % (candidate["value"],)
    try:
        if not _ISO_DATE_RE.fullmatch(candidate["pub_date"]):
            raise ValueError
        _date(candidate["pub_date"])
    except ValueError:
        return "pub_date is not an ISO date (YYYY-MM-DD): %r" % candidate["pub_date"]
    cit = candidate["citation"]
    if not isinstance(cit, dict):
        return "citation must be an object"
    if not isinstance(cit.get("present"), bool):
        return "citation.present must be true or false: %r" % (cit.get("present"),)
    if "locator" in cit and not isinstance(cit["locator"], str):
        return "citation.locator must be a string: %r" % (cit["locator"],)
    if not isinstance(candidate.get("unit_or_definition_change", False), bool):
        return "unit_or_definition_change must be true or false: %r" % (
            candidate["unit_or_definition_change"],)
    sid = candidate.get("source_id")
    if sid is not None and not isinstance(sid, str):
        return "source_id must be a string: %r" % (sid,)
    return None


# A standalone number: not glued to a word, a decimal point or a hyphen on the
# left (so the parts of 2026-10-12 are not read as 10 or 12), and not followed
# by "-<digit>" (so 2026 in 2026-10-12 is not read either).
_NUMBER_RE = re.compile(r"(?<![\w.\-])-?\d[\d,]*(?:\.\d+)?(?![\d.]*-\d)")


_GROUPED_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?")


def value_in_source(value, text):
    """True if the numeric value appears literally in the source text.

    Numbers in the text are read with thousands separators removed
    ("1,312,000" -> 1312000). This binds a citation to its document: a value
    the extractor reports but the document never states is not verified. It
    does not prove the number sits at the cited locator.
    """
    if not isinstance(text, str):
        return False
    target = float(value)
    for tok in _NUMBER_RE.findall(text):
        tok = tok.rstrip(",")
        if _GROUPED_RE.fullmatch(tok):
            readings = [tok.replace(",", "")]     # 1,312,000 -> 1312000
        else:
            readings = tok.split(",")             # CSV cells: 1312000,2026
        for r in readings:
            try:
                if r and float(r) == target:
                    return True
            except ValueError:
                continue
    return False


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


def _usable_peer(c):
    """A batch peer only counts if it could itself be decided on its merits.

    Malformed or uncited candidates are rejected on their own; they must not
    hold or supersede a valid candidate (and must not crash the batch).
    """
    return candidate_error(c) is None and citation_ok(c.get("citation"))


# Passed as source_text when source documents were supplied but none matches
# this candidate's source_id: the value cannot be verified, so it is held.
SOURCE_MISSING = object()


def decide(registry, record_entry, candidate, batch, source_text=None):
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
    source_text: optional text of the source document. When given, a value
        that does not appear in it is held (value_not_in_source). When None,
        the check is skipped (the corpus harness and plain CLI pass no text).
        SOURCE_MISSING means documents were supplied but not this one: held
        as source_not_found.
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

    # Rule 1b: the cited value must appear in the source document (when the
    # document text is available to check against).
    if source_text is SOURCE_MISSING:
        return {"decision": "hold", "reason_code": "source_not_found",
                "flags": flags, "canonical_entity": canon}
    if source_text is not None and not value_in_source(candidate["value"], source_text):
        return {"decision": "hold", "reason_code": "value_not_in_source",
                "flags": flags, "canonical_entity": canon}

    tracked = record_entry is not None
    if tracked and not is_finite_number(record_entry.get("current_value")):
        # A corrupt record value must never be silently replaced or compared.
        return {"decision": "hold", "reason_code": "invalid_record_value",
                "flags": flags, "canonical_entity": canon}
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
        if other is candidate or not _usable_peer(other):
            continue
        ocanon, oknown = resolve_entity(registry, other.get("entity"))
        if oknown and ocanon == canon and other.get("metric") == candidate.get("metric"):
            peers.append(other)
    if peers:
        values = [float(candidate["value"])] + [float(p["value"]) for p in peers]
        if _pct_diff(max(values), min(values)) > CONFLICT_THRESHOLD:
            return {"decision": "hold", "reason_code": "conflicting_sources",
                    "flags": flags, "canonical_entity": canon}
        # Agree within 2%: the best-authority (tie: latest pub_date, then
        # earliest in the batch, so exactly one of a tied pair applies).
        group = [candidate] + peers
        position = {id(c): i for i, c in enumerate(batch)}
        def sort_key(c):
            return (AUTHORITY_RANK.get(c.get("source_type"), 99),
                    -_date(c["pub_date"]).toordinal(),
                    position.get(id(c), -1))
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


def record_problems(record):
    """Return a list of problems with a record file (empty when usable)."""
    problems = []
    if not isinstance(record, dict) or not isinstance(record.get("entities"), dict):
        return ["record must be an object with an 'entities' object"]
    for ent, edata in record["entities"].items():
        metrics = edata.get("metrics") if isinstance(edata, dict) else None
        if not isinstance(metrics, dict):
            problems.append("%s: 'metrics' must be an object" % ent)
            continue
        for metric, m in metrics.items():
            where = "%s / %s" % (ent, metric)
            if not isinstance(m, dict):
                problems.append("%s: entry must be an object" % where)
                continue
            if not is_finite_number(m.get("value")):
                problems.append("%s: value must be a finite number" % where)
            for k in ("unit", "as_of"):
                if not isinstance(m.get(k), str):
                    problems.append("%s: %s must be a string" % (where, k))
            try:
                _date(m.get("as_of", ""))
            except (TypeError, ValueError):
                problems.append("%s: as_of must be an ISO date" % where)
            src = m.get("source")
            st = src.get("type") if isinstance(src, dict) else None
            if st not in AUTHORITY_RANK:
                problems.append("%s: source.type must be one of %s"
                                % (where, ", ".join(AUTHORITY_RANK)))
    return problems


def registry_problems(registry):
    """Return a list of problems with a registry file (empty when usable)."""
    if not isinstance(registry, dict) or not registry:
        return ["registry must be a non-empty object"]
    problems = []
    for name, info in registry.items():
        if not isinstance(info, dict):
            problems.append("%s: entry must be an object" % name)
            continue
        aliases = info.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            problems.append("%s: aliases must be a list of strings" % name)
    return problems


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def record_entry_for(registry, record, cand):
    """The decide() view of the record's current value for this candidate."""
    if not isinstance(cand, dict):
        return None
    canon, _known = resolve_entity(registry, cand.get("entity"))
    metric = cand.get("metric")
    if not canon or not isinstance(metric, str):
        return None
    entry = record.get("entities", {}).get(canon, {}).get("metrics", {}).get(metric)
    if not entry:
        return None
    return {
        "current_value": entry.get("value"),
        "unit": entry.get("unit"),
        "as_of": entry.get("as_of"),
        "source_type": (entry.get("source") or {}).get("type"),
    }


def apply_value(record, canon, cand):
    """Write an applied candidate into the record; return the prior entry."""
    ent = record.setdefault("entities", {}).setdefault(canon, {"metrics": {}})
    metrics = ent.setdefault("metrics", {})
    prior = metrics.get(cand["metric"])
    metrics[cand["metric"]] = {
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
    return prior


def batch_id_of(cand):
    """The candidate's batch id as a string; None when the id is unusable.

    Integer ids are kept distinct (1 and 2 are two batches) as before.
    """
    if not isinstance(cand, dict):
        return None  # a non-object never claims a batch id
    b = cand.get("batch", "default")
    if isinstance(b, int) and not isinstance(b, bool):
        return str(b)
    if isinstance(b, str) and b.strip():
        return b
    return None


def reconcile_batches(registry, record, candidates, source_texts=None):
    """Decide and apply candidates batch by batch, in input order.

    Within a batch every candidate is decided against the record as it stood
    at the start of that batch. Each batch's applies are written before the
    next batch is decided, so a later batch sees what an earlier one applied
    (an older value in batch 2 can no longer overwrite a newer one from batch
    1 unnoticed). source_texts optionally maps source_id -> document text for
    the value_not_in_source check.

    Returns (decisions, new_record). The input record is not mutated. Each
    decision is {"batch", "candidate", "result", "prior"}; prior is the
    superseded record entry for applies (None for new coverage).
    """
    record = copy.deepcopy(record)
    batches = {}
    bad_batch = []
    for c in candidates:
        b = batch_id_of(c)
        if b is None:
            bad_batch.append(c)
        else:
            batches.setdefault(b, []).append(c)

    def text_for(cand):
        if source_texts is None or not isinstance(cand, dict):
            return None
        sid = cand.get("source_id")
        if sid is not None and not isinstance(sid, str):
            return None  # malformed: decide() rejects it as invalid_candidate
        return source_texts.get(sid, SOURCE_MISSING)

    decisions = []
    for cand in bad_batch:
        decisions.append({"batch": "invalid", "candidate": cand, "prior": None, "result": {
            "decision": "reject", "reason_code": "invalid_candidate", "flags": [],
            "canonical_entity": None,
            "error": ("candidate is not an object" if not isinstance(cand, dict) else
                      "batch must be a non-empty string or integer: %r"
                      % (cand.get("batch"),))}})
    for batch_id, group in batches.items():
        # Only candidates whose value is confirmed by their own document may
        # act as peers (a fabricated value must not outrank a verified one).
        if source_texts is not None:
            peers = []
            for c in group:
                t = text_for(c)
                if (_usable_peer(c) and t is not SOURCE_MISSING
                        and value_in_source(c["value"], t)):
                    peers.append(c)
        else:
            peers = group
        batch_decisions = []
        for cand in group:
            entry = record_entry_for(registry, record, cand)
            result = decide(registry, entry, cand, peers, source_text=text_for(cand))
            before = None
            if entry is not None and result["decision"] == "hold":
                src = record["entities"][result["canonical_entity"]]["metrics"][cand["metric"]]
                before = {"value": src.get("value"), "as_of": src.get("as_of"),
                          "source_id": (src.get("source") or {}).get("source_id")}
            batch_decisions.append({"batch": batch_id, "candidate": cand,
                                    "result": result, "prior": None,
                                    "record_before": before})
        for d in batch_decisions:
            if d["result"]["decision"] == "apply":
                d["prior"] = apply_value(record, d["result"]["canonical_entity"],
                                         d["candidate"])
        decisions.extend(batch_decisions)
    return decisions, record


def audit_line(d, ts):
    """One audit-log entry for a decision from reconcile_batches()."""
    cand = d["candidate"] if isinstance(d["candidate"], dict) else {}
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
    if res.get("error"):
        line["error"] = res["error"]
    prior = d.get("prior")
    if res["decision"] == "apply" and prior:
        line["revision"] = {
            "supersedes_value": prior.get("value"),
            "prior_source_type": (prior.get("source") or {}).get("type"),
            "prior_as_of": prior.get("as_of"),
        }
    return line


def dumps_safe(obj, **kw):
    """json.dumps that refuses NaN/Infinity instead of writing invalid JSON.

    Malformed candidate fields (e.g. a NaN value) are kept in the audit trail
    as their repr so a rejected input stays inspectable without breaking the
    file for strict JSON readers.
    """
    try:
        return json.dumps(obj, allow_nan=False, **kw)
    except ValueError:
        return json.dumps(_sanitize(obj), allow_nan=False, **kw)


def _sanitize(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return repr(obj)
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def report_lines(decisions):
    lines = ["# Reconciliation report (synthetic demo)", ""]
    for d in decisions:
        cand = d["candidate"] if isinstance(d["candidate"], dict) else {}
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
    return lines


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
    except (OSError, ValueError, RecursionError) as e:
        print("error loading inputs: %s" % e, file=sys.stderr)
        return 2
    if not isinstance(candidates, list):
        print("error: candidates file must be a JSON list", file=sys.stderr)
        return 2
    problems = record_problems(record) + registry_problems(registry)
    if problems:
        print("error: input files are not usable:\n  " + "\n  ".join(problems),
              file=sys.stderr)
        return 2

    decisions, record = reconcile_batches(registry, record, candidates)

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with open(args.audit, "a", encoding="utf-8") as f:
        for d in decisions:
            f.write(dumps_safe(audit_line(d, ts)) + "\n")

    with open(args.record_out, "w", encoding="utf-8") as f:
        f.write(dumps_safe(record, indent=2) + "\n")

    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines(decisions)) + "\n")

    counts = {"apply": 0, "hold": 0, "reject": 0}
    for d in decisions:
        counts[d["result"]["decision"]] += 1
    print(json.dumps({"decisions": len(decisions), "counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
