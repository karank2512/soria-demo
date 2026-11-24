#!/usr/bin/env python3
"""Stdlib unittest suite for reconcile.py. Run: python3 -m unittest -v"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reconcile import (decide, resolve_entity, citation_ok, value_in_source,  # noqa: E402
                       reconcile_batches, dumps_safe)

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


class TestHardening(unittest.TestCase):
    def test_unknown_source_type_rejected(self):
        c = cand(source_type="blog")
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("reject", "invalid_candidate"))
        self.assertIn("source_type", r["error"])

    def test_missing_key_rejected(self):
        c = cand()
        del c["value"]
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("reject", "invalid_candidate"))

    def test_bad_pubdate_rejected(self):
        c = cand(pub_date="last Tuesday")
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("reject", "invalid_candidate"))

    def test_nonnumeric_value_rejected(self):
        c = cand(value="about 1.3 million")
        r = decide(REGISTRY, rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("reject", "invalid_candidate"))


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


class TestStrictTypes(unittest.TestCase):
    """Malformed fields must be rejected, never applied to the record."""

    def assertInvalid(self, c, record=None):
        r = decide(REGISTRY, record if record is not None else rec(), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("reject", "invalid_candidate"), r)

    def test_non_finite_values_rejected(self):
        for v in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
            self.assertInvalid(cand(value=v))
            self.assertInvalid(cand(value=v, metric="untracked_metric"), record=None)

    def test_bool_and_string_values_rejected(self):
        for v in (True, False, "1300000", None, [1300000]):
            self.assertInvalid(cand(value=v))

    def test_citation_present_must_be_bool(self):
        self.assertInvalid(cand(citation={"present": "false", "locator": "row 2"}))
        self.assertInvalid(cand(citation={"present": 1, "locator": "row 2"}))
        self.assertInvalid(cand(citation={"present": True, "locator": 7}))

    def test_text_fields_must_be_strings(self):
        self.assertInvalid(cand(metric=["ma_enrollment"]))
        self.assertInvalid(cand(entity={"name": "Halcyon"}))
        self.assertInvalid(cand(period=""))
        self.assertInvalid(cand(unit=None))
        self.assertInvalid(cand(source_id=12))

    def test_huge_integer_and_odd_dates_and_padded_metric_rejected(self):
        self.assertInvalid(cand(value=10 ** 400))
        for d in ("20261001", "2026-W40-4", "2026-10-1"):
            self.assertInvalid(cand(pub_date=d))
        self.assertInvalid(cand(metric="ma_enrollment "))

    def test_change_flag_must_be_bool(self):
        self.assertInvalid(cand(unit_or_definition_change="false"))

    def test_corrupt_record_value_held_not_overwritten(self):
        c = cand()
        r = decide(REGISTRY, rec(value=float("nan")), c, [c])
        self.assertEqual((r["decision"], r["reason_code"]), ("hold", "invalid_record_value"))


class TestBatchPeers(unittest.TestCase):
    def test_invalid_peer_does_not_crash_or_block(self):
        good, bad = cand(), cand(value="abc", source_type="news")
        r = decide(REGISTRY, rec(), good, [good, bad])
        self.assertEqual(r["decision"], "apply")

    def test_uncited_peer_cannot_supersede_valid_candidate(self):
        good = cand(source_type="regulatory_filing", pub_date="2026-10-02")
        uncited = cand(value=1300500, citation={"present": False, "locator": ""})
        r = decide(REGISTRY, rec(), good, [good, uncited])
        self.assertEqual((r["decision"], r["reason_code"]),
                         ("apply", "authoritative_newer_consistent"))

    def test_uncited_conflicting_peer_does_not_hold(self):
        good = cand()
        uncited = cand(value=1400000, citation={"present": True, "locator": "an article"})
        r = decide(REGISTRY, rec(), good, [good, uncited])
        self.assertEqual(r["decision"], "apply")


class TestSourceBinding(unittest.TestCase):
    def test_value_found_with_thousands_separators(self):
        self.assertTrue(value_in_source(1312000, "enrollment reached 1,312,000 members"))
        self.assertTrue(value_in_source(85.0, "MLR of 85.0 percent"))
        self.assertTrue(value_in_source(85.0, "row: Halcyon,mlr_pct,85,percent"))
        self.assertTrue(value_in_source(1312000, "Halcyon,1312000,2026,members"))  # CSV cells
        self.assertFalse(value_in_source(1312000, "1,312,0000"))  # not valid grouping

    def test_value_absent_or_partial_match(self):
        self.assertFalse(value_in_source(1312000, "enrollment reached 1,313,000"))
        self.assertFalse(value_in_source(12, "published 2026-10-12"))  # not a date part
        self.assertFalse(value_in_source(3, "2026-Q3"))

    def test_value_not_in_source_held(self):
        c = cand(value=1300000)
        r = decide(REGISTRY, rec(), c, [c], source_text="members: 1,290,000")
        self.assertEqual((r["decision"], r["reason_code"]), ("hold", "value_not_in_source"))
        r = decide(REGISTRY, rec(), c, [c], source_text="members: 1,300,000")
        self.assertEqual(r["decision"], "apply")


class TestReconcileBatches(unittest.TestCase):
    RECORD = {"entities": {"Halcyon Health": {"metrics": {"ma_enrollment": {
        "value": 1250000, "unit": "members", "period": "2026-Q2", "as_of": "2026-06-30",
        "source": {"type": "regulatory_filing", "source_id": "SEED", "pub_date": "2026-08-15",
                   "citation": {"present": True, "locator": "seed"}}}}}}}

    def test_later_batch_sees_earlier_apply(self):
        newer = cand(batch="b1", value=1300000, pub_date="2026-10-01", source_id="NEW")
        older = cand(batch="b2", value=1280000, pub_date="2026-09-15", source_id="OLD")
        decisions, record = reconcile_batches(REGISTRY, self.RECORD, [newer, older])
        self.assertEqual([d["result"]["decision"] for d in decisions], ["apply", "hold"])
        self.assertEqual(decisions[1]["result"]["reason_code"], "stale_source")
        m = record["entities"]["Halcyon Health"]["metrics"]["ma_enrollment"]
        self.assertEqual((m["value"], m["source"]["source_id"]), (1300000, "NEW"))
        self.assertEqual(decisions[0]["prior"]["value"], 1250000)

    def test_input_record_not_mutated_and_non_object_rejected(self):
        before = json.dumps(self.RECORD, sort_keys=True)
        decisions, _ = reconcile_batches(REGISTRY, self.RECORD, [5, cand()])
        self.assertEqual(json.dumps(self.RECORD, sort_keys=True), before)
        self.assertEqual(decisions[0]["result"]["reason_code"], "invalid_candidate")

    def test_integer_batch_ids_stay_distinct_and_bad_ids_rejected(self):
        a = cand(batch=1, value=1312000)
        b = cand(batch=2, value=1400000, pub_date="2026-10-05")
        decisions, _ = reconcile_batches(REGISTRY, self.RECORD, [a, b, cand(batch=["x"])])
        by_batch = {d["batch"]: d["result"]["reason_code"] for d in decisions}
        self.assertEqual(by_batch["1"], "authoritative_newer_consistent")
        self.assertNotEqual(by_batch["2"], "conflicting_sources")
        self.assertEqual(by_batch["invalid"], "invalid_candidate")

    def test_tied_duplicates_apply_exactly_once(self):
        a = cand(value=1300000, source_id="A")
        b = cand(value=1300100, source_id="B")
        decisions, record = reconcile_batches(REGISTRY, self.RECORD, [a, b])
        self.assertEqual([d["result"]["decision"] for d in decisions], ["apply", "reject"])
        self.assertEqual(record["entities"]["Halcyon Health"]["metrics"]["ma_enrollment"]
                         ["source"]["source_id"], "A")

    def test_unverified_peer_cannot_supersede_verified_value(self):
        real = cand(value=1312000, source_type="news", source_id="REAL")
        fake = cand(value=1313000, source_type="regulatory_filing", source_id="FAKE")
        texts = {"REAL": "members: 1,312,000", "FAKE": "no numbers here"}
        decisions, _ = reconcile_batches(REGISTRY, {"entities": {}}, [real, fake],
                                         source_texts=texts)
        got = [(d["result"]["decision"], d["result"]["reason_code"]) for d in decisions]
        self.assertEqual(got, [("apply", "new_coverage"), ("hold", "value_not_in_source")])

    def test_source_missing_from_supplied_documents_is_held(self):
        c = cand(source_id="NOT-IN-MANIFEST")
        decisions, _ = reconcile_batches(REGISTRY, self.RECORD, [c], source_texts={})
        self.assertEqual(decisions[0]["result"]["reason_code"], "source_not_found")

    def test_dumps_safe_never_writes_nan(self):
        out = dumps_safe({"v": float("nan"), "w": [float("inf")]})
        self.assertNotIn("NaN", out)
        json.loads(out)  # strict JSON readers accept it


if __name__ == "__main__":
    unittest.main()
