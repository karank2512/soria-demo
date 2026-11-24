#!/usr/bin/env python3
"""Tests for workspace.py: the persistent review workflow (init, ingest,
queue, resolve, render), its refusal paths, and its durability guarantees
(lock, journal roll-forward, no double ingest). Synthetic data only."""

import contextlib
import io
import json
import os
import tempfile
import unittest

import workspace

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_RECORD = os.path.join(HERE, "record.json")
REGISTRY = os.path.join(HERE, "registry.json")
DEMO_CANDS = os.path.join(HERE, "demo-candidates.json")
MANIFEST = os.path.join(HERE, "demo_docs", "manifest.json")


def run(*argv):
    """Run the CLI; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = workspace.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def cand(**kw):
    base = {"entity": "Halcyon Health", "metric": "ma_enrollment", "value": 1300000,
            "unit": "members", "period": "2026-Q3", "pub_date": "2026-10-01",
            "source_type": "government_data", "source_id": "SRC-T",
            "citation": {"present": True, "locator": "row 2, column value"},
            "unit_or_definition_change": False, "batch": "b1"}
    base.update(kw)
    return base


class WSBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = os.path.join(self.tmp, "ws")
        rc, _, err = run("init", self.ws, "--record", SEED_RECORD, "--registry", REGISTRY)
        self.assertEqual(rc, 0, err)

    # No tearDown: test workspaces are kept for inspection, never deleted.
    # Run the suite with TMPDIR pointing at a scratch folder to keep them
    # out of the system temp directory.

    def write_cands(self, cands, name="c.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cands, f)
        return path

    def ingest_demo(self):
        rc, out, err = run("ingest", self.ws, "--candidates", DEMO_CANDS,
                           "--manifest", MANIFEST)
        self.assertEqual(rc, 0, err)
        return out

    def state(self):
        """Snapshot of every persistent file, to prove refusals change nothing."""
        snap = {}
        for name in ("record.json", "audit.jsonl", "workspace.json"):
            with open(os.path.join(self.ws, name), encoding="utf-8") as f:
                snap[name] = f.read()
        return snap

    def record_value(self, entity, metric):
        with open(os.path.join(self.ws, "record.json"), encoding="utf-8") as f:
            return json.load(f)["entities"][entity]["metrics"][metric]

    def audit(self):
        return workspace.Workspace(self.ws).audit()


class TestInit(WSBase):
    def test_refuses_existing_workspace_and_non_empty_dir(self):
        rc, _, err = run("init", self.ws)
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        open(os.path.join(other, "keep.txt"), "w").close()
        rc, _, err = run("init", other)
        self.assertEqual(rc, 2)
        self.assertIn("not empty", err)
        self.assertEqual(os.listdir(other), ["keep.txt"])

    def test_refuses_seed_record_with_non_finite_value(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            f.write('{"entities": {"X": {"metrics": {"m": {"value": NaN, "unit": "u",'
                    ' "as_of": "2026-01-01", "source": {"type": "news"}}}}}}')
        rc, _, err = run("init", os.path.join(self.tmp, "ws2"), "--record", bad)
        self.assertEqual(rc, 2)
        self.assertIn("finite number", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ws2")))

    def test_bad_init_inputs_give_clean_errors(self):
        a_file = os.path.join(self.tmp, "afile")
        open(a_file, "w").close()
        rc, _, err = run("init", a_file)
        self.assertEqual(rc, 2)
        self.assertIn("not a directory", err)
        rc, _, err = run("init", os.path.join(a_file, "sub"))
        self.assertEqual(rc, 2)
        bad_src = os.path.join(self.tmp, "bad_src.json")
        with open(bad_src, "w") as f:
            json.dump({"entities": {"X": {"metrics": {"m": {
                "value": 1, "unit": "u", "as_of": "2026-01-01",
                "source": {"type": ["news"]}}}}}}, f)
        rc, _, err = run("init", os.path.join(self.tmp, "w5"), "--record", bad_src)
        self.assertEqual(rc, 2)
        self.assertIn("source.type", err)
        for aliases in (None, "abc", [1]):
            reg = os.path.join(self.tmp, "reg.json")
            with open(reg, "w") as f:
                json.dump({"A": {"aliases": aliases}}, f)
            rc, _, err = run("init", os.path.join(self.tmp, "w3"), "--registry", reg)
            self.assertEqual(rc, 2, aliases)
            self.assertIn("aliases must be a list of strings", err)
        rc, _, err = run("init", os.path.join(self.tmp, "w4"), "--record", self.tmp)
        self.assertEqual(rc, 2)
        self.assertIn("cannot read", err)
        rc, _, err = run("ingest", self.ws, "--candidates", self.write_cands([cand()]),
                         "--manifest", self.tmp)
        self.assertEqual(rc, 2)

    def test_reconcile_cli_refuses_malformed_record_and_registry(self):
        import reconcile
        bad_rec = os.path.join(self.tmp, "bad_rec.json")
        with open(bad_rec, "w") as f:
            json.dump({"entities": {"Halcyon Health": {"metrics": {
                "ma_enrollment": {"value": 1, "unit": "members"}}}}}, f)  # no as_of
        bad_reg = os.path.join(self.tmp, "bad_reg.json")
        with open(bad_reg, "w") as f:
            json.dump(["not", "an", "object"], f)
        cands = self.write_cands([cand()])
        for rec, reg in ((bad_rec, REGISTRY), (SEED_RECORD, bad_reg)):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = reconcile.main(["--record", rec, "--registry", reg, "--candidates", cands,
                                     "--audit", os.path.join(self.tmp, "a.jsonl"),
                                     "--record-out", os.path.join(self.tmp, "r.json"),
                                     "--report", os.path.join(self.tmp, "rep.md")])
            self.assertEqual(rc, 2)
            self.assertIn("not usable", err.getvalue())

    def test_commands_on_missing_workspace_fail_cleanly(self):
        rc, _, err = run("queue", os.path.join(self.tmp, "nope"))
        self.assertEqual(rc, 2)
        self.assertIn("not a workspace", err)


class TestIngestAndQueue(WSBase):
    def test_demo_ingest_ids_and_queue(self):
        out = self.ingest_demo()
        self.assertIn("2 applied, 2 held, 2 rejected", out)
        audit = self.audit()
        self.assertEqual([e["id"] for e in audit], ["D-%d" % i for i in range(1, 7)])
        self.assertTrue(all(e["source_checked"] for e in audit))
        rc, out, _ = run("queue", self.ws, "--json")
        holds = json.loads(out)
        self.assertEqual([h["id"] for h in holds], ["D-2", "D-3"])
        self.assertEqual({h["reason_code"] for h in holds}, {"conflicting_sources"})
        self.assertEqual(self.record_value("Halcyon Health", "ma_enrollment")["value"], 1312000)

    def test_reingesting_same_batch_is_refused_without_change(self):
        self.ingest_demo()
        before = self.state()
        rc, _, err = run("ingest", self.ws, "--candidates", DEMO_CANDS, "--manifest", MANIFEST)
        self.assertEqual(rc, 2)
        self.assertIn("already ingested", err)
        self.assertEqual(self.state(), before)

    def test_state_carries_across_ingests(self):
        run("ingest", self.ws, "--candidates", self.write_cands([cand(batch="b1")]))
        older = cand(batch="b2", value=1290000, pub_date="2026-09-20", source_id="OLD")
        rc, out, _ = run("ingest", self.ws, "--candidates", self.write_cands([older], "c2.json"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.record_value("Halcyon Health", "ma_enrollment")["value"], 1300000)
        last = self.audit()[-1]
        self.assertEqual((last["id"], last["reason_code"]), ("D-2", "stale_source"))

    def test_bad_candidates_file_changes_nothing(self):
        before = self.state()
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            f.write("{not json")
        rc, _, err = run("ingest", self.ws, "--candidates", bad)
        self.assertEqual(rc, 2)
        self.assertIn("not valid JSON", err)
        rc, _, err = run("ingest", self.ws, "--candidates", self.write_cands({"a": 1}))
        self.assertEqual(rc, 2)
        self.assertEqual(self.state(), before)

    def test_malformed_candidates_are_logged_not_applied(self):
        cands = [cand(value=float("nan")), cand(value="1300000"), 7,
                 cand(citation={"present": "false", "locator": "row 2"})]
        rc, out, err = run("ingest", self.ws, "--candidates", self.write_cands(cands))
        self.assertEqual(rc, 0, err)
        self.assertIn("0 applied, 0 held, 4 rejected", out)
        self.assertEqual(self.record_value("Halcyon Health", "ma_enrollment")["value"], 1250000)
        with open(os.path.join(self.ws, "audit.jsonl")) as f:
            text = f.read()
        self.assertNotIn("NaN", text)  # audit stays strict JSON
        self.assertTrue(all(e["reason_code"] == "invalid_candidate" for e in self.audit()))

    def test_same_file_and_unbatched_candidates_refused(self):
        path = self.write_cands([cand(batch="b1")])
        run("ingest", self.ws, "--candidates", path)
        before = self.state()
        rc, _, err = run("ingest", self.ws, "--candidates", path)
        self.assertEqual(rc, 2)
        self.assertIn("exact candidates file was already ingested", err)
        c = cand()
        del c["batch"]
        rc, _, err = run("ingest", self.ws, "--candidates", self.write_cands([c], "nb.json"))
        self.assertEqual(rc, 2)
        self.assertIn("no batch id", err)
        self.assertEqual(self.state(), before)
        rc, _, err = run("ingest", self.ws, "--candidates", self.write_cands([c], "nb.json"),
                         "--batch", "b2")
        self.assertEqual(rc, 0, err)

    def test_hostile_files_give_clean_errors(self):
        deep = os.path.join(self.tmp, "deep.json")
        with open(deep, "w") as f:
            f.write("[" * 100000 + "]" * 100000)
        rc, _, err = run("ingest", self.ws, "--candidates", deep)
        self.assertEqual(rc, 2)
        docs = os.path.join(self.tmp, "d")
        os.makedirs(docs)
        with open(os.path.join(docs, "x.txt"), "wb") as f:
            f.write(b"\xff\xfe\x00bad")
        with open(os.path.join(docs, "m.json"), "w") as f:
            json.dump([{"source_id": "X", "file": "x.txt"}], f)
        rc, _, err = run("ingest", self.ws, "--candidates", self.write_cands([cand()]),
                         "--manifest", os.path.join(docs, "m.json"))
        self.assertEqual(rc, 2)
        self.assertIn("not UTF-8", err)
        rc, _, err = run("render", self.ws, "--out", os.path.join(self.tmp, "no", "r.html"))
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", err)

    def test_rejected_junk_still_renders(self):
        junk = [cand(batch="j", metric=["a"]), cand(batch="j", value=float("nan")),
                cand(batch="j", value=10 ** 400), cand(batch="j", citation=[1]),
                cand(batch="j", source_type=["x"], unit={"u": 1}), 5]
        rc, _, err = run("ingest", self.ws, "--candidates", self.write_cands(junk))
        self.assertEqual(rc, 0, err)
        out_path = os.path.join(self.tmp, "r.html")
        rc, _, err = run("render", self.ws, "--out", out_path)
        self.assertEqual(rc, 0, err)
        with open(out_path, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("Rejected outright (6)", page)

    def test_non_object_candidate_does_not_consume_a_batch_id(self):
        run("ingest", self.ws, "--candidates", self.write_cands([5, cand(batch="b1")]))
        rc, _, err = run("ingest", self.ws, "--candidates",
                         self.write_cands([7, cand(batch="b2")], "c2.json"))
        self.assertEqual(rc, 0, err)

    def test_malformed_source_id_with_manifest_rejected_cleanly(self):
        c = cand(source_id=["x"], batch="zz")
        rc, out, err = run("ingest", self.ws, "--candidates", self.write_cands([c]),
                           "--manifest", MANIFEST)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.audit()[0]["reason_code"], "invalid_candidate")

    def test_render_refuses_to_overwrite_workspace_data(self):
        before = self.state()
        for name in ("audit.jsonl", "record.json", "workspace.json", "registry.json"):
            rc, _, err = run("render", self.ws, "--out", os.path.join(self.ws, name))
            self.assertEqual(rc, 2, name)
            self.assertIn("refusing", err)
        rc, _, err = run("render", self.ws, "--out", self.ws)
        self.assertEqual(rc, 2)
        hard = os.path.join(self.tmp, "hard.html")
        os.link(os.path.join(self.ws, "record.json"), hard)
        rc, _, err = run("render", self.ws, "--out", hard)
        self.assertEqual(rc, 2)
        self.assertEqual(self.state(), before)

    def test_value_missing_from_source_document_is_held(self):
        docs = os.path.join(self.tmp, "docs")
        os.makedirs(docs)
        with open(os.path.join(docs, "s1.txt"), "w") as f:
            f.write("Published 2026-10-01\nHalcyon Health,ma_enrollment,1290000,members\n")
        with open(os.path.join(docs, "manifest.json"), "w") as f:
            json.dump([{"source_id": "S1", "file": "s1.txt", "batch": "m1"}], f)
        c = cand(source_id="S1", value=1300000)
        del c["batch"]  # filled from the manifest
        rc, _, err = run("ingest", self.ws, "--candidates", self.write_cands([c]),
                         "--manifest", os.path.join(docs, "manifest.json"))
        self.assertEqual(rc, 0, err)
        e = self.audit()[0]
        self.assertEqual((e["batch"], e["decision"], e["reason_code"]),
                         ("m1", "hold", "value_not_in_source"))


class TestResolve(WSBase):
    def setUp(self):
        super().setUp()
        self.ingest_demo()

    def test_accept_applies_value_with_revision_and_reviewer(self):
        rc, out, err = run("resolve", self.ws, "D-2", "--accept", "--reviewer", "analyst-a",
                           "--note", "government dataset preferred")
        self.assertEqual(rc, 0, err)
        m = self.record_value("Halcyon Health", "mlr_pct")
        self.assertEqual((m["value"], m["source"]["source_id"]), (85.0, "SRC-GOV-DEMO2"))
        self.assertEqual(m["source"]["citation"]["locator"], "row 2 (Halcyon Health), column value")
        r = self.audit()[-1]
        self.assertEqual((r["id"], r["type"], r["action"], r["resolves"], r["reviewer"]),
                         ("R-1", "resolution", "accept", "D-2", "analyst-a"))
        self.assertEqual(r["revision"]["supersedes_value"], 84.2)
        rc, out, _ = run("queue", self.ws, "--json")
        self.assertEqual([h["id"] for h in json.loads(out)], ["D-3"])

    def test_dismiss_leaves_record_unchanged(self):
        before = self.record_value("Halcyon Health", "mlr_pct")
        rc, _, _ = run("resolve", self.ws, "D-3", "--dismiss", "--reviewer", "analyst-a")
        self.assertEqual(rc, 0)
        self.assertEqual(self.record_value("Halcyon Health", "mlr_pct"), before)

    def test_refusals_change_nothing(self):
        run("resolve", self.ws, "D-3", "--dismiss", "--reviewer", "a")
        before = self.state()
        for argv, msg in [
            (("D-3", "--accept", "--reviewer", "b"), "already resolved"),
            (("D-1", "--accept", "--reviewer", "b"), "not a hold"),
            (("D-99", "--dismiss", "--reviewer", "b"), "no decision"),
            (("D-2", "--accept", "--reviewer", "  "), "--reviewer is required"),
        ]:
            rc, _, err = run("resolve", self.ws, *argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(msg, err)
        self.assertEqual(self.state(), before)

    def test_accept_refused_when_record_changed_since_hold(self):
        # Accepting one side of a conflict changes the record; accepting the
        # other side must then be a deliberate --force, not a silent overwrite.
        run("resolve", self.ws, "D-2", "--accept", "--reviewer", "a")
        before = self.state()
        rc, _, err = run("resolve", self.ws, "D-3", "--accept", "--reviewer", "a")
        self.assertEqual(rc, 2)
        self.assertIn("changed since D-3 was held", err)
        self.assertEqual(self.state(), before)
        rc, _, err = run("resolve", self.ws, "D-3", "--accept", "--reviewer", "a", "--force")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.audit()[-1]["forced_over_change"])
        self.assertEqual(self.record_value("Halcyon Health", "mlr_pct")["value"], 88.6)
        out_path = os.path.join(self.tmp, "forced.html")
        run("render", self.ws, "--out", out_path)
        with open(out_path, encoding="utf-8") as f:
            self.assertIn("forced: record had changed since the hold", f.read())

    def test_render_shows_only_open_holds_and_logs_resolution(self):
        run("resolve", self.ws, "D-2", "--accept", "--reviewer", "analyst-a",
            "--note", "<checked>")
        out_path = os.path.join(self.tmp, "review.html")
        rc, _, err = run("render", self.ws, "--out", out_path)
        self.assertEqual(rc, 0, err)
        with open(out_path, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("Held for review (1)", page)
        self.assertIn("held &middot; D-3", page)
        self.assertNotIn("held &middot; D-2", page)
        self.assertIn("R-1 accepted by analyst-a", page)
        self.assertIn("note: &lt;checked&gt;", page)   # escaped
        self.assertIn("2 rejected &middot; 1 resolved", page)
        self.assertNotIn("—", page)


class TestDurability(WSBase):
    def test_lock_blocks_concurrent_writer_and_is_released(self):
        with workspace.Workspace(self.ws):
            rc, _, err = run("ingest", self.ws, "--candidates", self.write_cands([cand()]))
            self.assertEqual(rc, 2)
            self.assertIn("locked", err)
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".lock")))
        rc, _, _ = run("ingest", self.ws, "--candidates", self.write_cands([cand()]))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".lock")))

    def test_interrupted_change_rolls_forward_exactly_once(self):
        ws = workspace.Workspace(self.ws)
        # Simulate a crash after the journal was written and one of two audit
        # lines was appended, before the record and metadata were replaced.
        meta = ws.meta()
        record = ws.record()
        record["entities"]["Halcyon Health"]["metrics"]["ma_enrollment"]["value"] = 1300000
        meta["next_decision"] = 3
        lines = [{"id": "D-1", "decision": "apply"}, {"id": "D-2", "decision": "hold"}]
        with open(os.path.join(self.ws, "txn.json"), "w") as f:
            json.dump({"audit": lines, "record": record, "meta": meta}, f)
        with open(os.path.join(self.ws, "audit.jsonl"), "a") as f:
            f.write(json.dumps(lines[0]) + "\n")
        rc, out, _ = run("status", self.ws)
        self.assertEqual(rc, 0)
        self.assertEqual([e["id"] for e in self.audit()], ["D-1", "D-2"])
        self.assertEqual(self.record_value("Halcyon Health", "ma_enrollment")["value"], 1300000)
        self.assertEqual(ws.meta()["next_decision"], 3)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "txn.json")))

    def test_torn_audit_tail_is_repaired_during_roll_forward(self):
        ws = workspace.Workspace(self.ws)
        lines = [{"id": "D-1", "decision": "apply"}, {"id": "D-2", "decision": "hold"}]
        meta = dict(ws.meta(), next_decision=3)
        with open(os.path.join(self.ws, "txn.json"), "w") as f:
            json.dump({"audit": lines, "record": None, "meta": meta}, f)
        for torn in (json.dumps(lines[0])[:10], json.dumps(lines[0])):  # half line; no newline
            with open(os.path.join(self.ws, "audit.jsonl"), "w") as f:
                f.write(torn)
            if not os.path.exists(os.path.join(self.ws, "txn.json")):
                with open(os.path.join(self.ws, "txn.json"), "w") as f:
                    json.dump({"audit": lines, "record": None, "meta": meta}, f)
            rc, _, err = run("status", self.ws)
            self.assertEqual(rc, 0, err)
            self.assertEqual([e["id"] for e in self.audit()], ["D-1", "D-2"])

    def test_corrupt_audit_line_reported_not_ignored(self):
        with open(os.path.join(self.ws, "audit.jsonl"), "a") as f:
            f.write("{truncated\n")
        rc, _, err = run("queue", self.ws)
        self.assertEqual(rc, 2)
        self.assertIn("line 1 is not valid JSON", err)
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".lock")))


if __name__ == "__main__":
    unittest.main()
