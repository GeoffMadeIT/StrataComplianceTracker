# Version: 1.3.2
"""Core: database, streaming CSV import with header mapping, and line matching across imports.

Design notes
- Each import is stored as "observations" (one per CSV row) holding only normalized fields needed for analysis and
  matching. Wide descriptive columns are not stored per row: control titles go in a small bpc_titles table (one per
  control), unrecognized columns are ignored unless you choose to keep them, and the raw remediation path is reduced
  to the part that matters for matching.
- CSV files are read in a stream (from bytes or from a path on disk), so memory stays flat for very large files.
- A "line" is the persistent identity of a control/device/object across imports. Line ids are derived by replaying
  imports in as-of-date order, so they can always be rebuilt.
- Line identity = device serial + Bpc Id + Object Id + Location, with the remediation location as a tie-breaker when
  several lines share those four. Human decisions (serial replaced, line retired, manual links, remediation section
  renamed) are stored as RULES keyed by field values, not by ids, so a rebuild re-applies them.
"""
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .log import log, timed

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "field_map.json").read_text())
DB_PATH = ROOT / "data" / "tracker.db"
SCHEMA_VERSION = 2

CANON = list(CONFIG["canonical"])
REQUIRED = ["bpc_id", "status"]
SERIAL_RE = re.compile(r"\d{14,16}")
OBS_COLS = ["serial_key", "device_group", "bpc_id", "control_groups", "device_type", "object_type", "severity",
            "location", "rem_tail", "object_id", "status", "user_excluded", "extras_json"]
