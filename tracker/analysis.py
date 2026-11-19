# Version: 1.3.2
"""Analysis: lean cached-friendly loading, one aggregation pass, and the chart data model.

Speed notes
- Text columns are loaded as pandas categoricals (compact, fast group-by / isin).
- Filtering is boolean masks over category codes; the control-group (semicolon list) filter is evaluated on the
  few unique strings, then broadcast to rows, instead of per row.
- Everything is derived from ONE group-by on (date, group, status, excluded) -> a small count table. Percentages,
  absolute values, stacks and the "control group" split all come from that small table, not from the raw rows.
"""
import json

import numpy as np
import pandas as pd

from . import core
from .log import log, timed

GROUP_FIELDS = {
    "Severity": "severity", "Device type": "device_type", "Object type": "object_type",
    "Device serial": "serial_key", "Device group": "device_group", "Control (BPC) id": "bpc_id",
    "Control title": "bpc_title", "Control group (split on ;)": "control_group", "Location": "location",
}
CAT_COLS = ["serial_key", "device_group", "bpc_id", "bpc_title", "control_groups", "device_type", "object_type",
            "severity", "location", "status"]
BUCKETS = ["pass", "exception", "fail", "other", "excluded"]
STACK_ORDER = ["fail", "exception", "pass", "other", "excluded"]      # bottom -> top in a stacked status chart
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "warning": 4, "info": 5, "informational": 5}
MEASURES = {"pct": "% complete", "lines": "Lines (total)", "pass": "Pass", "fail": "Fail",
            "exception": "Exception", "excluded": "Excluded (flagged)", "status": "Status breakdown"}
DEFAULT_CFG = dict(group=None, measure="pct", chart="Line", stack="off", norm100=False, facet=False,
                   exceptions="exclude", excluded="exclude", cohort=False, filters={}, dates=[], max_groups=25)

_SQL = ("SELECT o.import_id, o.line_id, o.serial_key, o.device_group, o.bpc_id, o.control_groups, "
        "o.device_type, o.object_type, o.severity, o.location, o.status, o.user_excluded, o.extras_json, "
        "i.as_of_date FROM obs o JOIN imports i ON i.id=o.import_id WHERE i.framework=? "
        "ORDER BY i.as_of_date, i.id, o.row_num")


# ----------------------------------------------------------------------------- loading
def load_df(conn, framework):
    with timed("load_df", fw=framework):
        df = pd.read_sql_query(_SQL, conn, params=(framework,))
        if df.empty:
            return df
        df["as_of_date"] = pd.to_datetime(df["as_of_date"])
        df["bpc_title"] = df["bpc_id"].map(core.titles(conn, framework)).fillna("")   # titles are stored once per control
        df["ex"] = df["user_excluded"].fillna(0).eq(1)
        df["control_groups"] = df["control_groups"].fillna("")
        for c in CAT_COLS:
            df[c] = df[c].fillna("").astype(str).astype("category")
        df["line_id"] = df["line_id"].astype("int64")
        if df["extras_json"].notna().any():          # unmapped CSV columns -> extra categorical fields
            parsed = {u: json.loads(u) for u in df["extras_json"].dropna().unique()}
            for k in sorted({k for d in parsed.values() for k in d}):
                df[f"extra: {k}"] = df["extras_json"].map(
                    lambda u, k=k: str(parsed[u].get(k, "")) if isinstance(u, str) else "").astype("category")
        df = df.drop(columns=["extras_json", "user_excluded"])
        log.info("loaded %d rows, %d imports, %.1f MB in memory", len(df), df["import_id"].nunique(),
                 df.memory_usage(deep=True).sum() / 1e6)
    return df


def available_group_fields(df):
    out = {}
    for label, col in GROUP_FIELDS.items():
        src = "control_groups" if col == "control_group" else col
        if src in df and (df[src].astype(str) != "").any():
            out[label] = col
    for c in df.columns:
        if c.startswith("extra: "):
            out[c] = c
    return out


def filter_options(df):
    """Small JSON-able summary used to build the filter widgets (cached separately from the data)."""
    fields = available_group_fields(df)
    values = {}
    for label, col in fields.items():
        if col == "control_group":
            vals = sorted({x.strip() for s in df["control_groups"].cat.categories for x in str(s).split(";") if x.strip()})
        else:
            vals = sorted(v for v in df[col].cat.categories if v != "")
        if 1 < len(vals) <= 500:
            values[col] = vals
    return {"fields": fields, "values": values,
            "dates": sorted(d.date().isoformat() for d in df["as_of_date"].unique())}


