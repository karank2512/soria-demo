#!/usr/bin/env python3
"""Persistent local review workspace for the reconciliation engine (stdlib only).

A workspace is a directory holding the record, the entity registry and an
append-only audit log. It turns the one-shot pipeline into a workflow that
carries state across runs:

  init     create a workspace from a seed record + registry
  ingest   reconcile a candidates file into the workspace (batches in order)
  queue    list holds still waiting for an analyst
  resolve  accept or dismiss one hold; accepting writes the value to the record
  render   write the HTML review page for the workspace's current state
  status   counts and record summary

The audit log is the source of truth for decisions: every engine decision and
every analyst resolution gets a stable id (D-<n> / R-<n>). The open review
queue is derived from it (holds with no resolution), so there is no second
queue file to drift out of sync.

Durability, within the limits of a local single-machine tool:
- one writer at a time (lock file created with O_EXCL);
- each change is first written as a journal (txn.json) holding the new audit
  lines, record and metadata, then rolled forward; an interrupted change is
  completed on the next command, so record and audit cannot disagree;
- files are replaced atomically (write temp file, fsync, os.replace);
- a batch id that was already ingested is refused, so re-running the same
  input does not double-apply.

Not provided: multi-user access control, a server, or tamper-proofing. All data
used with this demo is synthetic.

Usage:
  python3 workspace.py init WS --record record.json --registry registry.json
  python3 workspace.py ingest WS --candidates c.json [--manifest demo_docs/manifest.json]
  python3 workspace.py queue WS
  python3 workspace.py resolve WS D-3 --accept --reviewer NAME --note "checked filing"
  python3 workspace.py render WS [--out WS/review.html]
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import reconcile  # noqa: E402

SCHEMA = 1
META = "workspace.json"
RECORD = "record.json"
REGISTRY = "registry.json"
AUDIT = "audit.jsonl"
TXN = "txn.json"
LOCK = ".lock"


class WorkspaceError(Exception):
    """A user-facing error: printed without a traceback, exit code 2."""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _fsync_dir(path):
    """Persist a rename/unlink in the directory itself (POSIX; best effort)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def _load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise WorkspaceError("%s not found: %s" % (what, path))
    except OSError as e:  # a directory, permissions, ...
        raise WorkspaceError("cannot read %s (%s): %s" % (what, e.strerror, path))
    except (ValueError, RecursionError) as e:
        raise WorkspaceError("%s is not valid JSON (%s): %s" % (what, e, path))


# Shared with reconcile.py's CLI so both entry points refuse the same files.
validate_record = reconcile.record_problems
validate_registry = reconcile.registry_problems


