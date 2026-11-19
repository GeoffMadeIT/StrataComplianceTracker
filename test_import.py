# Version: 1.3.2
"""Import safety checks: duplicate data, same-date conflicts, structure messages, incremental == full rebuild.
Run: python test_import.py"""
import collections
import datetime as dt
import runpy
import tempfile
from pathlib import Path

from tracker import core

runpy.run_path("make_sample_data.py", run_name="__main__")
S, FW = Path("samples"), "X"
conn = core.connect(Path(tempfile.mkdtemp()) / "t.db")
f = [(S / f"nist_csf_{d}.csv").read_bytes() for d in ("2026-08-13", "2026-09-10", "2026-10-01")]
D = [dt.date(2026, 8, 13), dt.date(2026, 9, 10), dt.date(2026, 10, 1)]

pf = core.preflight(conn, FW, D[0], f[0])
assert pf["structure"]["kind"] == "first" and not pf["file_dup"]
core.import_csv(conn, FW, D[0], f[0], "a.csv")

# identical file -> its own error
try:
    core.import_csv(conn, FW, D[0], f[0], "a.csv"); raise SystemExit("no error")
except core.DuplicateFile as e:
    assert e.existing[0]["match"] == "file" and "same file" in str(e)
# re-saved copy (different bytes, same data) -> identified as identical DATA
try:
    core.import_csv(conn, FW, D[1], f[0].replace(b"\n", b"\r\n"), "copy.csv"); raise SystemExit("no error")
except core.DuplicateFile as e:
    assert e.existing[0]["match"] == "content" and "identical data" in str(e)
# different data, same as-of date -> must choose
try:
    core.import_csv(conn, FW, D[0], f[1], "b.csv"); raise SystemExit("no error")
except core.SameDateConflict:
    pass
core.import_csv(conn, FW, D[0], f[1], "b.csv", same_date_action="replace")
assert [i["filename"] for i in core.list_imports(conn, FW)] == ["b.csv"]
# header structure is reported separately from data duplication
s = core.preflight(conn, FW, D[2], f[2])["structure"]
assert s["kind"] == "changed" and "Device Type" in s["added"] and "Product" in s["removed"]
assert core.preflight(conn, FW, D[2], f[1])["structure"]["kind"] == "identical"
# "keep both" leaves two imports on the same date
core.import_csv(conn, FW, D[0], f[0], "a.csv", same_date_action="keep_both")
assert len(core.list_imports(conn, FW)) == 2

# incremental matching == full rebuild
c2 = core.connect(Path(tempfile.mkdtemp()) / "t2.db")
for d, b in zip(D, f):
    core.import_csv(c2, FW, d, b, "x")
shape = lambda c: collections.Counter(n for _, n in c.execute("SELECT line_id, COUNT(*) FROM obs GROUP BY line_id"))
inc = shape(c2)
core.rebuild(c2, FW)
assert inc == shape(c2)

# --- files can be imported straight from a path on disk, same result as bytes
cp = core.connect(Path(tempfile.mkdtemp()) / "p.db")
core.import_csv(cp, FW, D[0], str(S / "nist_csf_2026-08-13.csv"))
assert cp.execute("SELECT filename, row_count FROM imports").fetchone()[:] == ("nist_csf_2026-08-13.csv", 120)
try:
    core.import_csv(cp, FW, D[1], str(S / "nist_csf_2026-08-13.csv")); raise SystemExit("no error")
except core.DuplicateFile:
    pass

# --- unrecognized columns: ignored by default (and reported with their size); kept only on request, then remembered
import csv, io
def with_extra(rows=40):
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["Bpc Id", "Device Serial", "Object Id", "Location", "Bpc Status", "Wide Description", "Owner"])
    for k in range(rows):
        w.writerow([k, "12345678901234", "o", "v1", "pass" if k % 2 else "fail", "d" * 3000, f"team{k % 3}"])
    return out.getvalue().encode()
ce = core.connect(Path(tempfile.mkdtemp()) / "e.db")
pf = core.preflight(ce, FW, D[0], with_extra())
assert set(pf["undecided"]) == {"Wide Description", "Owner"} and pf["avg_len"]["Wide Description"] == 3000
core.import_csv(ce, FW, D[0], with_extra())
assert ce.execute("SELECT COUNT(*) FROM obs WHERE extras_json IS NOT NULL").fetchone()[0] == 0     # nothing wide stored
ce.execute("DELETE FROM imports"); ce.commit()
core.import_csv(ce, FW, D[0], with_extra(), extra_map={"Owner": "_keep", "Wide Description": "_ignore"})
assert ce.execute("SELECT extras_json FROM obs WHERE row_num=2").fetchone()[0] == '{"Owner": "team1"}'
assert core.plan_headers(ce, FW, ["Owner", "Wide Description"])[1:3] == (["Owner"], ["Wide Description"])   # remembered
print("IMPORT CHECKS PASSED")
