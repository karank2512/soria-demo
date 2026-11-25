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
import re
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
SOURCES = "sources"   # write-once source documents, named by sha256


class WorkspaceError(Exception):
    """A user-facing error: printed without a traceback, exit code 2.

    code is a stable machine-readable reason (e.g. "locked",
    "already_resolved", "record_changed") so a UI can choose how to present
    a refusal without parsing the message.
    """

    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


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
        raise WorkspaceError("%s not found: %s" % (what, path), "not_found")
    except OSError as e:  # a directory, permissions, ...
        raise WorkspaceError("cannot read %s (%s): %s" % (what, e.strerror, path),
                             "unreadable")
    except (ValueError, RecursionError) as e:
        raise WorkspaceError("%s is not valid JSON (%s): %s" % (what, e, path), "bad_json")


# Shared with reconcile.py's CLI so both entry points refuse the same files.
validate_record = reconcile.record_problems
validate_registry = reconcile.registry_problems


# Called with the workspace path whenever opening it completes an interrupted
# change, whatever the command then does (it may still refuse). The app uses
# this so the "change was completed" notice cannot be lost.
RECOVERY_LISTENERS = []


_ID_RE = re.compile(r"([DR])-([1-9][0-9]*)")
META_KEYS = ("schema", "created", "next_decision", "next_resolution", "ingests")


def journal_digest(audit, record, meta):
    """sha256 over a journal's canonical content; stored in the journal."""
    body = reconcile.dumps_safe({"audit": audit, "record": record, "meta": meta},
                                sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def journal_problem(txn, current_meta, current_audit, current_record):
    """Why a journal must not be rolled forward, or None if it is sound.

    These checks refuse a journal that is damaged or does not follow on from
    this workspace. They are consistency checks, not a defence against
    deliberate forgery (anyone can recompute a sha256; the workspace is not
    tamper-proof). A journal is sound only if all of the following hold:
    - exactly the fields commit() writes, with a matching sha256;
    - complete schema-1 metadata for this workspace (same "created"),
      counters that do not go backwards, and an import history that only
      grows by appending at most one entry;
    - a valid record, when it carries one;
    - well-formed entries whose ids are exactly the next D-/R- ids, where
      each resolution resolves an open hold of this workspace.
    A journal already applied up to its metadata (crash just before its
    removal) is sound only if all its entries are already in the audit log
    and its record is the current one.
    """
    if not isinstance(txn, dict) or set(txn) != {"audit", "record", "meta", "sha256"}:
        return "not a journal written by this tool"
    audit, record, meta = txn["audit"], txn["record"], txn["meta"]
    if not isinstance(audit, list) or not isinstance(meta, dict) or \
            not (record is None or isinstance(record, dict)):
        return "audit must be a list, meta an object, record an object or null"
    if not isinstance(txn["sha256"], str) or \
            txn["sha256"] != journal_digest(audit, record, meta):
        return "checksum missing or does not match its content"
    if set(meta) != set(META_KEYS) or meta["schema"] != SCHEMA:
        return "metadata is incomplete or has the wrong schema"
    nd, nr = meta["next_decision"], meta["next_resolution"]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in (nd, nr)) \
            or not isinstance(meta["ingests"], list):
        return "metadata counters are invalid"
    if meta["created"] != current_meta.get("created"):
        return "metadata belongs to a different workspace"
    cur_ing = current_meta.get("ingests", [])
    if meta["ingests"][:len(cur_ing)] != cur_ing or \
            len(meta["ingests"]) - len(cur_ing) not in (0, 1):
        return "import history does not follow on from this workspace"
    if record is not None and reconcile.record_problems(record):
        return "record is not valid"

    ids = []
    for e in audit:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or \
                not _ID_RE.fullmatch(e["id"]):
            return "audit entry with an invalid id"
        if e["id"][0] == "D" and e.get("decision") not in ("apply", "hold", "reject"):
            return "decision entry %s is incomplete" % e["id"]
        if e["id"][0] == "R" and (e.get("type") != "resolution"
                                  or e.get("action") not in ("accept", "dismiss")
                                  or not isinstance(e.get("resolves"), str)):
            return "resolution entry %s is incomplete" % e["id"]
        ids.append(e["id"])
    if len(set(ids)) != len(ids):
        return "audit entries repeat an id"

    present = {e.get("id") for e in current_audit}
    if meta == current_meta:
        # Applied up to the metadata already: nothing may be new.
        if any(i not in present for i in ids):
            return "entries missing although the metadata was already applied"
        if record is not None and record != current_record:
            return "record differs from the one already applied"
        return None

    cd, cr = current_meta.get("next_decision"), current_meta.get("next_resolution")
    if nd < cd or nr < cr:
        return "counters go backwards"
    got_d = sorted(int(i[2:]) for i in ids if i[0] == "D")
    got_r = sorted(int(i[2:]) for i in ids if i[0] == "R")
    if got_d != list(range(cd, nd)) or got_r != list(range(cr, nr)):
        return "audit ids do not match the next ids of this workspace"
    grew = len(meta["ingests"]) - len(cur_ing)
    if (got_r and (got_d or grew)) or (got_d and not grew):
        return "a change is either one import or one resolution"
    holds = {e.get("id") for e in current_audit if e.get("decision") == "hold"}
    resolved = {e.get("resolves") for e in current_audit if e.get("type") == "resolution"}
    for e in audit:
        if e["id"][0] == "R" and e["id"] not in present and (
                e["resolves"] not in holds or e["resolves"] in resolved):
            return "resolution %s does not resolve an open hold" % e["id"]
    return None