CONTENT_FIELDS = ["serial_key", "bpc_id", "object_id", "status", "location", "rem_tail", "user_excluded"]
csv.field_size_limit(2 ** 31 - 1)       # descriptions can be long

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY, framework TEXT NOT NULL, as_of_date TEXT NOT NULL,
  imported_at TEXT NOT NULL, filename TEXT, file_hash TEXT, headers_json TEXT, row_count INTEGER,
  content_hash TEXT
);
CREATE TABLE IF NOT EXISTS lines (id INTEGER PRIMARY KEY, framework TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS obs (
  id INTEGER PRIMARY KEY, import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
  row_num INTEGER, line_id INTEGER,
  serial_key TEXT, device_group TEXT, bpc_id TEXT, control_groups TEXT, device_type TEXT, object_type TEXT,
  severity TEXT, location TEXT, rem_tail TEXT, object_id TEXT, status TEXT, user_excluded INTEGER, extras_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_import ON obs(import_id);
CREATE INDEX IF NOT EXISTS ix_obs_line ON obs(line_id);
-- control titles, stored once per control instead of on every row
CREATE TABLE IF NOT EXISTS bpc_titles (
  framework TEXT NOT NULL, bpc_id TEXT NOT NULL, title TEXT, as_of_date TEXT,
  PRIMARY KEY (framework, bpc_id)
);
-- a line that existed in an import but not in the next one
CREATE TABLE IF NOT EXISTS ended (
  framework TEXT, line_id INTEGER, last_import_id INTEGER, gone_import_id INTEGER,
  obs_id INTEGER, reason TEXT,         -- 'unresolved' | 'retired'
  returned_import_id INTEGER           -- set when the line reappears in a later import
);
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY, framework TEXT NOT NULL,
  kind TEXT NOT NULL,                  -- link | retire_line | serial_alias | retire_serial | rem_rename
  old_serial TEXT, old_bpc_id TEXT, old_object_id TEXT,
  new_serial TEXT, new_bpc_id TEXT, new_object_id TEXT,
  effective_date TEXT, note TEXT, created_at TEXT,
  section TEXT, old_value TEXT, new_value TEXT          -- rem_rename: section label and its old -> new value
);
CREATE TABLE IF NOT EXISTS views (
  id INTEGER PRIMARY KEY, framework TEXT NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL,
  created_at TEXT, updated_at TEXT, UNIQUE (framework, name)
);
CREATE TABLE IF NOT EXISTS view_cache (
  view_id INTEGER PRIMARY KEY REFERENCES views(id) ON DELETE CASCADE,
  config_json TEXT, data_version TEXT, payload_json TEXT, computed_at TEXT, compute_seconds REAL
);
CREATE TABLE IF NOT EXISTS option_cache (
  framework TEXT PRIMARY KEY, data_version TEXT, options_json TEXT
);
CREATE TABLE IF NOT EXISTS column_map (
  framework TEXT NOT NULL DEFAULT '', header_norm TEXT NOT NULL, canonical TEXT NOT NULL,
  PRIMARY KEY (framework, header_norm)     -- canonical: a field name, or _keep / _ignore for extra columns
);
"""


class ImportProblem(Exception):
    pass


class SchemaOutdated(Exception):
    """The database was created by an older version. Run remediate_db.py once."""


def connect(path=None, allow_old=False):
    path = Path(path) if path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    has_obs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='obs'").fetchone()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if has_obs and version < SCHEMA_VERSION and not allow_old:
        conn.close()
        raise SchemaOutdated(
            f"{path.name} was created by an older version of this tool. Close the app, then run "
            "`python remediate_db.py` once (it makes a backup first), and start the app again.")
    if not has_obs:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    return conn


# ----------------------------------------------------------------------------- headers and normalization
def norm_header(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def alias_table(conn=None, framework=""):
    """normalized header -> canonical field (or _keep/_ignore), from config aliases then saved decisions."""
    table = {}
    for canon, aliases in CONFIG["canonical"].items():
        for a in aliases:
            table[norm_header(a)] = canon
    if conn is not None:
        for scope in ("", framework):
            for r in conn.execute("SELECT header_norm, canonical FROM column_map WHERE framework=?", (scope,)):
                table[r["header_norm"]] = r["canonical"]
    return table


def plan_headers(conn, framework, headers, extra_map=None):
    """Decide what to do with each CSV column. Returns (mapping {header: field}, keep [headers],
    ignored [headers], undecided [headers]). Undecided headers are ignored until you choose."""
    table = alias_table(conn, framework)
    for h, c in (extra_map or {}).items():
        table[norm_header(h)] = c
    mapping, keep, ignored, undecided = {}, [], [], []
    for h in headers:
        c = table.get(norm_header(h))
        if c in CANON:
            mapping[h] = c
        elif c == "_keep":
            keep.append(h)
        elif c == "_ignore":
            ignored.append(h)
        else:
            undecided.append(h)
    return mapping, keep, ignored, undecided


def map_headers(conn, framework, headers):
    """Return ({header: field}, [headers with no decision yet])."""
    mapping, _, _, undecided = plan_headers(conn, framework, headers)
    return mapping, undecided


def _s(v):
    return "" if v is None else str(v).strip()


def _norm_id(v):
    v = _s(v)
    try:
        f = float(v)
        if f == int(f):
            return str(int(f))
    except ValueError:
        pass
    return v


def split_serial(raw):
    """Device Serial sometimes also carries a device-group name. Return (serial_key, device_group)."""
    raw = _s(raw)
    m = SERIAL_RE.search(raw)
    if m:
        rest = (raw[:m.start()] + " " + raw[m.end():]).strip(" /-_:;,\t")
        return m.group(), rest
    return raw.lower(), ""


def rem_tail(rem):
    """Remediation Location minus the segments that embed serials/ids (tsgid, Device, Manager), lower-cased.
    What is left are 'label:value' sections such as 'device group:corp-dg'."""
    parts = [p.strip() for p in _s(rem).split("/") if p.strip()]
    keep = [p.lower() for p in parts if not re.match(r"(tsgid|device|manager)\s*:", p, re.I)]
    return "/".join(keep)


def _truthy(v):
    v = _s(v).lower()
    if v in ("true", "1", "yes", "y", "t"):
        return 1
    if v in ("false", "0", "no", "n", "f"):
        return 0
    return None


def _norm_rec(rec):
    """rec: {field: raw string} -> normalized observation fields (without extras)."""
    key, group = split_serial(rec.get("device_serial", ""))
    return {
        "serial_key": key, "device_group": group, "bpc_id": _norm_id(rec.get("bpc_id")),
        "control_groups": _s(rec.get("control_groups")), "device_type": _s(rec.get("device_type")).lower(),
        "object_type": _s(rec.get("object_type")), "severity": _s(rec.get("severity")),
        "location": _s(rec.get("location")), "rem_tail": rem_tail(rec.get("remediation_location")),
        "object_id": _s(rec.get("object_id")), "status": _s(rec.get("status")).lower(),
        "user_excluded": _truthy(rec.get("user_excluded")),
    }


def row_digest(row):
    """8-byte digest of the fields that define what a row *means* (used for the duplicate-data check)."""
    s = "\x1f".join("" if row[c] is None else str(row[c]) for c in CONTENT_FIELDS)
    return hashlib.blake2b(s.encode(), digest_size=8).digest()


def content_hash_from_digests(digests):
    return hashlib.sha256(b"".join(sorted(digests))).hexdigest()


def recompute_content_hashes(conn):
    """(Re)compute every import's content hash from the stored rows."""
    for (imp_id,) in conn.execute("SELECT id FROM imports").fetchall():
        rows = conn.execute(f"SELECT {','.join(CONTENT_FIELDS)} FROM obs WHERE import_id=?", (imp_id,))
        conn.execute("UPDATE imports SET content_hash=? WHERE id=?",
                     (content_hash_from_digests([row_digest(r) for r in rows]), imp_id))
    conn.commit()


# ----------------------------------------------------------------------------- streaming CSV
class _HashingRaw(io.RawIOBase):
    """Raw reader that hashes every byte it hands out, so the file hash costs no extra pass."""

    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, b):
        n = self.f.readinto(b)
        if n:
            self.h.update(memoryview(b)[:n])
        return n