# ----------------------------------------------------------------------------- the chart data model
def normalize_cfg(cfg):
    c = {**DEFAULT_CFG, **(cfg or {})}
    c["filters"] = {k: sorted(v) for k, v in (c.get("filters") or {}).items() if v}
    c["dates"] = sorted(c.get("dates") or [])
    c["group"] = c["group"] or None
    notes = []
    if c["norm100"]:       # 100% stacked needs a stacked count chart: set that up instead of silently ignoring the box
        was = (c["measure"], c["stack"])
        if c["measure"] == "pct":
            c["measure"] = "status"
        if c["stack"] == "off":
            c["stack"] = "both" if c["group"] else "status"
        if (c["measure"], c["stack"]) != was:
            notes.append("100% stacked: Measure set to Status breakdown and Stack set to "
                         f"'{'by group and status' if c['stack'] == 'both' else c['stack']}'.")
    if c["measure"] == "pct":
        if c["stack"] != "off":
            notes.append("Stacking doesn't apply to % complete; showing side by side.")
        c.update(stack="off", norm100=False)
    if c["stack"] in ("status", "both") and c["measure"] != "status":
        notes.append("Stacking by status uses the Status breakdown measure; switched.")
        c["measure"] = "status"
    if c["stack"] in ("group", "both") and not c["group"]:
        notes.append("Stacking by group needs a Group by field; stacking turned off." if c["stack"] == "group"
                     else "No Group by field chosen, so stacking by status only.")
        c["stack"] = "off" if c["stack"] == "group" else "status"
    if c["stack"] == "off" and c["norm100"]:
        c["norm100"] = False
    if c["chart"] == "Line" and c["stack"] != "off":
        c["chart"] = "Area"
        notes.append("A stacked line is drawn as a stacked area.")
    elif c["chart"] == "Area" and c["stack"] == "off":
        c["chart"] = "Line"
    if c["chart"] == "Bar: first vs latest" and c["measure"] != "pct":
        c["chart"] = "Bar"
    return c, notes


def _apply_filters(df, cfg):
    mask = np.ones(len(df), dtype=bool)
    if cfg["dates"]:
        mask &= df["as_of_date"].isin(pd.to_datetime(cfg["dates"])).values
    for col, vals in cfg["filters"].items():
        if col == "control_group":
            ok = np.array([any(x.strip() in vals for x in str(c).split(";")) for c in df["control_groups"].cat.categories] + [False])
            mask &= ok[df["control_groups"].cat.codes.values]
        elif col in df:
            mask &= df[col].isin(vals).values
    d = df[mask]
    if cfg["cohort"] and len(d):
        n = d["as_of_date"].nunique()
        cnt = d.groupby("line_id")["as_of_date"].nunique()
        d = d[d["line_id"].isin(cnt.index[cnt == n])]
    return d


def _counts(d, cfg):
    """One group-by -> wide count table: pass/exception/fail/other/excluded buckets + flagged-excluded count."""
    group = cfg["group"]
    by = ("control_groups" if group == "control_group" else group) if group else None
    keys = ["as_of_date"] + ([by] if by else [])
    g = d.groupby(keys + ["status", "ex"], observed=True).size().rename("n").reset_index()
    for c in ([by] if by else []) + ["status"]:
        g[c] = g[c].astype(str)
    st = g["status"].where(g["status"].isin(["pass", "fail", "exception"]), "other")
    g["bucket"] = np.where(g["ex"] & (cfg["excluded"] == "exclude"), "excluded", st)
    flagged = g[g["ex"]].groupby(keys)["n"].sum().rename("flagged")
    W = g.pivot_table(index=keys, columns="bucket", values="n", aggfunc="sum", fill_value=0)
    W = W.reindex(columns=BUCKETS, fill_value=0).join(flagged).fillna(0).reset_index()
    if group == "control_group":
        W["group"] = W["control_groups"].str.split(";")
        W = W.explode("group").drop(columns="control_groups")
        W["group"] = W["group"].fillna("").str.strip().replace("", "(none)")
    elif group:
        W = W.rename(columns={by: "group"})
    num = [*BUCKETS, "flagged"]
    keys2 = ["as_of_date"] + (["group"] if group else [])
    return W.groupby(keys2, as_index=False)[num].sum()


def _lump(W, max_groups):
    """Keep the largest groups; sum the rest into '(other)' so charts stay legible. Counts are additive."""
    if "group" not in W or W["group"].nunique() <= max_groups:
        return W, 0
    size = W.assign(t=W[BUCKETS].sum(axis=1)).groupby("group")["t"].sum().sort_values(ascending=False)
    keep = set(size.index[:max_groups])
    n_lumped = len(size) - len(keep)
    W = W.assign(group=W["group"].where(W["group"].isin(keep), "(other)"))
    return W.groupby(["as_of_date", "group"], as_index=False)[[*BUCKETS, "flagged"]].sum(), n_lumped