class Workspace:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._locked = False
        self.recovered = False  # True when entering completed an interrupted change

    def p(self, name):
        return os.path.join(self.path, name)

    # ---- locking and transactions ------------------------------------
    def __enter__(self):
        if not os.path.exists(self.p(META)):
            raise WorkspaceError("not a workspace (no %s): %s" % (META, self.path),
                                 "not_workspace")
        try:
            fd = os.open(self.p(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise WorkspaceError(
                "workspace is locked by another command (%s). If no other command "
                "is running, the lock is stale and can be removed." % self.p(LOCK),
                "locked")
        with os.fdopen(fd, "w") as f:
            f.write("pid %d at %s\n" % (os.getpid(), _now()))
        self._locked = True
        try:
            self.recovered = self.recover()
            if self.recovered:
                for listener in list(RECOVERY_LISTENERS):
                    listener(self.path)
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
        try:
            txn = _load_json(self.p(TXN), "journal")
        except WorkspaceError as e:
            raise WorkspaceError("the change journal %s is damaged (%s); nothing was "
                                 "changed. Inspect it before using this workspace."
                                 % (self.p(TXN), e), "corrupt_journal")
        problem = journal_problem(txn, _load_json(self.p(META), "workspace metadata"),
                                  self._complete_audit_entries(),
                                  _load_json(self.p(RECORD), "record"))
        if problem:
            raise WorkspaceError("the change journal %s is damaged (%s); nothing was "
                                 "changed. Inspect it before using this workspace."
                                 % (self.p(TXN), problem), "corrupt_journal")
        self._roll_forward(txn)
        return True

    def _complete_audit_entries(self):
        """Audit entries on complete lines, ignoring a torn final line.

        Used to validate a journal before anything is written: the torn line
        is the one the journal's own append was writing, and is repaired only
        once the journal has been accepted.
        """
        try:
            with open(self.p(AUDIT), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return []
        if data and not data.endswith(b"\n"):
            data = data[:data.rfind(b"\n") + 1]
        entries = []
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    raise WorkspaceError("audit log line %d is not valid JSON: %s"
                                         % (n, self.p(AUDIT)), "corrupt_audit")
        return entries

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
        txn = {"audit": audit_entries, "record": record, "meta": meta,
               "sha256": journal_digest(audit_entries, record, meta)}
        _atomic_write(self.p(TXN), reconcile.dumps_safe(txn) + "\n")
        self._roll_forward(txn)

    # ---- state ---------------------------------------------------------
    def meta(self):
        meta = _load_json(self.p(META), "workspace metadata")
        if meta.get("schema") != SCHEMA:
            raise WorkspaceError("unsupported workspace schema %r (expected %d)"
                                 % (meta.get("schema"), SCHEMA), "schema")
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
                                         % (n, self.p(AUDIT)), "corrupt_audit")
        return entries

    def store_source(self, text):
        """Keep a source document, write-once, named by its sha256. Returns it.

        Content-addressed files are never rewritten, so storing is safe
        outside the journal: a crash leaves at most an unreferenced file.
        """
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        folder = self.p(SOURCES)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, digest + ".txt")
        if not os.path.exists(path):
            _atomic_write(path, text)
        return digest

    def source_text(self, digest):
        """Stored source text for a digest, or None if it was never stored."""
        if not isinstance(digest, str) or len(digest) != 64 or \
                any(c not in "0123456789abcdef" for c in digest):
            return None
        try:
            with open(os.path.join(self.p(SOURCES), digest + ".txt"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def open_holds(self, audit=None):
        audit = self.audit() if audit is None else audit
        resolved = {e.get("resolves") for e in audit if e.get("type") == "resolution"}
        return [e for e in audit
                if e.get("decision") == "hold" and e.get("id") not in resolved]


# ---- library API (used by the CLI below and by app.py) --------------------

def init_workspace(path, record, registry):
    """Create a workspace from an in-memory seed record and registry."""
    path = os.path.abspath(path)
    if os.path.exists(os.path.join(path, META)):
        raise WorkspaceError("workspace already exists: %s" % path, "exists")
    if os.path.exists(path) and not os.path.isdir(path):
        raise WorkspaceError("path exists and is not a directory: %s" % path, "bad_path")
    if os.path.isdir(path) and os.listdir(path):
        raise WorkspaceError("directory is not empty: %s" % path, "bad_path")
    problems = validate_record(record) + validate_registry(registry)
    if problems:
        raise WorkspaceError("seed files are not usable:\n  " + "\n  ".join(problems),
                             "bad_seed")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise WorkspaceError("cannot create workspace directory (%s): %s"
                             % (e.strerror, path), "bad_path")
    _atomic_write(os.path.join(path, REGISTRY), json.dumps(registry, indent=2) + "\n")
    _atomic_write(os.path.join(path, RECORD), reconcile.dumps_safe(record, indent=2) + "\n")
    open(os.path.join(path, AUDIT), "a").close()
    meta = {"schema": SCHEMA, "created": _now(), "next_decision": 1,
            "next_resolution": 1, "ingests": []}
    # metadata last: its presence marks the workspace as complete.
    _atomic_write(os.path.join(path, META), json.dumps(meta, indent=2) + "\n")
    return path


def parse_candidates(raw):
    """Bytes of a candidates file -> list. Raises WorkspaceError."""
    try:
        candidates = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        raise WorkspaceError("candidates file is not valid JSON: %s" % e, "bad_json")
    if not isinstance(candidates, list):
        raise WorkspaceError("candidates file must be a JSON list", "not_list")
    return candidates


def parse_manifest(manifest, read_file):
    """Validate a manifest list and read its documents.

    read_file(name) returns the text of a document named by an entry's
    "file", or raises WorkspaceError. Returns (texts, batches), both keyed by
    source_id.
    """
    if not isinstance(manifest, list):
        raise WorkspaceError("manifest must be a JSON list", "bad_manifest")
    texts, batches = {}, {}
    for i, entry in enumerate(manifest):
        if not isinstance(entry, dict) or not isinstance(entry.get("source_id"), str):
            raise WorkspaceError("manifest entry %d needs a string source_id" % i,
                                 "bad_manifest")
        sid = entry["source_id"]
        if isinstance(entry.get("batch"), str):
            batches[sid] = entry["batch"]
        if isinstance(entry.get("file"), str):
            texts[sid] = read_file(entry["file"])
    return texts, batches


def _load_manifest(path):
    """Read a demo-style manifest file: [{source_id, file, batch}], files relative to it."""
    manifest = _load_json(path, "manifest")
    base = os.path.dirname(os.path.abspath(path))

    def read_file(name):
        fpath = os.path.join(base, name)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            raise WorkspaceError("manifest source: %s is not UTF-8 text" % fpath,
                                 "bad_manifest")
        except OSError as e:
            raise WorkspaceError("manifest source: cannot read %s (%s)" % (fpath, e.strerror),
                                 "bad_manifest")
    return parse_manifest(manifest, read_file)


def ingest(path, raw, filename, texts=None, batches=None, batch_default=None):
    """Reconcile a candidates file (raw bytes) into the workspace.

    texts / batches: optional source_id -> document text / batch id (from a
    manifest). Returns {"entries", "counts", "batches", "open_holds"}.
    Refusals raise WorkspaceError and change nothing.
    """
    candidates = parse_candidates(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if batches:
        for c in candidates:
            if (isinstance(c, dict) and "batch" not in c
                    and isinstance(c.get("source_id"), str) and c["source_id"] in batches):
                c["batch"] = batches[c["source_id"]]
    if batch_default:
        for c in candidates:
            if isinstance(c, dict) and "batch" not in c:
                c["batch"] = batch_default
    unbatched = sum(1 for c in candidates if isinstance(c, dict) and "batch" not in c)

    with Workspace(path) as ws:
        meta = ws.meta()
        # An exact re-import is reported as such first, whatever else is wrong.
        same_file = [ing for ing in meta["ingests"] if ing.get("sha256") == digest]
        if same_file:
            raise WorkspaceError("this exact candidates file was already ingested at %s. "
                                 "Nothing was changed." % same_file[0]["ts"], "duplicate_file")
        if unbatched:
            raise WorkspaceError(
                "%d candidate(s) have no batch id. Add a 'batch' field, use a manifest "
                "that assigns one, or pass --batch NAME. Nothing was changed." % unbatched,
                "unbatched")
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
                "a new batch id." % ", ".join(repeated), "duplicate_batch")

        decisions, record = reconcile.reconcile_batches(
            ws.registry(), ws.record(), candidates, source_texts=texts)
        stored = {}
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
                if line["source_checked"]:
                    if sid not in stored:
                        stored[sid] = ws.store_source(texts[sid])
                    line["source_sha256"] = stored[sid]
            entries.append(line)
        counts = {"apply": 0, "hold": 0, "reject": 0}
        for e in entries:
            counts[e["decision"]] += 1
        meta["next_decision"] = n
        meta["ingests"].append({
            "ts": ts, "file": os.path.basename(filename),
            "sha256": digest,
            "batches": incoming, "decisions": len(entries), "counts": counts,
            "first_id": entries[0]["id"] if entries else None})
        ws.commit(entries, record, meta)
        open_n = len(ws.open_holds())
        recovered = ws.recovered

    return {"entries": entries, "counts": counts, "batches": incoming, "open_holds": open_n,
            "recovered": recovered}


def _short(state):
    if not state:
        return "no value"
    return "%s as of %s from %s" % (state["value"], state["as_of"], state["source_id"])


def _record_state(record, entity, metric):
    """What a hold was compared against; used to detect later record changes."""
    m = record.get("entities", {}).get(entity, {}).get("metrics", {}).get(metric)
    if not isinstance(m, dict):
        return None
    return {"value": m.get("value"), "as_of": m.get("as_of"),
            "source_id": (m.get("source") or {}).get("source_id")}


def hold_staleness(record, hold):
    """(then, now) record states for a hold; then != now means it changed."""
    now = _record_state(record, hold.get("entity"), hold.get("metric"))
    return hold.get("record_at_hold", now), now


def resolve(path, hold_id, action, reviewer, note="", force=False):
    """Accept or dismiss one hold. Returns {"entry", "open_holds"}."""
    if action not in ("accept", "dismiss"):
        raise WorkspaceError("action must be accept or dismiss", "bad_action")
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise WorkspaceError("--reviewer is required (who is making this call)",
                             "no_reviewer")
    with Workspace(path) as ws:
        meta = ws.meta()
        audit = ws.audit()
        target = next((e for e in audit if e.get("id") == hold_id), None)
        if target is None:
            raise WorkspaceError("no decision with id %s" % hold_id, "unknown_id")
        if target.get("decision") != "hold":
            raise WorkspaceError("%s is not a hold (it was %s); only holds can be resolved"
                                 % (hold_id, target.get("decision")), "not_a_hold")
        done = next((e for e in audit if e.get("type") == "resolution"
                     and e.get("resolves") == hold_id), None)
        if done is not None:
            raise WorkspaceError("%s was already resolved by %s (%s, %s)"
                                 % (hold_id, done["id"], done["action"], done["reviewer"]),
                                 "already_resolved")

        entry = {"id": "R-%d" % meta["next_resolution"], "type": "resolution",
                 "ts": _now(), "resolves": hold_id, "action": action,
                 "reviewer": reviewer, "note": note or "",
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
            then, now = hold_staleness(record, target)
            if now != then and not force:
                raise WorkspaceError(
                    "the record for %s / %s changed since %s was held (then %s, now %s). "
                    "Accepting would overwrite the newer value. Review it, then re-run "
                    "with --force to accept anyway, or --dismiss."
                    % (target.get("entity"), target.get("metric"), hold_id,
                       _short(then), _short(now)), "record_changed")
            cand = {k: target.get(k) for k in ("metric", "unit", "period", "pub_date",
                                                "source_type", "source_id", "citation")}
            cand["value"] = target.get("claimed_value")
            if reconcile.candidate_error(dict(cand, entity=target.get("entity"))):
                raise WorkspaceError("%s cannot be applied: its stored fields are "
                                     "incomplete" % hold_id, "incomplete")
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
        recovered = ws.recovered
    return {"entry": entry, "open_holds": left, "recovered": recovered}


def summary(meta, audit, record, holds):
    counts = {"apply": 0, "hold": 0, "reject": 0}
    for e in audit:
        if e.get("decision") in counts and e.get("type") != "resolution":
            counts[e["decision"]] += 1
    res = [e for e in audit if e.get("type") == "resolution"]
    return {"ingests": len(meta["ingests"]), "decisions": counts,
            "resolutions": {"accepted": sum(1 for e in res if e["action"] == "accept"),
                            "dismissed": sum(1 for e in res if e["action"] == "dismiss")},
            "open_holds": len(holds), "entities": len(record["entities"]),
            "record_values": sum(len(v.get("metrics", {}))
                                 for v in record["entities"].values())}


# ---- CLI commands (thin wrappers over the library) ---------------------------

def cmd_init(args):
    record = _load_json(args.record, "seed record")
    registry = _load_json(args.registry, "registry")
    path = init_workspace(args.workspace, record, registry)
    print("initialized workspace %s" % path)
    return 0


def cmd_ingest(args):
    try:
        with open(args.candidates, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise WorkspaceError("cannot read candidates file: %s" % e, "unreadable")
    parse_candidates(raw)  # report JSON errors before reading the manifest
    texts = batches = None
    if args.manifest:
        texts, batches = _load_manifest(args.manifest)
    r = ingest(args.workspace, raw, args.candidates, texts=texts, batches=batches,
               batch_default=args.batch)
    c = r["counts"]
    print("ingested %d candidates in %d batch(es): %d applied, %d held, %d rejected. "
          "%d hold(s) open in the queue."
          % (len(r["entries"]), len(r["batches"]), c["apply"], c["hold"], c["reject"],
             r["open_holds"]))
    return 0


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


def cmd_resolve(args):
    action = "accept" if args.accept else "dismiss"
    r = resolve(args.workspace, args.hold_id, action, args.reviewer, args.note, args.force)
    entry = r["entry"]
    what = ("accepted: record updated" if action == "accept"
            else "dismissed: record unchanged")
    print("%s %s %s (%s). %d hold(s) still open." % (entry["id"], args.hold_id, what,
                                                     entry["reviewer"], r["open_holds"]))
    return 0


def cmd_render(args):
    import review_page
    with Workspace(args.workspace) as ws:
        out = os.path.abspath(args.out or ws.p("review.html"))
        protected = {os.path.realpath(ws.p(n)) for n in
                     (META, RECORD, REGISTRY, AUDIT, TXN, LOCK)}
        def is_protected(path):
            if os.path.isdir(path) or os.path.realpath(path) in protected:
                return True
            for name in (META, RECORD, REGISTRY, AUDIT, TXN, LOCK):
                try:  # also catches hard links to workspace files
                    if os.path.samefile(path, ws.p(name)):
                        return True
                except OSError:
                    pass
            return False
        if is_protected(out):
            raise WorkspaceError("refusing to write the page over %s (workspace data "
                                 "or a directory); choose another --out" % out, "bad_out")
        if not os.path.isdir(os.path.dirname(out)):
            raise WorkspaceError("output directory does not exist: %s"
                                 % os.path.dirname(out), "bad_out")
        return review_page.main(["--record", ws.p(RECORD), "--audit", ws.p(AUDIT),
                                 "--out", out])


def cmd_status(args):
    with Workspace(args.workspace) as ws:
        meta = ws.meta()
        audit = ws.audit()
        record = ws.record()
        holds = ws.open_holds(audit)
    print(json.dumps(dict({"workspace": ws.path}, **summary(meta, audit, record, holds)),
                     indent=2))
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
