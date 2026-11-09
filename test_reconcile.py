#!/usr/bin/env python3
"""Stdlib unittest suite for reconcile.py. Run: python3 -m unittest -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reconcile import decide, resolve_entity, citation_ok  # noqa: E402

REGISTRY = {
    "Halcyon Health": {"aliases": ["Halcyon", "Halcyon Health, Inc."]},
    "Corvian Medical": {"aliases": ["Corvian"]},
    "Bexar Therapeutics": {"aliases": ["Bexar"]},
}


def rec(value=1250000, unit="members", as_of="2026-06-30", stype="regulatory_filing"):
    return {"current_value": value, "unit": unit, "as_of": as_of, "source_type": stype}


def cand(**kw):
    base = {"entity": "Halcyon Health", "metric": "ma_enrollment", "value": 1300000,
            "unit": "members", "period": "2026-Q3", "pub_date": "2026-10-01",
            "source_type": "government_data", "source_id": "SRC-T",
            "citation": {"present": True, "locator": "row 2, column value"},
            "unit_or_definition_change": False}
    base.update(kw)
    return base


class TestEntityIdentity(unittest.TestCase):
    def test_alias_resolves(self):
        self.assertEqual(resolve_entity(REGISTRY, "halcyon"),
                         ("Halcyon Health", True))

    def test_unknown_rejected(self):
        r = decide(REGISTRY, rec(), cand(entity="Merritt Health"), [cand(entity="Merritt Health")])
        self.assertEqual((r["decision"], r["reason_code"]), ("reject", "unknown_entity"))


class TestCitation(unittest.TestCase):
    def test_missing_citation_rejected(self):
        c = cand(citation={"present": False, "locator": ""})
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]), ("reject", "missing_citation"))

    def test_vague_locator_rejected(self):
        c = cand(citation={"present": True, "locator": "a recent article"})
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]), ("reject", "missing_citation"))

    def test_specific_locator_ok(self):
        self.assertTrue(citation_ok({"present": True, "locator": "page 4, table 2"}))


class TestStaleness(unittest.TestCase):
    def test_stale_source_held(self):
        c = cand(pub_date="2026-06-30")  # equal to as_of = not newer
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]), ("hold", "stale_source"))


class TestUnits(unittest.TestCase):
    def test_flagged_change_held(self):
        c = cand(unit_or_definition_change=True)
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("hold", "unit_or_definition_change"))

    def test_unit_mismatch_held(self):
        c = cand(value=0.851, unit="ratio", metric="mlr_pct")
        r = decide(REGISTRY, rec(value=84.2, unit="percent"), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("hold", "unit_or_definition_change"))


class TestDeltas(unittest.TestCase):
    def test_large_delta_held(self):
        c = cand(value=1720000)
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]), ("hold", "large_delta"))

    def test_low_authority_override_held(self):
        c = cand(value=1180000, source_type="news")  # 5.6% diff, worse rank
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("hold", "low_authority_override"))
        self.assertIn("low_authority_override", r["flags"])

    def test_redundant_confirmation_rejected(self):
        c = cand(value=1260000, source_type="news")  # 0.8% diff, worse rank
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("reject", "redundant_confirmation"))


class TestBatches(unittest.TestCase):
    def test_conflict_holds_both(self):
        a = cand(value=1300000, source_type="regulatory_filing", source_id="A")
        b = cand(value=1460000, source_type="news", source_id="B")
        ra = decide(REGISTRY, rec(), a, [a, b])
        rb = decide(REGISTRY, rec(), b, [a, b])
        self.assertEqual(ra["reason_code"], "conflicting_sources")
        self.assertEqual(rb["reason_code"], "conflicting_sources")

    def test_agreeing_duplicate_best_applies(self):
        a = cand(value=1300000, source_type="analyst_spreadsheet", source_id="A",
                 pub_date="2026-10-08")
        b = cand(value=1305000, source_type="regulatory_filing", source_id="B",
                 pub_date="2026-10-09")
        ra = decide(REGISTRY, rec(), a, [a, b])
        rb = decide(REGISTRY, rec(), b, [a, b])
        self.assertEqual((ra["decision"], ra["reason_code"]),
                         ("reject", "superseded_duplicate"))
        self.assertEqual((rb["decision"], rb["reason_code"]),
                         ("apply", "authoritative_newer_consistent"))


class TestApply(unittest.TestCase):
    def test_authoritative_apply(self):
        c = cand()
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("apply", "authoritative_newer_consistent"))

    def test_new_coverage_low_authority_flag(self):
        c = cand(metric="medicaid_enrollment", value=410000, source_type="news")
        r = decide(REGISTRY, None, c, [c])
        self.assertEqual((r["decision"], r["reason_code"]), ("apply", "new_coverage"))
        self.assertIn("low_authority", r["flags"])


if __name__ == "__main__":
    unittest.main()