def group_rank(groups, field, W=None):
    """Display order of groups: severity by severity (Critical first), anything else largest first. '(other)' is last."""
    gs = list(dict.fromkeys(groups))
    if field == "severity":
        key = lambda g: (g == "(other)", SEV_RANK.get(str(g).strip().lower(), 9), str(g))
    else:
        size = W.assign(t=W[BUCKETS].sum(axis=1)).groupby("group")["t"].sum().to_dict() if W is not None else {}
        key = lambda g: (g == "(other)", -size.get(g, 0), str(g))
    return {g: i for i, g in enumerate(sorted(gs, key=key))}


def _derive(W, exceptions):
    W = W.copy()
    W["lines"] = W[BUCKETS].sum(axis=1)
    if exceptions == "pass":
        num, den = W["pass"] + W["exception"], W["pass"] + W["fail"] + W["exception"]
    elif exceptions == "fail":
        num, den = W["pass"], W["pass"] + W["fail"] + W["exception"]
    else:
        num, den = W["pass"], W["pass"] + W["fail"]
    W["pct_complete"] = (num / den.where(den > 0)) * 100
    return W


def build_plot_data(df, cfg):
    """df -> {'plot': long frame, 'table': wide frame, 'cfg': final cfg, 'notes': [...], 'facet': bool}"""
    cfg, notes = normalize_cfg(cfg)
    with timed("build_plot_data", measure=cfg["measure"], group=cfg["group"], stack=cfg["stack"]):
        d = _apply_filters(df, cfg)
        if d.empty:
            return {"plot": pd.DataFrame(), "table": pd.DataFrame(), "cfg": cfg, "notes": notes + ["No rows match."], "facet": False}
        W = _counts(d, cfg)
        W, lumped = _lump(W, int(cfg["max_groups"]))
        if lumped:
            notes.append(f"{lumped} smaller groups were combined into '(other)' (limit {cfg['max_groups']}).")
        T = _derive(W, cfg["exceptions"])
        group, measure, stack = cfg["group"], cfg["measure"], cfg["stack"]
        if cfg["chart"] == "Bar: first vs latest":
            ends = [T["as_of_date"].min(), T["as_of_date"].max()]
            fl = T[T["as_of_date"].isin(ends)]
            plot = pd.DataFrame({"as_of_date": fl["as_of_date"], "x": fl["group"] if group else "All lines",
                                 "series": fl["as_of_date"].dt.date.astype(str), "value": fl["pct_complete"]})
            plot["date_label"] = plot["series"]
            return {"plot": plot, "table": _table(T), "cfg": cfg, "notes": notes, "facet": False}
        facet = bool(group) and (cfg["facet"] or stack == "status") and measure == "status"
        if cfg["facet"] and group and measure != "status":
            notes.append("Facets are only used with the Status breakdown measure.")
        if measure == "status":
            drop = [b for b in ("other", "excluded") if T[b].sum() == 0]
            long = T.melt(id_vars=["as_of_date"] + (["group"] if group else []), value_vars=[b for b in BUCKETS if b not in drop],
                          var_name="bucket", value_name="value")
            long["series"] = long["bucket"] if (not group or facet) else long["group"] + " · " + long["bucket"]
            grank = group_rank(T["group"], group, T) if group else {}
            long["s_ord"] = [STACK_ORDER.index(b) * 1000 + (0 if (not group or facet) else grank[g])
                             for b, g in zip(long["bucket"], long["group"] if group else [None] * len(long))]
        else:
            col = {"pct": "pct_complete", "lines": "lines", "pass": "pass", "fail": "fail",
                   "exception": "exception", "excluded": "flagged"}[measure]
            long = T[["as_of_date"] + (["group"] if group else [])].assign(value=T[col])
            long["series"] = long["group"] if group else MEASURES[measure]
            grank = group_rank(T["group"], group, T) if group else {}
            long["s_ord"] = long["group"].map(grank) if group else 0
        idx = ["as_of_date"] + (["group"] if facet else []) + ["series"]
        ord_map = long.drop_duplicates("series").set_index("series")["s_ord"]
        long = long[idx + ["value"]].groupby(idx, as_index=False)["value"].sum(min_count=1)
        long["s_ord"] = long["series"].map(ord_map).astype(int)
        if stack != "off":   # stacked areas/bars need every date x series present
            full = pd.MultiIndex.from_product([long[c].unique() for c in idx], names=idx)
            long = long.set_index(idx).reindex(full, fill_value=0).reset_index()
        if stack != "off":
            long["s_ord"] = long["series"].map(ord_map).astype(int)
        long = long.sort_values(["as_of_date"] + [c for c in idx if c != "as_of_date"])
        if cfg["norm100"]:   # each date (and facet) sums to 100; the count stays in 'value' for the tooltip
            tot = long.groupby(["as_of_date"] + (["group"] if facet else []))["value"].transform("sum")
            long["share"] = (long["value"] / tot.where(tot > 0) * 100).fillna(0)
        long["date_label"] = long["as_of_date"].dt.date.astype(str)
    return {"plot": long, "table": _table(T), "cfg": cfg, "notes": notes, "facet": facet}


