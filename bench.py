# Version: 1.3.2
"""Timing benchmark on synthetic data. Run: python bench.py [imports] [rows_per_import]
Defaults to 30 imports x 5000 rows (~150k rows)."""
import csv
import datetime as dt
import io
import random
import sys
import tempfile
import time
from pathlib import Path

from tracker import analysis, core, views

N_IMP = int(sys.argv[1]) if len(sys.argv) > 1 else 30
ROWS = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
HEAD = ["BPC Title", "Device Serial", "Bpc Id", "Control Groups", "Device Type", "Object Type", "Severity", "Location",
        "Remediation Location", "Object Id", "Bpc Status", "User Excluded"]
SEV = ["Critical", "High", "Medium", "Low", "Warning"]
rnd = random.Random(7)
n_dev = 10
per_dev = ROWS // n_dev
serials = [f"{1000000000000 + i * 7919:014d}" for i in range(n_dev)]
state = {(d, b): rnd.random() for d in range(n_dev) for b in range(per_dev)}


def make_csv(k):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(HEAD)
    for d in range(n_dev):
        for b in range(per_dev):
            if rnd.random() < 0.01:           # ~1% of lines drop out of any given export
                continue
            state[(d, b)] = min(1.0, state[(d, b)] + rnd.random() * 0.03)
            st = "pass" if rnd.random() < state[(d, b)] else ("exception" if rnd.random() < 0.05 else "fail")
            w.writerow([f"Control {b}", serials[d], b, f"NIST CSF; CIS v8; 800-53 AC-{b % 9}", "ngfw", f"type_{b % 12}",
                        SEV[b % 5], "vsys1", f"tsgid:1/Device:{serials[d]}/Device Group:g{b % 4}/", f"obj-{b % 31}", st,
                        "True" if b % 53 == 0 else ""])
    return out.getvalue().encode()


def t(label, fn):
    t0 = time.perf_counter()
    r = fn()
    print(f"{label:<52}{time.perf_counter() - t0:8.2f}s")
    return r


conn = core.connect(Path(tempfile.mkdtemp()) / "bench.db")
start = dt.date(2026, 1, 1)
first = last = None
for k in range(N_IMP):
    data = make_csv(k)
    dur = time.perf_counter()
    core.import_csv(conn, "BENCH", start + dt.timedelta(days=7 * k), data, f"b{k}.csv")
    dur = time.perf_counter() - dur
    first = first or dur
    last = dur
print(f"import #1 {first:.2f}s ... import #{N_IMP} {last:.2f}s (each includes matching against the previous export)")
total = conn.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
print(f"rows stored: {total:,}")
ver = views.data_version(conn, "BENCH")
df = t("load_df (once per data change, then cached in memory)", lambda: analysis.load_df(conn, "BENCH"))
t("filter_options (once, then stored)", lambda: analysis.filter_options(df))
P = analysis.build_plot_data
t("% complete, no grouping", lambda: P(df, {}))
t("% complete by severity", lambda: P(df, {"group": "severity"}))
t("% complete by control group (split on ;)", lambda: P(df, {"group": "control_group"}))
t("% complete by bpc_id (limited to 25 groups)", lambda: P(df, {"group": "bpc_id"}))
t("status breakdown stacked, 100%, by severity", lambda: P(df, {"measure": "status", "group": "severity", "stack": "both", "norm100": True}))
t("+ filter on a control group", lambda: P(df, {"filters": {"control_group": ["CIS v8"]}, "group": "severity"}))
t("+ only lines present in every import", lambda: P(df, {"cohort": True, "group": "severity"}))
vid = views.save_view(conn, "BENCH", "bench", {"group": "severity"})
cfg, _ = analysis.normalize_cfg({"group": "severity"})
res = P(df, cfg)
key = views.cfg_key(cfg)
views.put_cached(conn, vid, key, ver, res, 0.0)
t("saved view served from its stored result", lambda: views.get_cached(conn, vid, key, ver))
t("rebuild everything after a rule change", lambda: core.rebuild(conn, "BENCH"))
