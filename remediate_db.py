# Version: 1.3.2
"""One-time upgrade of an existing tracker database to the current layout.

    python remediate_db.py                 # asks before changing anything
    python remediate_db.py --dry-run       # only report what it would do
    python remediate_db.py --db path.db    # a different database file
    python remediate_db.py --keep-extras   # keep per-row extra columns (default: drop them)
    python remediate_db.py --yes           # don't ask

Close the dashboard first. A backup copy is made next to the database before anything changes.

What it does
  1. Control titles move to one row per control (bpc_titles) instead of being repeated on every row.
  2. Wide per-row columns are dropped: bpc_title, serial_raw, the raw remediation_location (the part used for
     matching, rem_tail, is kept), and extras_json unless --keep-extras.
  3. Adds the new rule columns, recomputes each import's duplicate-detection hash, and clears cached view results.
  4. Re-matches every import with the current rules (line = serial + Bpc Id + Object Id + Location, remediation
     location as the tie-breaker). Your saved decisions (replaced devices, retired lines, links) are kept.
  5. Compacts the file (VACUUM).
"""
import argparse
import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path

from tracker import core
from tracker.version import VERSION

DROPPED = ["bpc_title", "serial_raw", "remediation_location"]
NEW_OBS = ("INSERT INTO obs_new(id, import_id, row_num, line_id, serial_key, device_group, bpc_id, control_groups, "
           "device_type, object_type, severity, location, rem_tail, object_id, status, user_excluded, extras_json) "
           "SELECT id, import_id, row_num, line_id, serial_key, device_group, bpc_id, control_groups, device_type, "
           "object_type, severity, location, rem_tail, object_id, status, user_excluded, {extras} FROM obs")