class Workspace:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._locked = False

    def p(self, name):
        return os.path.join(self.path, name)

    # ---- locking and transactions ------------------------------------
    def __enter__(self):
        if not os.path.exists(self.p(META)):
            raise WorkspaceError("not a workspace (no %s): %s" % (META, self.path))
        try:
            fd = os.open(self.p(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise WorkspaceError(
                "workspace is locked by another command (%s). If no other command "
                "is running, the lock is stale and can be removed." % self.p(LOCK))
        with os.fdopen(fd, "w") as f:
            f.write("pid %d at %s\n" % (os.getpid(), _now()))
        self._locked = True
        try:
            self.recover()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if self._locked:
            os.remove(self.p(LOCK))
            self._locked = False
        return False

    def recover(self):
        """Roll an interrupted change forward. Idempotent. Returns True if any."""
        if not os.path.exists(self.p(TXN)):
            return False
        txn = _load_json(self.p(TXN), "journal")
        self._roll_forward(txn)
        return True

    def _repair_audit_tail(self):
        """Drop a torn final audit line left by an interrupted append.

        Only called while a journal exists: the journal holds every line that
        append was writing, so the dropped bytes are re-appended in full.
        """
        path = self.p(AUDIT)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return
        if not data or data.endswith(b"\n"):
            return
        keep = data[:data.rfind(b"\n") + 1]
        with open(path, "r+b") as f:
            f.truncate(len(keep))
            f.flush()
            os.fsync(f.fileno())

    def _roll_forward(self, txn):
        self._repair_audit_tail()
        present = {e.get("id") for e in self.audit()}
        missing = [e for e in txn["audit"] if e["id"] not in present]
        if missing:
            with open(self.p(AUDIT), "a", encoding="utf-8") as f:
                for e in missing:
                    f.write(reconcile.dumps_safe(e) + "\n")
                f.flush()
                os.fsync(f.fileno())
        if txn.get("record") is not None:
            _atomic_write(self.p(RECORD), reconcile.dumps_safe(txn["record"], indent=2) + "\n")
        _atomic_write(self.p(META), json.dumps(txn["meta"], indent=2) + "\n")
        os.remove(self.p(TXN))
        _fsync_dir(self.path)

    def commit(self, audit_entries, record, meta):
        """Journal the change, then apply it. record=None leaves it unchanged."""
        if not self._locked:
            raise RuntimeError("commit outside lock")
        txn = {"audit": audit_entries, "record": record, "meta": meta}
        _atomic_write(self.p(TXN), reconcile.dumps_safe(txn) + "\n")
        self._roll_forward(txn)

    # ---- state ---------------------------------------------------------
    def meta(self):
        meta = _load_json(self.p(META), "workspace metadata")
        if meta.get("schema") != SCHEMA:
            raise WorkspaceError("unsupported workspace schema %r (expected %d)"
                                 % (meta.get("schema"), SCHEMA))
        return meta

    def record(self):
        return _load_json(self.p(RECORD), "record")

    def registry(self):
        return _load_json(self.p(REGISTRY), "registry")

    def audit(self):
        entries = []
        try:
            f = open(self.p(AUDIT), "r", encoding="utf-8")
        except FileNotFoundError:
            return entries
        with f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    raise WorkspaceError("audit log line %d is not valid JSON: %s"
                                         % (n, self.p(AUDIT)))
        return entries

    def open_holds(self, audit=None):
        audit = self.audit() if audit is None else audit
        resolved = {e.get("resolves") for e in audit if e.get("type") == "resolution"}
        return [e for e in audit
                if e.get("decision") == "hold" and e.get("id") not in resolved]


# ---- commands ------------------------------------------------------------

def cmd_init(args):
    path = os.path.abspath(args.workspace)
    if os.path.exists(os.path.join(path, META)):
        raise WorkspaceError("workspace already exists: %s" % path)
    if os.path.exists(path) and not os.path.isdir(path):
        raise WorkspaceError("path exists and is not a directory: %s" % path)
    if os.path.isdir(path) and os.listdir(path):
        raise WorkspaceError("directory is not empty: %s" % path)
    record = _load_json(args.record, "seed record")
    registry = _load_json(args.registry, "registry")
    problems = validate_record(record) + validate_registry(registry)
    if problems:
        raise WorkspaceError("seed files are not usable:\n  " + "\n  ".join(problems))
    os.makedirs(path, exist_ok=True)
    _atomic_write(os.path.join(path, REGISTRY), json.dumps(registry, indent=2) + "\n")
    _atomic_write(os.path.join(path, RECORD), reconcile.dumps_safe(record, indent=2) + "\n")
    open(os.path.join(path, AUDIT), "a").close()
    meta = {"schema": SCHEMA, "created": _now(), "next_decision": 1,
            "next_resolution": 1, "ingests": []}
    # metadata last: its presence marks the workspace as complete.
    _atomic_write(os.path.join(path, META), json.dumps(meta, indent=2) + "\n")
    print("initialized workspace %s" % path)
    return 0


def _load_manifest(path):
    """Read a demo-style manifest: [{source_id, file, batch}], files relative to it."""
    manifest = _load_json(path, "manifest")
    if not isinstance(manifest, list):
        raise WorkspaceError("manifest must be a JSON list")
    base = os.path.dirname(os.path.abspath(path))
    texts, batches = {}, {}
    for i, entry in enumerate(manifest):
        if not isinstance(entry, dict) or not isinstance(entry.get("source_id"), str):
            raise WorkspaceError("manifest entry %d needs a string source_id" % i)
        sid = entry["source_id"]
        if isinstance(entry.get("batch"), str):
            batches[sid] = entry["batch"]
        if isinstance(entry.get("file"), str):
            fpath = os.path.join(base, entry["file"])
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    texts[sid] = f.read()
            except OSError as e:
                raise WorkspaceError("manifest source %s: cannot read %s (%s)"
                                     % (sid, fpath, e.strerror))
            except UnicodeDecodeError:
                raise WorkspaceError("manifest source %s: %s is not UTF-8 text"
                                     % (sid, fpath))
    return texts, batches


def cmd_ingest(args):
    try:
        with open(args.candidates, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise WorkspaceError("cannot read candidates file: %s" % e)
    try:
        candidates = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        raise WorkspaceError("candidates file is not valid JSON: %s" % e)
    if not isinstance(candidates, list):
        raise WorkspaceError("candidates file must be a JSON list")
    digest = hashlib.sha256(raw).hexdigest()
    texts = None
    if args.manifest:
        texts, batches = _load_manifest(args.manifest)
        for c in candidates:
            if (isinstance(c, dict) and "batch" not in c
                    and isinstance(c.get("source_id"), str) and c["source_id"] in batches):
                c["batch"] = batches[c["source_id"]]
    if args.batch:
        for c in candidates:
            if isinstance(c, dict) and "batch" not in c:
                c["batch"] = args.batch
    unbatched = sum(1 for c in candidates if isinstance(c, dict) and "batch" not in c)
    if unbatched:
        raise WorkspaceError(
            "%d candidate(s) have no batch id. Add a 'batch' field, use a manifest "
            "that assigns one, or pass --batch NAME. Nothing was changed." % unbatched)

    with Workspace(args.workspace) as ws:
        meta = ws.meta()
        same_file = [ing for ing in meta["ingests"] if ing.get("sha256") == digest]
        if same_file:
            raise WorkspaceError("this exact candidates file was already ingested at %s. "
                                 "Nothing was changed." % same_file[0]["ts"])
        seen = {b for ing in meta["ingests"] for b in ing["batches"]}
        incoming = []
        for c in candidates:
            b = reconcile.batch_id_of(c)
            if b is not None and b not in incoming:
                incoming.append(b)
        repeated = [b for b in incoming if b in seen]
        if repeated:
            raise WorkspaceError(
                "batch already ingested: %s. Nothing was changed. Give new updates "
                "a new batch id." % ", ".join(repeated))

        decisions, record = reconcile.reconcile_batches(
            ws.registry(), ws.record(), candidates, source_texts=texts)
        ts = _now()
        n = meta["next_decision"]
        entries = []
        for d in decisions:
            line = reconcile.audit_line(d, ts)
            line = dict({"id": "D-%d" % n}, **line)
            if line["decision"] == "hold":
                line["record_at_hold"] = d.get("record_before")
            n += 1
            if texts is not None:
                sid = d["candidate"].get("source_id") if isinstance(d["candidate"], dict) else None
                line["source_checked"] = isinstance(sid, str) and sid in texts
            entries.append(line)
        meta["next_decision"] = n
        meta["ingests"].append({
            "ts": ts, "file": os.path.basename(args.candidates),
            "sha256": digest,
            "batches": incoming, "decisions": len(entries)})
        ws.commit(entries, record, meta)
        open_n = len(ws.open_holds())

    counts = {"apply": 0, "hold": 0, "reject": 0}
    for e in entries:
        counts[e["decision"]] += 1
    print("ingested %d candidates in %d batch(es): %d applied, %d held, %d rejected. "
          "%d hold(s) open in the queue."
          % (len(entries), len(incoming), counts["apply"], counts["hold"],
             counts["reject"], open_n))
    return 0


def _short(state):
    if not state:
        return "no value"
    return "%s as of %s from %s" % (state["value"], state["as_of"], state["source_id"])


def _fmt_hold(e):
    cit = (e.get("citation") or {}).get("locator", "") if isinstance(e.get("citation"), dict) else ""
    return "%-6s %-20s %-22s %14s %-8s %-28s %s | %s, pub %s | %s" % (
        e.get("id"), str(e.get("entity"))[:20], str(e.get("metric"))[:22],
        e.get("claimed_value"), str(e.get("unit"))[:8], e.get("reason_code"),
        e.get("period"), e.get("source_id"), e.get("pub_date"), cit)


def cmd_queue(args):
    with Workspace(args.workspace) as ws:
        holds = ws.open_holds()
    if args.json:
        print(json.dumps(holds, indent=2))
        return 0
    if not holds:
        print("no open holds")
        return 0
    print("%d open hold(s):" % len(holds))
    for e in holds:
        print("  " + _fmt_hold(e))
    print("resolve with: workspace.py resolve %s <id> --accept|--dismiss --reviewer NAME"
          % args.workspace)
    return 0


def _record_state(record, entity, metric):
    """What a hold was compared against; used to detect later record changes."""
    m = record.get("entities", {}).get(entity, {}).get("metrics", {}).get(metric)
    if not isinstance(m, dict):
        return None
    return {"value": m.get("value"), "as_of": m.get("as_of"),
            "source_id": (m.get("source") or {}).get("source_id")}


def cmd_resolve(args):
    reviewer = (args.reviewer or "").strip()
    if not reviewer:
        raise WorkspaceError("--reviewer is required (who is making this call)")
    with Workspace(args.workspace) as ws:
        meta = ws.meta()
        audit = ws.audit()
        target = next((e for e in audit if e.get("id") == args.hold_id), None)
        if target is None:
            raise WorkspaceError("no decision with id %s" % args.hold_id)
        if target.get("decision") != "hold":
            raise WorkspaceError("%s is not a hold (it was %s); only holds can be resolved"
                                 % (args.hold_id, target.get("decision")))
        done = next((e for e in audit if e.get("type") == "resolution"
                     and e.get("resolves") == args.hold_id), None)
        if done is not None:
            raise WorkspaceError("%s was already resolved by %s (%s, %s)"
                                 % (args.hold_id, done["id"], done["action"], done["reviewer"]))

        action = "accept" if args.accept else "dismiss"
        entry = {"id": "R-%d" % meta["next_resolution"], "type": "resolution",
                 "ts": _now(), "resolves": args.hold_id, "action": action,
                 "reviewer": reviewer, "note": args.note or "",
                 "entity": target.get("entity"), "metric": target.get("metric"),
                 "claimed_value": target.get("claimed_value"),
                 "unit": target.get("unit"), "period": target.get("period"),
                 "source_id": target.get("source_id"),
                 "source_type": target.get("source_type"),
                 "pub_date": target.get("pub_date"),
                 "citation": target.get("citation"),
                 "hold_reason": target.get("reason_code")}
        record = None
        if action == "accept":
            # The hold passed validation, entity resolution and the citation
            # gate before it was held, so its fields are well-formed.
            record = ws.record()
            now = _record_state(record, target.get("entity"), target.get("metric"))
            then = target.get("record_at_hold", now)
            if now != then and not args.force:
                raise WorkspaceError(
                    "the record for %s / %s changed since %s was held (then %s, now %s). "
                    "Accepting would overwrite the newer value. Review it, then re-run "
                    "with --force to accept anyway, or --dismiss."
                    % (target.get("entity"), target.get("metric"), args.hold_id,
                       _short(then), _short(now)))
            cand = {k: target.get(k) for k in ("metric", "unit", "period", "pub_date",
                                                "source_type", "source_id", "citation")}
            cand["value"] = target.get("claimed_value")
            if reconcile.candidate_error(dict(cand, entity=target.get("entity"))):
                raise WorkspaceError("%s cannot be applied: its stored fields are "
                                     "incomplete" % args.hold_id)
            prior = reconcile.apply_value(record, target["entity"], cand)
            if now != then:
                entry["forced_over_change"] = True
            if prior:
                entry["revision"] = {
                    "supersedes_value": prior.get("value"),
                    "prior_source_type": (prior.get("source") or {}).get("type"),
                    "prior_as_of": prior.get("as_of")}
        meta["next_resolution"] += 1
        ws.commit([entry], record, meta)
        left = len(ws.open_holds())

    what = ("accepted: record updated" if action == "accept"
            else "dismissed: record unchanged")
    print("%s %s %s (%s). %d hold(s) still open." % (entry["id"], args.hold_id, what,
                                                     reviewer, left))
    return 0


def cmd_render(args):
    import review_page
    with Workspace(args.workspace) as ws:
        out = os.path.abspath(args.out or ws.p("review.html"))
        protected = {os.path.realpath(ws.p(n)) for n in
                     (META, RECORD, REGISTRY, AUDIT, TXN, LOCK)}
        if os.path.realpath(out) in protected or os.path.isdir(out):
            raise WorkspaceError("refusing to write the page over %s (workspace data "
                                 "or a directory); choose another --out" % out)
        if not os.path.isdir(os.path.dirname(out)):
            raise WorkspaceError("output directory does not exist: %s"
                                 % os.path.dirname(out))
        return review_page.main(["--record", ws.p(RECORD), "--audit", ws.p(AUDIT),
                                 "--out", out])


def cmd_status(args):
    with Workspace(args.workspace) as ws:
        meta = ws.meta()
        audit = ws.audit()
        record = ws.record()
        holds = ws.open_holds(audit)
    counts = {"apply": 0, "hold": 0, "reject": 0}
    for e in audit:
        if e.get("decision") in counts:
            counts[e["decision"]] += 1
    res = [e for e in audit if e.get("type") == "resolution"]
    nvals = sum(len(v.get("metrics", {})) for v in record["entities"].values())
    print(json.dumps({
        "workspace": ws.path, "ingests": len(meta["ingests"]),
        "decisions": counts, "resolutions": {
            "accepted": sum(1 for e in res if e["action"] == "accept"),
            "dismissed": sum(1 for e in res if e["action"] == "dismiss")},
        "open_holds": len(holds), "entities": len(record["entities"]),
        "record_values": nvals}, indent=2))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Local review workspace (synthetic demo data).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a workspace from a seed record + registry")
    p.add_argument("workspace")
    p.add_argument("--record", default=os.path.join(HERE, "record.json"))
    p.add_argument("--registry", default=os.path.join(HERE, "registry.json"))
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("ingest", help="reconcile a candidates file into the workspace")
    p.add_argument("workspace")
    p.add_argument("--candidates", required=True)
    p.add_argument("--manifest", help="manifest mapping source_id to source document; "
                   "enables the value-in-source check and fills missing batch ids")
    p.add_argument("--batch", help="batch id for candidates that have none")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("queue", help="list open holds")
    p.add_argument("workspace")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_queue)

    p = sub.add_parser("resolve", help="accept or dismiss one hold")
    p.add_argument("workspace")
    p.add_argument("hold_id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true", help="apply the held value to the record")
    g.add_argument("--dismiss", action="store_true", help="close the hold; record unchanged")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--force", action="store_true",
                   help="accept even though the record changed since the hold")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("render", help="write the HTML review page")
    p.add_argument("workspace")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("status", help="summary counts")
    p.add_argument("workspace")
    p.set_defaults(fn=cmd_status)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except WorkspaceError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
