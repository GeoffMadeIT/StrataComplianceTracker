# Version: 1.3.2
"""End-to-end check of the core library against the synthetic samples. Run: python test_core.py"""
import datetime as dt
import runpy
import tempfile
from pathlib import Path

from tracker import analysis, core

runpy.run_path("make_sample_data.py", run_name="__main__")

S = Path("samples")
FW = "NIST CSF 2.0"
conn = core.connect(Path(tempfile.mkdtemp()) / "t.db")
DATES = ["2026-08-13", "2026-09-10", "2026-10-01"]
A, B, C, D = "12345678901234", "23456789012345", "34567890123456", "45678901234567"

# Import out of order on purpose: the as-of date, not the import order, drives matching.
for d in [DATES[0], DATES[2], DATES[1]]:
    core.import_csv(conn, FW, dt.date.fromisoformat(d), (S / f"nist_csf_{d}.csv").read_bytes(), f"nist_csf_{d}.csv")
imps = core.list_imports(conn, FW)
assert [i["as_of_date"] for i in imps] == DATES
i1, i2, i3 = [i["id"] for i in imps]


def lines(imp_id):
    return {(r["serial_key"], r["bpc_id"]): r["line_id"] for r in conn.execute("SELECT * FROM obs WHERE import_id=?", (imp_id,))}


l1, l2, l3 = lines(i1), lines(i2), lines(i3)

# Device group appended to the serial must not break matching.
assert core.split_serial(f"{C} Branch-FW") == (C, "Branch-FW")
assert all(l1[(C, str(b))] == l2[(C, str(b))] == l3[(C, str(b))] for b in range(1, 36))

# A lines 5-8 vanish in import 2 and return in import 3: same line ids, resolved, not "new".
assert all(l1[(A, str(b))] == l3[(A, str(b))] for b in (5, 6, 7, 8))
assert not any(k == A and b in "5678" for (k, b) in l2)
assert not [u for u in core.unresolved(conn, FW, i2) if u["serial_key"] == A]
assert not [a for a in core.new_lines(conn, FW, i3, i2) if a["serial_key"] == A]
ch = analysis.changes(conn, FW)
assert ch.iloc[1]["returned"] == 4

# C 36-40 vanished for good: unresolved until retired.
assert {(u["serial_key"], u["bpc_id"]) for u in core.unresolved(conn, FW, i2)} == {(C, str(b)) for b in range(36, 41)}

# B disappears and D appears at import 3.
un3 = core.unresolved(conn, FW, i3)
assert sum(u["serial_key"] == B for u in un3) == 45
assert not any(l3[(D, str(b))] in l2.values() for b in range(1, 46))
sugg = core.suggest_serial_aliases(un3, core.new_lines(conn, FW, i3, i2))
assert sugg and sugg[0]["old_serial"] == B and sugg[0]["new_serial"] == D, sugg

core.apply_rules(conn, FW, [("serial_alias", DATES[2], "B replaced by D", {"old_serial": B, "new_serial": D})])
l2, l3 = lines(i2), lines(i3)
assert all(l2[(B, str(b))] == l3[(D, str(b))] for b in range(1, 46))
assert not [u for u in core.unresolved(conn, FW, i3) if u["serial_key"] == B]

core.apply_rules(conn, FW, [("retire_line", DATES[1], "", {"old_serial": C, "old_bpc_id": str(b), "old_object_id": f"obj-{b % 7}"})
                            for b in range(36, 41)])
assert not core.unresolved(conn, FW, i2)
assert conn.execute("SELECT COUNT(*) FROM ended WHERE reason='retired'").fetchone()[0] == 5

# Rebuild is deterministic and re-applies saved rules.
before = lines(i3)
core.rebuild(conn, FW)
assert lines(i3) == before

# Header history uses the dates you supplied for v1 / v2.
hh = analysis.header_history(conn, FW)
assert list(hh["format"]) == ["v1", "v1", "v2"]
assert "Product" in hh.iloc[2]["removed"] and "Device Type" in hh.iloc[2]["added"]
h2, _ = core.read_csv_bytes((S / "nist_csf_2026-10-01.csv").read_bytes())
assert analysis.schema_warning(h2, "2026-09-01") and not analysis.schema_warning(h2, "2026-10-01")

# Analysis
import itertools
df = analysis.load_df(conn, FW)
P = analysis.build_plot_data
r = P(df, {})
t = r["table"]
print(t.round({"pct_complete": 1}).to_string(index=False))
assert list(t["date_label"]) == DATES and t["pct_complete"].is_monotonic_increasing
assert t["excluded"].gt(0).all()
# user-excluded lines are left out of the % by default, and included on request
inc = P(df, {"excluded": "include"})["table"]
assert (inc["lines"] == t["lines"]).all() and (inc["passed"] >= t["passed"]).all()
assert (inc["pct_complete"] != t["pct_complete"]).any()
# cohort: same lines every date
co = P(df, {"cohort": True, "measure": "lines"})["table"]
assert co["lines"].nunique() == 1
# status buckets add up to all lines; absolute measures
st = P(df, {"measure": "status", "stack": "status"})
tot = st["plot"].groupby("as_of_date")["value"].sum().tolist()
assert tot == df.groupby("as_of_date").size().tolist(), tot
assert {"pass", "fail", "exception", "excluded"} <= set(st["plot"]["series"])
# stacking by group / both / 100%, facets
g = P(df, {"measure": "fail", "group": "severity", "stack": "group", "norm100": True})
assert g["cfg"]["chart"] == "Area" and g["plot"].groupby(["as_of_date"]).size().nunique() == 1   # zero-filled grid
both = P(df, {"measure": "status", "group": "severity", "stack": "both"})
assert any(" · " in x for x in both["plot"]["series"])
fac = P(df, {"measure": "status", "group": "severity", "stack": "status"})
assert fac["facet"] and "group" in fac["plot"] and set(fac["plot"]["series"]) <= {"pass", "fail", "exception", "excluded", "other"}
# invalid combinations adjust themselves with a note instead of failing
bad = P(df, {"measure": "pct", "stack": "both", "group": None})
assert bad["notes"] and bad["cfg"]["stack"] == "off"
# control-group split (a line counts once in every group it belongs to) + filter on a control group
cgp = P(df[df.as_of_date >= DATES[2]], {"group": "control_group", "measure": "lines"})
assert "NIST CSF" in set(cgp["plot"]["series"])
fl = P(df, {"filters": {"control_group": ["NIST CSF"]}, "measure": "lines"})["table"]
assert fl["date_label"].tolist() == [DATES[2]] and fl["lines"].tolist() == [130]   # only the v2 export has control groups
# first vs latest
fvl = P(df, {"group": "severity", "chart": "Bar: first vs latest"})
assert set(fvl["plot"]["series"]) == {DATES[0], DATES[2]} and "x" in fvl["plot"]
# every combination runs
for meas, grp, stk, chart in itertools.product(analysis.MEASURES, [None, "severity", "device_type", "control_group", "serial_key"],
                                               ["off", "group", "status", "both"], ["Line", "Bar", "Area"]):
    r = P(df, {"measure": meas, "group": grp, "stack": stk, "chart": chart})
    assert len(r["plot"]) > 0, (meas, grp, stk, chart)
# group limit
lim = P(df, {"measure": "lines", "group": "bpc_id", "max_groups": 5})
assert lim["plot"]["series"].nunique() == 6 and "(other)" in set(lim["plot"]["series"])
print(analysis.changes(conn, FW).to_string(index=False))
print("ALL CHECKS PASSED")