def _is_path(source):
    return isinstance(source, (str, os.PathLike))


def _open_source(source):
    raw = open(source, "rb") if _is_path(source) else io.BytesIO(source)
    hraw = _HashingRaw(raw)
    text = io.TextIOWrapper(io.BufferedReader(hraw, 1 << 20), encoding="utf-8-sig", errors="replace", newline="")
    return text, hraw


def read_csv_bytes(data):
    """Small-file helper: (headers, list of row dicts)."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    return headers, list(reader)


def guess_date(filename):
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", filename or "")
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


class Scan:
    """Result of one streaming pass over a file: no rows are kept, only what the checks need."""
    headers = mapping = keep = ignored = undecided = missing_required = None
    rows = 0
    file_hash = content_hash = None
    avg_len = None          # average characters per row for each column that isn't mapped to a field


def scan(conn, framework, source, extra_map=None):
    with timed("scan", fw=framework):
        text, hraw = _open_source(source)
        try:
            reader = csv.reader(text)
            headers = next(reader, None)
            if not headers:
                raise ImportProblem("The file is empty.")
            sc = Scan()
            sc.headers = headers
            sc.mapping, sc.keep, sc.ignored, sc.undecided = plan_headers(conn, framework, headers, extra_map)
            sc.missing_required = [c for c in REQUIRED if c not in sc.mapping.values()]
            idx = {}
            for i, h in enumerate(headers):
                if h in sc.mapping:
                    idx.setdefault(sc.mapping[h], i)
            other = {h: i for i, h in enumerate(headers) if h not in sc.mapping}
            sizes = dict.fromkeys(other, 0)
            digests, n = [], 0
            if not sc.missing_required:
                for row in reader:
                    if not row:
                        continue
                    nr = _norm_rec({c: row[i] if i < len(row) else "" for c, i in idx.items()})
                    digests.append(row_digest(nr))
                    n += 1
                    for h, i in other.items():
                        if i < len(row):
                            sizes[h] += len(row[i])
                sc.file_hash = hraw.h.hexdigest()
                sc.content_hash = content_hash_from_digests(digests)
            sc.rows = n
            sc.avg_len = {h: round(s / n) if n else 0 for h, s in sizes.items()}
            return sc
        finally:
            text.close()


# ----------------------------------------------------------------------------- importing
class DuplicateFile(ImportProblem):
    """This data is already stored. .existing lists the matching imports and why they matched."""

    def __init__(self, existing):
        self.existing = existing
        e = existing[0]
        how = "the same file" if e["match"] == "file" else "different file bytes but identical data"
        super().__init__(f"Already imported: {how} was imported on {e['imported_at']} as the "
                         f"{e['as_of_date']} export ({e['filename'] or 'unnamed'}).")


class SameDateConflict(ImportProblem):
    """Different data already exists for this as-of date. The caller must choose what to do."""

    def __init__(self, existing):
        self.existing = existing
        super().__init__(f"{len(existing)} import(s) with this as-of date already exist with different data. "
                         "Choose replace or keep both.")


def _import_rows(conn, framework):
    return conn.execute("SELECT * FROM imports WHERE framework=? ORDER BY as_of_date, id", (framework,)).fetchall()


def _iso(as_of_date):
    return as_of_date.isoformat() if hasattr(as_of_date, "isoformat") else str(as_of_date)


def preflight(conn, framework, as_of_date, source, extra_map=None):
    """Everything the Import screen should tell the user before importing; changes nothing.
    source: bytes, or a path to a CSV file on this computer."""
    as_of, framework = _iso(as_of_date), framework.strip()
    sc = scan(conn, framework, source, extra_map)
    out = {"headers": sc.headers, "rows": sc.rows, "mapping": sc.mapping, "keep": sc.keep, "ignored": sc.ignored,
           "undecided": sc.undecided, "avg_len": sc.avg_len, "missing_required": sc.missing_required,
           "file_dup": [], "same_date": [], "structure": {"kind": "first"}}
    prior = _import_rows(conn, framework)
    if not sc.missing_required:
        for i in prior:
            if i["file_hash"] == sc.file_hash:
                out["file_dup"].append({**dict(i), "match": "file"})
            elif i["content_hash"] == sc.content_hash:
                out["file_dup"].append({**dict(i), "match": "content"})
        out["same_date"] = [dict(i) for i in prior if i["as_of_date"] == as_of and i["file_hash"] != sc.file_hash
                            and i["content_hash"] != sc.content_hash]
    if prior:
        hist = [(i["as_of_date"], json.loads(i["headers_json"])) for i in prior]
        same = [d for d, h in hist if h == sc.headers]
        reorder = [d for d, h in hist if set(h) == set(sc.headers) and h != sc.headers]
        if same:
            out["structure"] = {"kind": "identical", "ref": same[-1]}
        elif reorder:
            out["structure"] = {"kind": "reordered", "ref": reorder[-1]}
        else:
            earlier = [(d, h) for d, h in hist if d <= as_of] or hist
            d, h = earlier[-1]
            out["structure"] = {"kind": "changed", "ref": d, "added": sorted(set(sc.headers) - set(h)),
                                "removed": sorted(set(h) - set(sc.headers))}
    log.info("preflight fw=%s rows=%d file_dup=%d same_date=%d structure=%s undecided=%d", framework, sc.rows,
             len(out["file_dup"]), len(out["same_date"]), out["structure"].get("kind"), len(sc.undecided))
    return out


def import_csv(conn, framework, as_of_date, source, filename="", extra_map=None, allow_duplicate=False,
               same_date_action=None):
    """Store one export, streaming it in batches.
    source:           bytes, or a path to a CSV file on this computer.
    extra_map:        {header: field | '_keep' | '_ignore'} decisions for columns that aren't recognized.
    allow_duplicate:  import even though identical data already exists.
    same_date_action: required when other data already exists for this as-of date: 'replace' or 'keep_both'."""
    framework = framework.strip()
    if not framework:
        raise ImportProblem("Framework name is required.")
    as_of = _iso(as_of_date)
    if not filename and _is_path(source):
        filename = Path(source).name
    with timed("import_csv", fw=framework, file=filename, as_of=as_of):
        for h, c in (extra_map or {}).items():
            if c and (c in CANON or c in ("_keep", "_ignore")):
                conn.execute("INSERT OR REPLACE INTO column_map VALUES (?,?,?)", (framework, norm_header(h), c))
        sc = scan(conn, framework, source)
        if sc.missing_required:
            raise ImportProblem(f"Required fields not found in headers: {', '.join(sc.missing_required)}. Map them first.")
        if not sc.rows:
            raise ImportProblem("The file has no data rows.")
        prior = _import_rows(conn, framework)
        dups = [{**dict(i), "match": "file" if i["file_hash"] == sc.file_hash else "content"} for i in prior
                if i["file_hash"] == sc.file_hash or i["content_hash"] == sc.content_hash]
        if dups and not allow_duplicate:
            raise DuplicateFile(dups)
        same = [dict(i) for i in prior if i["as_of_date"] == as_of]
        if same and same_date_action is None:
            raise SameDateConflict(same)
        if same_date_action not in (None, "replace", "keep_both"):
            raise ImportProblem(f"Unknown same-date action: {same_date_action}")
        try:
            import_id = _insert_import(conn, framework, as_of, source, filename, sc)
            replaced = 0
            if same_date_action == "replace":
                for i in same:
                    conn.execute("DELETE FROM imports WHERE id=?", (i["id"],))
                    replaced += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        order = [r["id"] for r in _import_rows(conn, framework)]
        if replaced or order[-1] != import_id:
            log.info("full rebuild after import (replaced=%d, latest=%s)", replaced, order[-1] == import_id)
            rebuild(conn, framework)
        else:
            append_import(conn, framework, import_id)
        log.info("imported %d rows into %s as of %s (replaced %d)", sc.rows, framework, as_of, replaced)
        return import_id


def _insert_import(conn, framework, as_of, source, filename, sc):
    cur = conn.execute(
        "INSERT INTO imports(framework, as_of_date, imported_at, filename, file_hash, headers_json, row_count,"
        " content_hash) VALUES (?,?,?,?,?,?,?,?)",
        (framework, as_of, dt.datetime.now().isoformat(timespec="seconds"), filename, sc.file_hash,
         json.dumps(sc.headers), sc.rows, sc.content_hash))
    import_id = cur.lastrowid
    ph = ",".join("?" * (len(OBS_COLS) + 2))
    sql = f"INSERT INTO obs(import_id,row_num,{','.join(OBS_COLS)}) VALUES ({ph})"
    idx = {}
    for i, h in enumerate(sc.headers):
        if h in sc.mapping:
            idx.setdefault(sc.mapping[h], i)
    keep_idx = {h: i for i, h in enumerate(sc.headers) if h in sc.keep}
    titles = {}
    text, _ = _open_source(source)
    try:
        reader = csv.reader(text)
        next(reader, None)
        batch, n = [], 0
        for row in reader:
            if not row:
                continue
            n += 1
            nr = _norm_rec({c: row[i] if i < len(row) else "" for c, i in idx.items()})
            if "bpc_title" in idx and idx["bpc_title"] < len(row):
                titles[nr["bpc_id"]] = _s(row[idx["bpc_title"]])
            extras = {h: row[i] for h, i in keep_idx.items() if i < len(row) and row[i] != ""}
            nr["extras_json"] = json.dumps(extras) if extras else None
            batch.append((import_id, n, *[nr[c] for c in OBS_COLS]))
            if len(batch) >= 5000:
                conn.executemany(sql, batch)
                batch = []
        if batch:
            conn.executemany(sql, batch)
    finally:
        text.close()
    conn.executemany(
        "INSERT INTO bpc_titles(framework,bpc_id,title,as_of_date) VALUES (?,?,?,?) "
        "ON CONFLICT(framework,bpc_id) DO UPDATE SET title=excluded.title, as_of_date=excluded.as_of_date "
        "WHERE excluded.as_of_date >= bpc_titles.as_of_date",
        [(framework, b, t, as_of) for b, t in titles.items()])
    return import_id


def titles(conn, framework):
    """bpc_id -> control title (stored once per control)."""
    return {r[0]: r[1] for r in conn.execute("SELECT bpc_id, title FROM bpc_titles WHERE framework=?", (framework,))}


def delete_import(conn, import_id):
    fw = conn.execute("SELECT framework FROM imports WHERE id=?", (import_id,)).fetchone()["framework"]
    conn.execute("DELETE FROM imports WHERE id=?", (import_id,))
    conn.commit()
    log.info("deleted import %s", import_id)
    rebuild(conn, fw)


def set_as_of(conn, import_id, as_of):
    fw = conn.execute("SELECT framework FROM imports WHERE id=?", (import_id,)).fetchone()["framework"]
    conn.execute("UPDATE imports SET as_of_date=? WHERE id=?", (str(as_of), import_id))
    conn.commit()
    log.info("changed as-of of import %s to %s", import_id, as_of)
    rebuild(conn, fw)


# ----------------------------------------------------------------------------- matching
def list_imports(conn, framework):
    return _import_rows(conn, framework)


def _rules(conn, framework):
    return conn.execute("SELECT * FROM rules WHERE framework=? ORDER BY id", (framework,)).fetchall()


class _Ctx:
    """Saved decisions turned into lookups for the matcher."""

    def __init__(self, rules):
        self.alias = {r["old_serial"]: (r["new_serial"], r["effective_date"] or "") for r in rules if r["kind"] == "serial_alias"}
        self.retired_serial = {r["old_serial"]: r["effective_date"] or "" for r in rules if r["kind"] == "retire_serial"}
        self.retired_line = {(r["old_serial"], r["old_bpc_id"], r["old_object_id"]) for r in rules if r["kind"] == "retire_line"}
        self.renames = [(r["section"], r["old_value"], r["new_value"], r["effective_date"] or "")
                        for r in rules if r["kind"] == "rem_rename"]
        self.links = defaultdict(list)
        for r in rules:
            if r["kind"] == "link":
                self.links[(r["old_serial"], r["old_bpc_id"], r["old_object_id"])].append(
                    (r["new_serial"], r["new_bpc_id"], r["new_object_id"]))

    def eff_serial(self, s, asof):
        for _ in range(10):
            if s in self.alias and self.alias[s][1] <= asof:
                s = self.alias[s][0]
            else:
                break
        return s

    def rename_tail(self, tail, asof):
        """Apply approved remediation-section renames to an older line's tail before comparing."""
        if not self.renames or not tail:
            return tail
        parts = tail.split("/")
        for label, old, new, eff in self.renames:
            if eff <= asof:
                parts = [f"{label}:{new}" if p == f"{label}:{old}" else p for p in parts]
        return "/".join(parts)


