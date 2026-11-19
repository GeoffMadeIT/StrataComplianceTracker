# Version: 1.3.2
"""Wide-file benchmark: ~62k rows with a very wide BPC Title and a wide unrecognized column, imported from a path.
Run: python bench_wide.py [rows] [bytes_per_wide_cell]   (reports file size, import time, peak memory, DB size)"""
import csv, datetime as dt, resource, sys, tempfile, time
from pathlib import Path
from tracker import core

ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 62000
WIDE = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
HEAD = ["BPC Title", "Device Serial", "Bpc Id", "Control Groups", "Device Type", "Object Type", "Severity", "Location",
        "Remediation Location", "Object Id", "Bpc Status", "User Excluded", "Big Description"]
tmp = Path(tempfile.mkdtemp())
n_dev, per_dev = 20, ROWS // 20
serials = [f"{1000000000000 + i * 7919:014d}" for i in range(n_dev)]
titles = {b: f"Control {b}: " + ("lorem ipsum dolor " * (WIDE // 18)) for b in range(per_dev)}


def make(path, flip):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(HEAD)
        for d in range(n_dev):
            for b in range(per_dev):
                st = "pass" if (b + d + flip) % 3 else "fail"
                w.writerow([titles[b], serials[d], b, "NIST CSF", "ngfw", f"t{b % 12}", "High", "vsys1",
                            f"tsgid:1/Device:{serials[d]}/Device Group:g{b % 4}/", f"obj-{b % 31}", st, "",
                            "x" * WIDE])


conn = core.connect(tmp / "w.db")
for k, d in enumerate([dt.date(2026, 9, 1), dt.date(2026, 10, 1)]):
    p = tmp / f"wide_{k}.csv"; make(p, k)
    t0 = time.perf_counter()
    core.import_csv(conn, "WIDE", d, str(p), p.name)
    print(f"import {k + 1}: file {p.stat().st_size / 1e6:7.0f} MB, {ROWS:,} rows -> {time.perf_counter() - t0:6.1f}s, "
          f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:6.0f} MB")
t0 = time.perf_counter(); core.rebuild(conn, "WIDE"); print(f"full rebuild: {time.perf_counter() - t0:.1f}s")
print(f"database: {(tmp / 'w.db').stat().st_size / 1e6:.1f} MB")
