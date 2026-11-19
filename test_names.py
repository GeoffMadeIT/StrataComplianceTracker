# Version: 1.3.2
"""Device-name file and name-aware suggestions. Run: python test_names.py"""
import os
import tempfile
from pathlib import Path

from tracker import core, names

A, B, D, E = "12345678901234", "23456789012345", "45678901234567", "56789012345678"
d = Path(tempfile.mkdtemp())
f = d / "names.csv"
f.write_text("﻿Serial Number,Device Name,Notes\n"
             f"{A},Branch-FW,main site\n"
             f"{B} Branch-DG,Branch-FW,old unit\n"          # serial carrying a device group; same name as A
             f"{D},Branch-FW,\n"
             "1.2345E+13,Excel-mangled,\n"
             f"{E},,no name\n"
             f"{D},HQ-FW,duplicate row\n", encoding="utf-8")
nm = names.get(f)
assert nm[A] == "Branch-FW" and nm[B] == "Branch-FW" and nm[D] == "HQ-FW" and E not in nm
w = " ".join(nm.warnings)
assert "scientific notation" in w and "more than once" in w and "needs both" in w, nm.warnings
assert names.label(A, nm) == f"{A} (Branch-FW)" and names.label("999", nm) == "999"
assert names.label_series(f"{A} · fail", nm) == f"{A} (Branch-FW) · fail"
assert names.label_series("(other)", nm) == "(other)"
assert names.template_for([A, E, "777"], nm).splitlines() == ["serial,name,notes", f"{E},", "777,"]

# reloads when the file changes
f.write_text("serial,name\n" + f"{A},Renamed\n", encoding="utf-8")
st = f.stat(); os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
assert names.get(f)[A] == "Renamed"

# a missing or broken file never breaks anything; a missing file is created as an empty template
newf = d / "sub" / "n.csv"
assert names.get(newf) == {} and newf.read_text() == "serial,name,notes\n"
bad = d / "bad.csv"; bad.write_text("foo,bar\n1,2\n")
assert names.get(bad) == {} and names.get(bad).warnings

# suggestions: share of lines in common, both ways; a name only raises rank; small devices need a name or ~all lines
def line(serial, bpc, obj="o"): return {"serial_key": serial, "bpc_id": str(bpc), "object_id": obj, "location": "vsys1"}
gone = [line(A, b) for b in range(10)]
added = [line(B, b) for b in range(10)] + [line(D, b) for b in range(3)]
# B shares all 10 lines; D shares 3 of 10 old lines but has the same name. 100% overlap beats a name with 30%.
s = core.suggest_serial_aliases(gone, added, names={A: "Branch-FW", D: "branch-fw", B: "Other"})
assert s[0]["new_serial"] == B and not s[0]["same_name"] and s[0]["pct_old"] == 1.0
# same name AND strong overlap wins over strong overlap alone
s = core.suggest_serial_aliases(gone, [line(B, b) for b in range(10)] + [line(D, b) for b in range(10)],
                                names={A: "Branch-FW", D: "branch-fw"})
assert s[0]["new_serial"] == D and s[0]["same_name"] and s[0]["basis"] == "name + lines"
# a name match with weak overlap is offered only when nothing else qualifies, and is labelled
s = core.suggest_serial_aliases(gone, [line(D, b) for b in range(3)], names={A: "Same", D: "Same"})
assert s[0]["new_serial"] == D and s[0]["basis"] == "name only"
assert core.suggest_serial_aliases(gone, [line(D, b) for b in range(3)]) == []             # no name, 30%: nothing
assert core.suggest_serial_aliases(gone, [line(D, b, "other") for b in range(10)], names={}) == []   # no shared lines
# small devices (< 200 lines) need a name or ~complete overlap; 80% / 80% is not enough without a name
g8 = [line(A, b) for b in range(10)]
a8 = [line(B, b) for b in range(8)] + [line(B, 100), line(B, 101)]            # 8 of 10 shared, 8 of 10 on the new device
assert core.suggest_serial_aliases(g8, a8) == []
assert core.suggest_serial_aliases(g8, a8, names={A: "x", B: "X"})[0]["new_serial"] == B
# large devices: the 30 of 7431 and 183 of 1455 situations you saw are far below the thresholds
big_old = [line(A, b) for b in range(7431)]
big_new = [line(B, b) for b in range(30)] + [line(B, 10_000 + b) for b in range(7431)]
assert core.suggest_serial_aliases(big_old, big_new) == []
mid_old = [line(A, b) for b in range(1455)]
mid_new = [line(B, b) for b in range(183)] + [line(B, 10_000 + b) for b in range(1272)]
assert core.suggest_serial_aliases(mid_old, mid_new) == []
# ...but a real replacement (90% of old lines on a bigger new device) is suggested
real_new = [line(B, b) for b in range(1300)] + [line(B, 10_000 + b) for b in range(200)]
r = core.suggest_serial_aliases(mid_old, real_new)
assert r and r[0]["new_serial"] == B and round(r[0]["pct_old"], 2) == 0.89
# a header-only file explains itself; a second copy of the file nearby is reported
sub = d / "copy"; sub.mkdir()
(sub / "device_names.csv").write_text("serial,name,notes\n")
(d / "device_names.csv").write_text("serial,name,notes\n" + A + ",Edge\n")
n0 = names.get(sub / "device_names.csv")
assert any("header row but no names" in w for w in n0.warnings), n0.warnings
assert [Path(o).name for o in n0.others] == ["device_names.csv"] and n0.others[0] != str(n0.path)
assert names.get(d / "device_names.csv")[A] == "Edge" and names.get(d / "device_names.csv").rows_read == 1
print("NAMES CHECKS PASSED")