def _match(ctx, cands, cur, assigned, asof):
    """Match earlier observations (cands) to unassigned current rows. Returns the used candidate indexes."""
    used = set()
    pg, cg = defaultdict(list), defaultdict(list)
    for i, p in enumerate(cands):
        pg[(ctx.eff_serial(p["serial_key"], asof), p["bpc_id"], p["object_id"], p["location"])].append(i)
    for j, c in enumerate(cur):
        if j not in assigned:
            cg[(c["serial_key"], c["bpc_id"], c["object_id"], c["location"])].append(j)
    for k, cjs in cg.items():
        pis = pg.get(k)
        if pis:
            _pair(ctx, cands, cur, pis, cjs, assigned, used, asof)
    if ctx.links:
        cidx = defaultdict(list)
        for j, c in enumerate(cur):
            if j not in assigned:
                cidx[(c["serial_key"], c["bpc_id"], c["object_id"])].append(j)
        for i, p in enumerate(cands):
            if i in used:
                continue
            for nk in ctx.links.get((p["serial_key"], p["bpc_id"], p["object_id"]), []):
                free = [j for j in cidx.get(nk, []) if j not in assigned]
                if free:
                    assigned[free[0]] = p["line_id"]
                    used.add(i)
                    break
    return used


def _pair(ctx, cands, cur, pis, cjs, assigned, used, asof):
    """Rows sharing serial + Bpc Id + Object Id + Location. If exactly one row on each side, it is the same line
    whatever its remediation location says. If several share the key, the remediation location is the
    tie-breaker: only rows with an identical remediation location (after approved section renames) are paired.
    Anything left over counts as removed / added; it is never paired by file order."""
    if len(pis) == 1 and len(cjs) == 1:
        assigned[cjs[0]] = cands[pis[0]]["line_id"]
        used.add(pis[0])
        return
    by_tail = defaultdict(list)
    for i in pis:
        by_tail[ctx.rename_tail(cands[i]["rem_tail"], asof)].append(i)
    for j in cjs:
        pool = by_tail.get(cur[j]["rem_tail"])
        if pool:
            i = pool.pop(0)
            assigned[j] = cands[i]["line_id"]
            used.add(i)


