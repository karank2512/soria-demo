#!/usr/bin/env python3
"""Tests for review_page.py rendering: grouping, counts, periods, missing
data, escaping, and honesty constraints (no external resources, no emdashes).

These cover presentation logic only; the engine tests live in test_reconcile.py.
"""

import json
import os
import tempfile
import unittest

import review_page


def _src(type_, sid, pub, locator):
    return {"type": type_, "source_id": sid, "pub_date": pub,
            "citation": {"locator": locator}}


def _audit_row(decision, reason, entity, metric, value, unit="percent",
               period="2026-Q3", source_type="government_data",
               source_id="SRC-GOV-DEMO2", flags=None, ts="2026-11-09T13:12:09",
               revision=None):
    row = {"decision": decision, "reason_code": reason, "entity": entity,
           "metric": metric, "claimed_value": value, "unit": unit,
           "period": period, "source_type": source_type, "source_id": source_id,
           "pub_date": "2026-10-14",
           "citation": {"locator": "row 1, column value"},
           "flags": flags or [], "ts": ts}
    if revision:
        row["revision"] = revision
    return row


RECORD = {
    "entities": {
        "Halcyon Health": {"metrics": {
            "mlr_pct": {"value": 84.2, "unit": "percent", "period": "2026-Q2",
                        "as_of": "2026-06-30",
                        "source": _src("regulatory_filing", "SRC-SEED-02",
                                       "2026-08-15", "seed record, synthetic")},
            "ma_enrollment": {"value": 1312000, "unit": "members",
                              "period": "2026-Q3", "as_of": "2026-10-01",
                              "source": _src("government_data", "SRC-GOV-DEMO1",
                                             "2026-10-01", "row 1, column value")},
        }},
    }
}

CONFLICT_AUDIT = [
    _audit_row("apply", "newer_authoritative", "Halcyon Health", "ma_enrollment",
               1312000, unit="members", period="2026-Q3",
               source_id="SRC-GOV-DEMO1",
               revision={"supersedes_value": 1250000,
                         "prior_source_type": "regulatory_filing",
                         "prior_as_of": "2026-06-30"}),
    _audit_row("hold", "conflicting_sources", "Halcyon Health", "mlr_pct", 85.0),
    _audit_row("hold", "conflicting_sources", "Halcyon Health", "mlr_pct", 88.6,
               source_type="news", source_id="SRC-NEWS-DEMO3",
               flags=["low_authority_override"]),
    _audit_row("reject", "unknown_entity", "Merritt Health", "ma_enrollment",
               520000, unit="members", source_type="news",
               source_id="SRC-NEWS-DEMO6"),
]


class RenderBase(unittest.TestCase):
    def render(self, record, audit):
        tmp = tempfile.mkdtemp()
        rp = os.path.join(tmp, "record.json")
        ap = os.path.join(tmp, "audit.jsonl")
        op = os.path.join(tmp, "review.html")
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        with open(ap, "w", encoding="utf-8") as f:
            for row in audit:
                f.write(json.dumps(row) + "\n")
        review_page.main(["--record", rp, "--audit", ap, "--out", op])
        with open(op, "r", encoding="utf-8") as f:
            return f.read()


class TestConflictGrouping(RenderBase):
    def test_conflict_panel_groups_candidates_with_current(self):
        out = self.render(RECORD, CONFLICT_AUDIT)
        self.assertEqual(out.count('class="conflict"'), 1)  # one panel for the pair
        self.assertIn("Current record", out)
        self.assertIn("Candidate 1", out)
        self.assertIn("Candidate 2", out)
        self.assertIn("84.2%", out)
        self.assertIn("85%", out)
        self.assertIn("88.6%", out)

    def test_periods_separated_current_vs_claimed(self):
        out = self.render(RECORD, CONFLICT_AUDIT)
        self.assertIn("2026-Q2 &middot; as of 2026-06-30", out)  # current record period
        self.assertEqual(out.count("claimed for 2026-Q3"), 3)     # 2 candidates + 1 reject row
        self.assertNotIn("claimed for 2026-Q2", out)              # current is never "claimed"

    def test_flag_label_truthful_and_raw_code_retained(self):
        out = self.render(RECORD, CONFLICT_AUDIT)
        self.assertIn("Lower-ranked source", out)
        self.assertIn("low_authority_override", out)          # raw flag retained
        self.assertNotIn("overrides a better source", out)    # no executed-action wording

    def test_conflict_without_current_value(self):
        audit = [_audit_row("hold", "conflicting_sources", "Corvian Medical",
                            "outpatient_visits_q", 1240000, unit="visits")]
        out = self.render(RECORD, audit)
        self.assertIn("No current value for this metric.", out)
        self.assertIn("1,240,000 visits", out)


