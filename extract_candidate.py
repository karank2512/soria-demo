#!/usr/bin/env python3
"""Soria demo: extractor wrapper around the claude CLI (no API keys).

Reads ONE synthetic source document, asks the model for a structured candidate
update, validates the schema, and writes candidate JSON. The extractor never
sees expected answers - only the document text plus the entity/metric registry.

Usage:
  python3 extract_candidate.py --doc doc.txt --registry registry.json \
      --metrics metrics.json --out candidate.json [--claude-bin PATH]

Supported input slice: plain-text excerpts and CSV-style text. NOT implemented
in this demo: PDF parsing, real spreadsheets, web fetching, real-time feeds.
"""

import argparse
import json
import os
import subprocess
import sys

PROMPT_TEMPLATE = """You are an extraction component in a SYNTHETIC demonstration pipeline.
The document below is fictional demonstration data, not a real filing.

Task: extract the single metric update the document reports, as strict JSON.

Tracked entities (use the canonical name; aliases in parentheses):
{entities}

Known metrics: {metrics}
(If the document reports a metric not in this list, use a short snake_case name.)

Authority classes for source_type (pick exactly one):
- government_data: government/CMS-style dataset or release
- regulatory_filing: NAIC-style or regulator-filed statement
- company_filing: company-issued report or update
- analyst_spreadsheet: internal analyst spreadsheet note
- news: press or news item

Rules:
- value: the numeric value only (no commas, no units).
- unit: one of members, percent, musd, stars, visits, encounters, ratio - as
  stated in the document. Do NOT convert units.
- period: the reporting period the value is for (e.g. 2026-Q3), as stated.
- pub_date: the document's publication/filing date, YYYY-MM-DD.
- citation.locator: the exact place in the document where the value appears
  (e.g. "row 2, column value", "page 4, table 2", "paragraph 3"). If the
  document gives no locator, set citation.present to false.
- unit_or_definition_change: true ONLY if the document explicitly notes a unit,
  definition, or methodology change.

Output ONLY the JSON object, no prose, no markdown fences:
{{"entity": "...", "metric": "...", "value": 0, "unit": "...", "period": "...",
 "pub_date": "...", "source_type": "...", "citation": {{"present": true, "locator": "..."}},
 "unit_or_definition_change": false}}

DOCUMENT:
{document}
"""

REQUIRED = ["entity", "metric", "value", "unit", "period", "pub_date",
            "source_type", "citation", "unit_or_definition_change"]
SOURCE_TYPES = {"government_data", "regulatory_filing", "company_filing",
                "analyst_spreadsheet", "news"}


def extract_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1])
    raise ValueError("unbalanced JSON in model output")


def validate(cand):
    missing = [k for k in REQUIRED if k not in cand]
    if missing:
        raise ValueError("candidate missing keys: %s" % ", ".join(missing))
    if cand["source_type"] not in SOURCE_TYPES:
        raise ValueError("bad source_type: %r" % cand["source_type"])
    cand["value"] = float(cand["value"])
    if not isinstance(cand["citation"], dict):
        raise ValueError("citation must be an object")
    cand["citation"]["present"] = bool(cand["citation"].get("present"))
    cand["citation"]["locator"] = str(cand["citation"].get("locator", ""))
    cand["unit_or_definition_change"] = bool(cand["unit_or_definition_change"])
    return cand


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--metrics", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--claude-bin",
                    default=os.environ.get("CLAUDE_BIN",
                                           os.path.expanduser("~/.local/bin/claude")))
    args = ap.parse_args(argv)

    with open(args.doc, "r", encoding="utf-8") as f:
        document = f.read()
    with open(args.registry, "r", encoding="utf-8") as f:
        registry = json.load(f)
    with open(args.metrics, "r", encoding="utf-8") as f:
        metrics = json.load(f)

    entities = "\n".join(
        "- %s (aliases: %s)" % (k, ", ".join(v.get("aliases", [])) or "none")
        for k, v in registry.items())
    prompt = PROMPT_TEMPLATE.format(entities=entities,
                                    metrics=", ".join(metrics),
                                    document=document)

    proc = subprocess.run([args.claude_bin, "-p", prompt],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        print("claude CLI failed (%d): %s" % (proc.returncode, proc.stderr[:500]),
              file=sys.stderr)
        return 2
    try:
        cand = validate(extract_json(proc.stdout))
    except (ValueError, json.JSONDecodeError) as e:
        print("extraction parse/validate failed: %s" % e, file=sys.stderr)
        print("raw output head: %s" % proc.stdout[:500], file=sys.stderr)
        return 3

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cand, f, indent=2)
        f.write("\n")
    print(json.dumps(cand))
    return 0


if __name__ == "__main__":
    sys.exit(main())