def _step(conn, framework, ctx, imp, prev, prev_imp, pool):
    """Assign line ids to one import given the previous import and the pool of vanished lines."""
    cur = [dict(r) for r in conn.execute("SELECT * FROM obs WHERE import_id=? ORDER BY row_num", (imp["id"],))]
    asof = imp["as_of_date"]
    assigned = {}
    st = {"rows": len(cur), "matched": 0, "returned": 0, "ended_unresolved": 0, "ended_retired": 0}
    if prev:
        used_prev = _match(ctx, prev, cur, assigned, asof)
        st["matched"] = len(assigned)
        if pool:
            plist = list(pool.values())
            for i in _match(ctx, plist, cur, assigned, asof):
                lid = plist[i]["line_id"]
                conn.execute("UPDATE ended SET returned_import_id=? WHERE framework=? AND line_id=? "
                             "AND returned_import_id IS NULL", (imp["id"], framework, lid))
                pool.pop(lid, None)
                st["returned"] += 1
        ended = []
        for i, p in enumerate(prev):
            if i in used_prev:
                continue
            okey = (p["serial_key"], p["bpc_id"], p["object_id"])
            is_ret = okey in ctx.retired_line or (
                p["serial_key"] in ctx.retired_serial and ctx.retired_serial[p["serial_key"]] <= asof)
            ended.append((framework, p["line_id"], prev_imp["id"], imp["id"], p["id"], "retired" if is_ret else "unresolved"))
            if is_ret:
                st["ended_retired"] += 1
            else:
                st["ended_unresolved"] += 1
                pool[p["line_id"]] = p
        conn.executemany("INSERT INTO ended(framework,line_id,last_import_id,gone_import_id,obs_id,reason) "
                         "VALUES (?,?,?,?,?,?)", ended)
    new = 0
    for j, c in enumerate(cur):
        if j not in assigned:
            assigned[j] = conn.execute("INSERT INTO lines(framework) VALUES (?)", (framework,)).lastrowid
            new += 1
        c["line_id"] = assigned[j]
    st["new_lines"] = new
    conn.executemany("UPDATE obs SET line_id=? WHERE id=?", [(c["line_id"], c["id"]) for c in cur])
    log.info("matched import %s (%s): %s", imp["id"], asof, st)
    return cur, st


