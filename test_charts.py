# Version: 1.3.2
"""100% stacked behaviour, stack order and tooltips data. Run: python test_charts.py (no plotly needed)."""
import numpy as np
import pandas as pd

from tracker import analysis, charts, version, views


def frame():
    rows = []
    for d, scale in [("2026-01-01", 1), ("2026-02-01", 2)]:        # different totals per date
        for sev in ["Low", "Critical", "High", "Medium", "Warning"]:
            for st, n in [("pass", 3), ("fail", 2), ("exception", 1)]:
                for _ in range(n * scale):
                    rows.append(dict(as_of_date=pd.Timestamp(d), line_id=len(rows), severity=sev, status=st, ex=False,
                                     control_groups="a", serial_key="x"))
    df = pd.DataFrame(rows)
    for c in ["severity", "status", "control_groups", "serial_key"]:
        df[c] = df[c].astype("category")
    return df


df = frame()

# ticking 100% on the defaults (measure % complete, stack off) sets up a stacked status chart and says so
r = analysis.build_plot_data(df, dict(norm100=True))
assert r["cfg"]["measure"] == "status" and r["cfg"]["stack"] == "status" and r["cfg"]["chart"] == "Area", r["cfg"]
assert any("100% stacked" in n for n in r["notes"]), r["notes"]
assert charts.series_order(r["plot"]) == ["fail", "exception", "pass"]
r = analysis.build_plot_data(df, dict(norm100=True, group="severity"))
assert r["cfg"]["stack"] == "both"
p = r["plot"]
assert np.allclose(p.groupby("as_of_date")["share"].sum(), 100)
# tooltip data: the count is the absolute number of lines, the share is its % of that date
x = p[(p.series == "Critical · fail") & (p.as_of_date == "2026-02-01")].iloc[0]
assert x["value"] == 4 and abs(x["share"] - 4 / (6 * 5 * 2) * 100) < 1e-9, x
# fails at the bottom (Critical first), then exceptions, then passes; 'Warning' after 'Low'
order = charts.series_order(p)
assert order[:5] == [f"{s} · fail" for s in ["Critical", "High", "Medium", "Low", "Warning"]], order
assert order[5:10] == [f"{s} · exception" for s in ["Critical", "High", "Medium", "Low", "Warning"]]
assert order[10:] == [f"{s} · pass" for s in ["Critical", "High", "Medium", "Low", "Warning"]]
cm = charts.status_colors(order, "severity")
assert cm and cm["Critical · fail"] != cm["Low · fail"] and cm["Critical · fail"] != cm["Critical · pass"]
assert charts.status_colors(order, "device_type") is None           # shades only mean something for severity

# 100% unticked: no share column, nothing changes for ordinary charts
r = analysis.build_plot_data(df, dict(group="severity"))
assert "share" not in r["plot"] and r["cfg"]["stack"] == "off"

# a stored view's cached result keeps the new columns
import json
packed = views._unpack(views._pack(analysis.build_plot_data(df, dict(norm100=True))["plot"]))
assert {"share", "s_ord"} <= set(packed.columns) and packed["share"].dtype.kind == "f"

# versions: every project file carries the project version
bad = [(f, v) for f, v in version.file_versions() if v != version.VERSION]
assert not bad, bad
print("CHART CHECKS PASSED")
