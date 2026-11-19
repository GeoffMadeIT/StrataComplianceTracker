# Version: 1.3.2
"""remediate_db.py upgrades an old database: backup, titles stored once, wide columns dropped, hashes recomputed,
saved decisions preserved, everything re-matched. Run: python test_remediate.py"""
import datetime as dt
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tracker import core

runpy.run_path("make_sample_data.py", run_name="__main__")
S, FW = Path("samples"), "NIST CSF 2.0"
B, D = "23456789012345", "45678901234567"
DATES = ["2026-08-13", "2026-09-10", "2026-10-01"]
work = Path(tempfile.mkdtemp())


def make_old_db(path):
    """Build a database shaped like the previous version's: wide per-row columns, no titles table."""
    c = core.connect(path)
    for d in DATES:
        core.import_csv(c, FW, dt.date.fromisoformat(d), str(S / f"nist_csf_{d}.csv"))
    c.close()
    db = sqlite3.connect(path)
    for col in ("bpc_title", "serial_raw", "remediation_location"):
        db.execute(f"ALTER TABLE obs ADD COLUMN {col} TEXT")
    db.execute("UPDATE obs SET bpc_title='Control '||bpc_id||' '||hex(randomblob(800)), serial_raw=serial_key, "
               "remediation_location='tsgid:1/Device:'||serial_key||'/'||rem_tail")
    db.execute("UPDATE obs SET extras_json='{\"Wide Col\": \"'||hex(randomblob(400))||'\"}' WHERE row_num % 3 = 0")
    db.execute("DROP TABLE bpc_titles")
    # a decision saved by the old version: device B was replaced by D
    if sqlite3.sqlite_version_info >= (3, 35, 0):
        for col in ("section", "old_value", "new_value"):
            db.execute(f"ALTER TABLE rules DROP COLUMN {col}")
    db.execute("INSERT INTO rules(framework,kind,old_serial,new_serial,effective_date,note,created_at) "
               "VALUES (?,?,?,?,?,?,?)", (FW, "serial_alias", B, D, "2026-10-01", "old", "2026-10-02"))
    db.execute("UPDATE imports SET content_hash=NULL")
    db.execute("PRAGMA user_version=0")
    db.commit()
    db.close()


def run(*args):
    return subprocess.run([sys.executable, "remediate_db.py", *args], capture_output=True, text=True,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


old = work / "old.db"
make_old_db(old)
size_before = old.stat().st_size

try:
    core.connect(old)
    raise SystemExit("an old database should be refused")
except core.SchemaOutdated as e:
    assert "remediate_db.py" in str(e)

# dry run changes nothing
r = run("--db", str(old), "--dry-run")
assert r.returncode == 0 and "Dry run" in r.stdout and "will be dropped" in r.stdout, r.stdout + r.stderr
assert sqlite3.connect(old).execute("PRAGMA user_version").fetchone()[0] == 0 and not list(work.glob("*.bak-*"))

# the real thing
r = run("--db", str(old), "--yes")
assert r.returncode == 0, r.stdout + r.stderr
print(r.stdout)
assert len(list(work.glob("old.db.bak-*"))) == 1, "backup made"
c = core.connect(old)
cols = [x[1] for x in c.execute("PRAGMA table_info(obs)")]
assert not {"bpc_title", "serial_raw", "remediation_location"} & set(cols)
assert c.execute("SELECT COUNT(*) FROM obs WHERE extras_json IS NOT NULL").fetchone()[0] == 0
titles = core.titles(c, FW)
assert len(titles) == 45 and titles["7"].startswith("Control 7")
assert all(i["content_hash"] for i in core.list_imports(c, FW))
assert old.stat().st_size < size_before * 0.8, (old.stat().st_size, size_before)

# saved decision survived and was applied by the re-match: B's lines continue as D's
imps = core.list_imports(c, FW)
lines = lambda imp, s: {r["bpc_id"]: r["line_id"] for r in c.execute("SELECT * FROM obs WHERE import_id=? AND serial_key=?", (imp["id"], s))}
assert lines(imps[1], B) == lines(imps[2], D) and len(lines(imps[2], D)) == 45
assert not [u for u in core.unresolved(c, FW, imps[2]["id"]) if u["serial_key"] == B]

# a second run says it is up to date; re-importing identical data is still caught after the hash recompute
r2 = run("--db", str(old), "--yes")
assert "already up to date" in (r2.stdout + r2.stderr)
try:
    core.import_csv(c, FW, dt.date(2026, 11, 1), str(S / "nist_csf_2026-10-01.csv")); raise SystemExit("no error")
except core.DuplicateFile:
    pass

# --keep-extras keeps them
old2 = work / "old2.db"
make_old_db(old2)
assert run("--db", str(old2), "--yes", "--keep-extras").returncode == 0
assert core.connect(old2).execute("SELECT COUNT(*) FROM obs WHERE extras_json IS NOT NULL").fetchone()[0] > 0
print("REMEDIATE CHECKS PASSED")