def rebuild(conn, framework):
    """Replay all imports in as-of order and (re)assign line ids using the matching rules + saved decisions.
    A line that disappears and later returns keeps its id (it is simply absent in between)."""
    with timed("rebuild", fw=framework):
        ctx = _Ctx(_rules(conn, framework))
        conn.execute("DELETE FROM ended WHERE framework=?", (framework,))
        conn.execute("UPDATE obs SET line_id=NULL WHERE import_id IN (SELECT id FROM imports WHERE framework=?)", (framework,))
        conn.execute("DELETE FROM lines WHERE framework=?", (framework,))
        prev, prev_imp, pool = [], None, {}
        for imp in list_imports(conn, framework):
            prev, _ = _step(conn, framework, ctx, imp, prev, prev_imp, pool)
            prev_imp = imp
        conn.commit()


def append_import(conn, framework, import_id):
    """Match a new chronologically-last import against stored state instead of replaying everything."""
    with timed("append_import", fw=framework, import_id=import_id):
        imps = list_imports(conn, framework)
        assert imps[-1]["id"] == import_id, "append_import needs the newest import"
        ctx = _Ctx(_rules(conn, framework))
        prev, prev_imp = [], None
        if len(imps) > 1:
            prev_imp = imps[-2]
            prev = [dict(r) for r in conn.execute("SELECT * FROM obs WHERE import_id=? ORDER BY row_num", (prev_imp["id"],))]
        pool = {}
        for r in conn.execute("SELECT o.* FROM ended e JOIN obs o ON o.id=e.obs_id WHERE e.framework=? "
                              "AND e.reason='unresolved' AND e.returned_import_id IS NULL ORDER BY e.rowid", (framework,)):
            pool[r["line_id"]] = dict(r)
        _step(conn, framework, ctx, imps[-1], prev, prev_imp, pool)
        conn.commit()


