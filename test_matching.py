# Version: 1.3.2
"""Matching rules: key = serial + Bpc Id + Object Id + Location, remediation location as the tie-breaker.
Run: python test_matching.py"""
import csv
import datetime as dt
import io
import tempfile
from pathlib import Path

from tracker import core

A, B = "12345678901234", "23456789012345"
FW = "T"
HEAD = ["Bpc Id", "Device Serial", "Object Id", "Location", "Remediation Location", "Bpc Status", "BPC Title"]


def export(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(HEAD)
    for serial, bpc, obj, loc, rem, status in rows:
        w.writerow([bpc, serial, obj, loc, f"tsgid:1/Device:{serial}/{rem}/", status, f"Title of control {bpc} " + "x" * 300])
    return out.getvalue().encode()


def fresh():
    return core.connect(Path(tempfile.mkdtemp()) / "t.db")


def imp(conn, day, rows):
    return core.import_csv(conn, FW, dt.date(2026, 10, day), export(rows), f"d{day}.csv")


def lines(conn, imp_id):
    return [(r["serial_key"], r["bpc_id"], r["rem_tail"], r["line_id"]) for r in
            conn.execute("SELECT * FROM obs WHERE import_id=? ORDER BY row_num", (imp_id,))]


# --- 1) unique key: a changed remediation location can't veto the match
c = fresh()
i1 = imp(c, 1, [(A, 1, "o", "v1", "Device Group:old/Object Type:x", "fail")])
i2 = imp(c, 2, [(A, 1, "o", "v1", "Device Group:NEW/Object Type:x", "pass")])
assert lines(c, i1)[0][3] == lines(c, i2)[0][3]
assert not core.unresolved(c, FW, i2)

# --- 2) location is part of the key
c = fresh()
i1 = imp(c, 1, [(A, 1, "o", "v1", "g:1", "fail"), (A, 1, "o", "v2", "g:1", "pass")])
i2 = imp(c, 2, [(A, 1, "o", "v2", "g:1", "pass"), (A, 1, "o", "v1", "g:1", "pass")])      # order swapped
l1, l2 = lines(c, i1), lines(c, i2)
assert l1[0][3] == l2[1][3] and l1[1][3] == l2[0][3]

# --- 3) duplicates: remediation is the tie-breaker; leftovers are NOT paired by order
c = fresh()
dup = lambda t1, t2, st="fail": [(A, 1, "o", "v1", t1, st), (A, 1, "o", "v1", t2, st)]
i1 = imp(c, 1, dup("Device Group:a", "Device Group:b"))
i2 = imp(c, 2, dup("Device Group:b", "Device Group:a", "pass"))   # same two lines, order swapped, now passing
l1, l2 = lines(c, i1), lines(c, i2)
assert l1[0][3] == l2[1][3] and l1[1][3] == l2[0][3]
i3 = imp(c, 3, dup("Device Group:b", "Device Group:RENAMED", "exception"))   # one line's remediation changed
l3 = lines(c, i3)
assert l3[0][3] == l2[0][3]                                       # exact match still pairs
assert l3[1][3] not in {x[3] for x in l2}                          # the changed one is a NEW line...
assert [u["rem_tail"] for u in core.unresolved(c, FW, i3)] == ["device group:a"]      # ...and the old one removed

# --- 4) rename detection needs the same change on several lines AND several devices
def dups_for(serial, dg):
    return [(serial, 1, "o", "v1", f"Device Group:{dg}/Object Type:x", "fail"),
            (serial, 1, "o", "v1", f"Device Group:{dg}/Object Type:y", "fail")]

c = fresh()
i1 = imp(c, 1, dups_for(A, "old-dg") + dups_for(B, "old-dg"))
i2 = imp(c, 2, dups_for(A, "new-dg") + dups_for(B, "new-dg"))
assert len(core.unresolved(c, FW, i2)) == 4
s = core.suggest_rem_renames(c, FW, i2)
assert s == [{"section": "device group", "old": "old-dg", "new": "new-dg", "lines": 4, "devices": 2}], s
core.apply_rules(c, FW, [("rem_rename", "2026-10-02", "accepted", {"section": "device group", "old_value": "old-dg", "new_value": "new-dg"})])
assert not core.unresolved(c, FW, i2)
assert {x[3] for x in lines(c, i1)} == {x[3] for x in lines(c, i2)}          # all four lines carried over
core.rebuild(c, FW)                                                             # rules survive a rebuild
assert not core.unresolved(c, FW, i2)

# only one device -> not enough evidence; a lone change stays "removed and added"
c = fresh()
i1 = imp(c, 1, dups_for(A, "old-dg"))
i2 = imp(c, 2, dups_for(A, "new-dg"))
assert core.suggest_rem_renames(c, FW, i2) == [] and len(core.unresolved(c, FW, i2)) == 2
# one line on each of two devices -> only 2 lines... allowed (>=2 lines, >=2 devices)
c = fresh()
i1 = imp(c, 1, [(A, 1, "o", "v1", "Device Group:old/Object Type:x", "fail"), (A, 1, "o", "v1", "Device Group:old/Object Type:y", "fail"),
                (B, 1, "o", "v1", "Device Group:old/Object Type:x", "fail"), (B, 1, "o", "v1", "Device Group:old/Object Type:y", "fail")])
i2 = imp(c, 2, [(A, 1, "o", "v1", "Device Group:new/Object Type:x", "fail"), (A, 1, "o", "v1", "Device Group:new/Object Type:y", "fail"),
                (B, 1, "o", "v1", "Device Group:old/Object Type:x", "fail"), (B, 1, "o", "v1", "Device Group:old/Object Type:y", "fail")])
assert core.suggest_rem_renames(c, FW, i2) == []                  # only device A changed
# adding or removing a whole section is not a rename
c = fresh()
i1 = imp(c, 1, [(s_, 1, "o", "v1", f"Device Group:g/Object Type:{t}", "fail") for s_ in (A, B) for t in "xy"])
i2 = imp(c, 2, [(s_, 1, "o", "v1", f"Device Group:g/Object Type:{t}/Extra:1", "fail") for s_ in (A, B) for t in "xy"])
assert core.suggest_rem_renames(c, FW, i2) == []

# --- 5) titles are stored once per control, not on every row
c = fresh()
imp(c, 1, [(A, b, "o", "v1", "g:1", "pass") for b in range(1, 6)] + [(B, b, "o", "v1", "g:1", "pass") for b in range(1, 6)])
assert len(core.titles(c, FW)) == 5 and core.titles(c, FW)["3"].startswith("Title of control 3")
assert "bpc_title" not in [r[1] for r in c.execute("PRAGMA table_info(obs)")]
print("MATCHING CHECKS PASSED")