def _table(T):
    t = T.rename(columns={"pass": "passed", "fail": "failed", "flagged": "excluded", "excluded": "excluded_bucket"})
    t = t.assign(date_label=T["as_of_date"].dt.date.astype(str))
    return t[[c for c in ["date_label", "group", "lines", "passed", "failed", "exception", "excluded", "pct_complete"] if c in t]]


# ----------------------------------------------------------------------------- change summary / headers
def changes(conn, framework):
    """Per transition: lines added / returned / removed / retired and status movement for matched lines."""
    imps = pd.read_sql_query(
        "SELECT id, as_of_date FROM imports WHERE framework=? ORDER BY as_of_date, id", conn, params=(framework,))
    rows = []
    for a, b in zip(imps.itertuples(), imps.iloc[1:].itertuples()):
        pa = pd.read_sql_query("SELECT line_id, status FROM obs WHERE import_id=?", conn, params=(a.id,))
        pb = pd.read_sql_query("SELECT line_id, status FROM obs WHERE import_id=?", conn, params=(b.id,))
        m = pa.merge(pb, on="line_id", suffixes=("_old", "_new"))
        ended = pd.read_sql_query(
            "SELECT reason, COUNT(*) n FROM ended WHERE gone_import_id=? GROUP BY reason", conn, params=(b.id,))
        e = dict(zip(ended["reason"], ended["n"]))
        ret = conn.execute("SELECT COUNT(*) FROM ended WHERE returned_import_id=?", (b.id,)).fetchone()[0]
        fp = ((m.status_old == "fail") & (m.status_new == "pass"))
        pf = ((m.status_old == "pass") & (m.status_new == "fail"))
        rows.append({
            "from": a.as_of_date, "to": b.as_of_date, "matched": len(m),
            "added": len(pb) - len(m) - ret, "returned": ret,
            "removed (unresolved)": e.get("unresolved", 0), "retired": e.get("retired", 0),
            "fail→pass": int(fp.sum()), "pass→fail": int(pf.sum()),
            "other status changes": int(((m.status_old != m.status_new) & ~(fp | pf)).sum()),
        })
    return pd.DataFrame(rows)


def detect_schema(headers):
    """Name the known export format a header list belongs to (first match in config order, v2 before v1)."""
    hs = {core.norm_header(h) for h in headers}
    for sc in reversed(core.CONFIG.get("known_schemas", [])):
        if any(core.norm_header(r) in hs for r in sc["requires_any"]):
            return sc
    return None


def schema_warning(headers, as_of):
    sc = detect_schema(headers)
    if sc and sc.get("strict") and str(as_of) < sc["first_seen"]:
        return (f"These headers match export format {sc['name']}, which first appeared on {sc['first_seen']}; "
                f"an as-of date of {as_of} is earlier than that. Check the date.")
    return None


def header_history(conn, framework):
    """Show how the CSV headers changed between imports (adds/removes/reorders) and which known format each is."""
    rows, prev = [], None
    for r in conn.execute("SELECT as_of_date, filename, headers_json FROM imports WHERE framework=? "
                          "ORDER BY as_of_date, id", (framework,)):
        h = json.loads(r["headers_json"])
        sc = detect_schema(h)
        rows.append({
            "as_of_date": r["as_of_date"], "file": r["filename"], "format": sc["name"] if sc else "unknown",
            "columns": len(h),
            "added": ", ".join(sorted(set(h) - set(prev))) if prev is not None else "",
            "removed": ", ".join(sorted(set(prev) - set(h))) if prev is not None else "",
            "reordered": bool(prev is not None and set(h) == set(prev) and h != prev),
            "headers": " | ".join(h)})
        prev = h
    return pd.DataFrame(rows)