# ----------------------------------------------------------------------------- rules / review helpers
def add_rule(conn, framework, kind, effective_date=None, note="", **k):
    conn.execute(
        "INSERT INTO rules(framework,kind,old_serial,old_bpc_id,old_object_id,new_serial,new_bpc_id,"
        "new_object_id,effective_date,note,created_at,section,old_value,new_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (framework, kind, k.get("old_serial"), k.get("old_bpc_id"), k.get("old_object_id"), k.get("new_serial"),
         k.get("new_bpc_id"), k.get("new_object_id"), str(effective_date) if effective_date else "", note,
         dt.datetime.now().isoformat(timespec="seconds"), k.get("section"), k.get("old_value"), k.get("new_value")))


def delete_rule(conn, rule_id, framework):
    conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    conn.commit()
    rebuild(conn, framework)


def apply_rules(conn, framework, rules):
    """rules: list of (kind, effective_date, note, kwargs). One rebuild at the end."""
    for kind, eff, note, kw in rules:
        add_rule(conn, framework, kind, eff, note, **kw)
    conn.commit()
    log.info("saved %d rule(s): %s", len(rules), Counter(k for k, *_ in rules))
    rebuild(conn, framework)


def unresolved(conn, framework, gone_import_id):
    return [dict(r) for r in conn.execute(
        "SELECT o.*, e.line_id AS e_line FROM ended e JOIN obs o ON o.id=e.obs_id "
        "WHERE e.framework=? AND e.gone_import_id=? AND e.reason='unresolved' AND e.returned_import_id IS NULL "
        "ORDER BY o.serial_key,o.bpc_id", (framework, gone_import_id))]


def new_lines(conn, framework, import_id, prev_import_id):
    """Observations in import_id whose line did not exist in the previous import."""
    return [dict(r) for r in conn.execute(
        "SELECT o.* FROM obs o WHERE o.import_id=? AND o.line_id NOT IN "
        "(SELECT line_id FROM obs WHERE import_id=?) AND o.line_id NOT IN "
        "(SELECT line_id FROM ended WHERE returned_import_id=?) ORDER BY o.serial_key,o.bpc_id",
        (import_id, prev_import_id, import_id))]


def _params(section, defaults):
    return {**defaults, **CONFIG.get(section, {})}


def suggest_serial_aliases(gone, added, names=None, params=None):
    """For each vanished serial, which newly appearing serial is the likely replacement?

    Evidence is the share of lines (bpc_id + object_id + location) two serials have in common, measured both ways:
    what fraction of the old device's lines reappear, and what fraction of the new device's lines were on the old one.
    Many devices of one type share most controls, so a handful of shared lines proves nothing. Thresholds come
    from config 'alias_suggestions'. For small devices (few lines) the overlap is weak evidence, so a name match
    or a near-complete overlap is also required. A matching device name (names: serial -> name) raises a candidate's
    rank, and is offered on its own, flagged 'name only', only when nothing else qualifies. Never decides anything."""
    p = _params("alias_suggestions", {"min_old_pct": 0.7, "min_new_pct": 0.5, "small_lines": 200, "small_min_pct": 0.95})
    names = names or {}
    old_sets, new_sets, new_index = defaultdict(set), defaultdict(set), defaultdict(set)
    for g in gone:
        old_sets[g["serial_key"]].add((g["bpc_id"], g["object_id"], g["location"]))
    for a in added:
        t = (a["bpc_id"], a["object_id"], a["location"])
        new_sets[a["serial_key"]].add(t)
        new_index[t].add(a["serial_key"])
    nm = lambda s: (names.get(s) or "").casefold()
    out = []
    for old, otrip in old_sets.items():
        cnt = Counter(s for t in otrip for s in new_index.get(t, ()))
        small = len(otrip) < p["small_lines"]
        good, named_only = [], []
        for new in set(cnt) | {s for s in new_sets if nm(old) and nm(s) == nm(old)}:
            if new == old:
                continue
            n = cnt.get(new, 0)
            pct_old, pct_new = n / len(otrip), (n / len(new_sets[new]) if new_sets[new] else 0.0)
            same = bool(nm(old)) and nm(new) == nm(old)
            strong = pct_old >= p["min_old_pct"] and pct_new >= p["min_new_pct"]
            if small:
                strong = strong and (same or (pct_old >= p["small_min_pct"] and pct_new >= p["small_min_pct"]))
            rec = {"old_serial": old, "new_serial": new, "gone_lines": len(otrip), "shared_lines": n,
                   "pct_old": pct_old, "pct_new": pct_new, "same_name": same, "small": small,
                   "basis": "name + lines" if same else "lines"}
            if strong:
                good.append(rec)
            elif same:
                named_only.append({**rec, "basis": "name only"})
        pick = (max(good, key=lambda r: (r["same_name"], r["pct_old"], r["pct_new"])) if good else
                max(named_only, key=lambda r: (r["shared_lines"], r["pct_old"])) if named_only else None)
        if pick:
            out.append(pick)
    return sorted(out, key=lambda r: (not r["same_name"], -r["pct_old"]))