class TestCountsAndOrder(RenderBase):
    def test_headline_counts(self):
        out = self.render(RECORD, CONFLICT_AUDIT)
        self.assertIn("Held for review (2)", out)
        self.assertIn("Rejected outright (1)", out)
        self.assertIn("Decision log (1 applied &middot; 2 held &middot; 1 rejected)", out)

    def test_review_section_precedes_record(self):
        out = self.render(RECORD, CONFLICT_AUDIT)
        self.assertLess(out.index('id="review"'), out.index('id="record"'))
        self.assertLess(out.index('id="record"'), out.index('id="rejected"'))
        self.assertLess(out.index('id="rejected"'), out.index('id="log"'))

    def test_nav_targets_match_sections(self):
        out = self.render(RECORD, CONFLICT_AUDIT)
        for anchor in ("review", "record", "rejected", "log"):
            self.assertIn('href="#%s"' % anchor, out)
            self.assertIn('id="%s"' % anchor, out)
        self.assertNotIn('href="#queue"', out)
        self.assertNotIn('href="#conflicts"', out)

    def test_other_holds_render_in_review_section(self):
        audit = CONFLICT_AUDIT + [
            _audit_row("hold", "stale_period", "Corvian Medical",
                       "outpatient_visits_q", 1240000, unit="visits",
                       period="2026-Q1", source_type="analyst_note",
                       source_id="SRC-AN-01")]
        out = self.render(RECORD, audit)
        self.assertIn("Held for review (3)", out)
        self.assertIn("Other holds", out)          # subheading splits conflicts from rows
        self.assertIn("stale_period", out)          # raw reason code retained
        self.assertLess(out.index('id="review"'), out.index("stale_period"))
        self.assertLess(out.index("stale_period"), out.index('id="record"'))

    def test_no_holds_renders_empty_message_once(self):
        audit = [r for r in CONFLICT_AUDIT if r["decision"] != "hold"]
        out = self.render(RECORD, audit)
        self.assertIn("Nothing waiting for review.", out)
        self.assertIn("Held for review (0)", out)


class TestEscapingAndHonesty(RenderBase):
    def test_entity_and_metric_escaped(self):
        evil = {"entities": {"Acme <script>alert(1)</script> & Co": {"metrics": {
            "x_<b>_metric": {"value": 1, "unit": "", "period": "2026-Q2",
                             "as_of": "2026-06-30",
                             "source": _src("news", "SRC-<N>", "2026-01-01", "p 1")}}}}}
        audit = [_audit_row("reject", "bad_<source>", "Acme <script>alert(1)</script> & Co",
                            "x_<b>_metric", 2)]
        out = self.render(evil, audit)
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("bad_&lt;source&gt;", out)
        self.assertIn("x &lt;b&gt; metric", out)  # unknown keys humanized, then escaped
        self.assertIn("SRC-&lt;N&gt;", out)

    def test_non_string_value_escaped(self):
        audit = [_audit_row("reject", "unknown_entity", "Merritt Health",
                            "ma_enrollment", "<img src=x>")]
        out = self.render(RECORD, audit)
        self.assertNotIn("<img src=x>", out)
        self.assertIn("&lt;img src=x&gt;", out)

    def test_no_external_resources_or_emdash(self):
        out = self.render(RECORD, CONFLICT_AUDIT)
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("—", out)

    def test_unknown_metric_key_humanized(self):
        audit = [_audit_row("reject", "unknown_entity", "Merritt Health", "icu_beds", 12)]
        out = self.render(RECORD, audit)
        self.assertIn("icu beds", out)

    def test_missing_optional_source_fields(self):
        row = _audit_row("reject", "unknown_entity", "Merritt Health", "ma_enrollment", 5)
        del row["pub_date"]
        del row["citation"]
        del row["source_type"]
        out = self.render(RECORD, [row])
        self.assertIn("SRC-GOV-DEMO2", out)

    def test_synthetic_notice_and_caveats_present(self):
        out = self.render(RECORD, CONFLICT_AUDIT)
        self.assertIn("Every entity, source and value on this page is fictional.", out)
        self.assertIn("not tamper-proof", out)
        self.assertIn("not implemented", out)


if __name__ == "__main__":
    unittest.main()