def cols(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def mb(path):
    return os.path.getsize(path) / 1e6


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(core.DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-extras", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    a = ap.parse_args()
    path = Path(a.db)
    if not path.exists():
        sys.exit(f"No database at {path}. Nothing to do.")
    db = sqlite3.connect(path)
    has_obs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='obs'").fetchone()
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not has_obs:
        sys.exit("The database has no data tables yet. Nothing to do.")
    if version >= core.SCHEMA_VERSION:
        sys.exit(f"{path.name} is already up to date (schema version {version}).")

    oc = cols(db, "obs")
    n_rows = db.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
    n_imp = db.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    print(f"remediate_db (project version {VERSION})")
    print(f"Database: {path}  ({mb(path):.1f} MB, {n_imp} imports, {n_rows:,} rows)")
    sizes = {c: db.execute(f"SELECT COALESCE(SUM(LENGTH({c})),0) FROM obs").fetchone()[0] for c in DROPPED + ["extras_json"] if c in oc}
    for c, s in sizes.items():
        print(f"  column {c:<22} {s / 1e6:8.1f} MB of text" + ("  (kept: --keep-extras)" if c == "extras_json" and a.keep_extras
                                                           else "  (will be dropped)"))
    if "extras_json" in oc:
        n_extra = db.execute("SELECT COUNT(*) FROM obs WHERE extras_json IS NOT NULL").fetchone()[0]
        print(f"  rows with extra columns: {n_extra:,}")
    if "bpc_title" in oc:
        multi = db.execute(
            "SELECT COUNT(*) FROM (SELECT i.framework, o.bpc_id FROM obs o JOIN imports i ON i.id=o.import_id "
            "WHERE o.bpc_title<>'' GROUP BY i.framework, o.bpc_id HAVING COUNT(DISTINCT o.bpc_title)>1)").fetchone()[0]
        print(f"  controls whose title changed over time: {multi} (the most recent title is kept)")
    before = {}
    for fw, in db.execute("SELECT DISTINCT framework FROM imports"):
        before[fw] = (db.execute("SELECT COUNT(*) FROM lines WHERE framework=?", (fw,)).fetchone()[0],
                      db.execute("SELECT COUNT(*) FROM ended WHERE framework=? AND reason='unresolved' "
                                 "AND returned_import_id IS NULL", (fw,)).fetchone()[0] if "returned_import_id" in cols(db, "ended") else None)
    if a.dry_run:
        print("\nDry run: nothing changed.")
        return
    if not a.yes and input("\nClose the dashboard first. Make a backup and upgrade now? [y/N] ").strip().lower() != "y":
        sys.exit("Cancelled. Nothing changed.")

    if not a.no_backup:
        backup = path.with_name(f"{path.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        dst = sqlite3.connect(backup)
        db.backup(dst)
        dst.close()
        print(f"Backup written: {backup}")

    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("BEGIN")
    db.execute("CREATE TABLE IF NOT EXISTS bpc_titles (framework TEXT NOT NULL, bpc_id TEXT NOT NULL, title TEXT, "
               "as_of_date TEXT, PRIMARY KEY (framework, bpc_id))")
    if "bpc_title" in oc:      # later rows overwrite earlier ones, so the newest title wins
        db.execute("INSERT OR REPLACE INTO bpc_titles SELECT i.framework, o.bpc_id, o.bpc_title, i.as_of_date "
                   "FROM obs o JOIN imports i ON i.id=o.import_id WHERE o.bpc_title IS NOT NULL AND o.bpc_title<>'' "
                   "ORDER BY i.as_of_date, i.id")
    print(f"Control titles stored: {db.execute('SELECT COUNT(*) FROM bpc_titles').fetchone()[0]}")
    db.execute("DROP TABLE IF EXISTS obs_new")
    db.execute("CREATE TABLE obs_new (id INTEGER PRIMARY KEY, import_id INTEGER NOT NULL REFERENCES imports(id) "
               "ON DELETE CASCADE, row_num INTEGER, line_id INTEGER, serial_key TEXT, device_group TEXT, bpc_id TEXT, "
               "control_groups TEXT, device_type TEXT, object_type TEXT, severity TEXT, location TEXT, rem_tail TEXT, "
               "object_id TEXT, status TEXT, user_excluded INTEGER, extras_json TEXT)")
    keep = "extras_json" if (a.keep_extras and "extras_json" in oc) else "NULL"
    db.execute(NEW_OBS.format(extras=keep))
    db.execute("DROP TABLE obs")
    db.execute("ALTER TABLE obs_new RENAME TO obs")
    db.execute("CREATE INDEX IF NOT EXISTS ix_obs_import ON obs(import_id)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_obs_line ON obs(line_id)")
    for table, col, typ in [("rules", "section", "TEXT"), ("rules", "old_value", "TEXT"), ("rules", "new_value", "TEXT"),
                            ("ended", "returned_import_id", "INTEGER"), ("imports", "content_hash", "TEXT")]:
        if col not in cols(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    db.executescript(core.SCHEMA)          # creates any table this version added (IF NOT EXISTS)
    db.execute("DELETE FROM view_cache")
    db.execute("DELETE FROM option_cache")
    db.execute(f"PRAGMA user_version={core.SCHEMA_VERSION}")
    db.commit()
    db.close()
    print("Layout upgraded. Re-matching every import with the current rules (this can take a minute)...")

    conn = core.connect(path)
    core.recompute_content_hashes(conn)
    for fw in before:
        core.rebuild(conn, fw)
        lines_after = conn.execute("SELECT COUNT(*) FROM lines WHERE framework=?", (fw,)).fetchone()[0]
        unres_after = conn.execute("SELECT COUNT(*) FROM ended WHERE framework=? AND reason='unresolved' "
                                   "AND returned_import_id IS NULL", (fw,)).fetchone()[0]
        lb, ub = before[fw]
        print(f"  {fw}: distinct lines {lb:,} -> {lines_after:,};  unexplained removals "
              f"{'?' if ub is None else format(ub, ',')} -> {unres_after:,}")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"\nDone. Database size: {mb(path):.1f} MB. You can start the dashboard again.")
    print("Lines that share serial + Bpc Id + Object Id + Location are now paired by remediation location, "
          "so some earlier pairings may differ.")


if __name__ == "__main__":
    main()