def _single_section_change(a_parts, b_parts):
    """Two remediation tails (as lists of 'label:value') that differ in exactly one section's value."""
    if len(a_parts) != len(b_parts):
        return None
    diffs = [(x, y) for x, y in zip(a_parts, b_parts) if x != y]
    if len(diffs) != 1:
        return None
    (x, y), = diffs
    if ":" not in x or ":" not in y:
        return None
    (lx, vx), (ly, vy) = x.split(":", 1), y.split(":", 1)
    return (lx, vx, vy) if lx == ly else None


def suggest_rem_renames(conn, framework, gone_import_id):
    """Remediation sections that look renamed. Only duplicates of the same serial + bpc + object + location are
    ever tied-broken by remediation, so those are the only lines a rename can strand. A change is suggested
    only when the same section changes between the same two values on several lines AND several devices
    (config 'rename_suggestions'); a lone change is left as one line removed and one added."""
    p = _params("rename_suggestions", {"min_lines": 2, "min_devices": 2})
    imps = list_imports(conn, framework)
    k = next(i for i, x in enumerate(imps) if x["id"] == gone_import_id)
    if k == 0:
        return []
    asof, prev_id = imps[k]["as_of_date"], imps[k - 1]["id"]
    ctx = _Ctx(_rules(conn, framework))
    gone, added = unresolved(conn, framework, gone_import_id), new_lines(conn, framework, gone_import_id, prev_id)
    aidx = defaultdict(list)
    for a in added:
        aidx[(a["serial_key"], a["bpc_id"], a["object_id"], a["location"])].append(a)
    found = defaultdict(lambda: {"lines": 0, "devices": set()})
    gidx = defaultdict(list)
    for g in gone:
        gidx[(ctx.eff_serial(g["serial_key"], asof), g["bpc_id"], g["object_id"], g["location"])].append(g)
    for key, gs in gidx.items():
        cands = aidx.get(key, [])
        used = set()
        for g in gs:
            gt = ctx.rename_tail(g["rem_tail"], asof).split("/")
            for ai, a in enumerate(cands):
                if ai in used:
                    continue
                ch = _single_section_change(gt, a["rem_tail"].split("/"))
                if ch:
                    f = found[ch]
                    f["lines"] += 1
                    f["devices"].add(key[0])
                    used.add(ai)
                    break
    return sorted(({"section": s, "old": o, "new": n, "lines": f["lines"], "devices": len(f["devices"])}
                   for (s, o, n), f in found.items()
                   if f["lines"] >= p["min_lines"] and len(f["devices"]) >= p["min_devices"]),
                  key=lambda r: -r["lines"])


def suggest_line_links(gone, added):
    """Use the non-automatic tiers from config. Only one-to-one candidates are suggested."""
    sugg, taken_old, taken_new = [], set(), set()
    for tier in CONFIG["tiers"]:
        if tier.get("auto"):
            continue
        f = tier["fields"]
        kf = lambda o: tuple(o[x] for x in f)
        gi, ai = defaultdict(list), defaultdict(list)
        for g in gone:
            if g["id"] not in taken_old:
                gi[kf(g)].append(g)
        for a in added:
            if a["id"] not in taken_new:
                ai[kf(a)].append(a)
        for k, gs in gi.items():
            as_ = ai.get(k, [])
            if len(gs) == 1 and len(as_) == 1:
                g, a = gs[0], as_[0]
                sugg.append({"tier": tier["name"], "old": g, "new": a})
                taken_old.add(g["id"]); taken_new.add(a["id"])
    return sugg
