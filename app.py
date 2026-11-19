# Version: 1.3.2
"""Compliance export tracker dashboard.  Run:  streamlit run app.py"""
import contextlib
import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from tracker import analysis, charts, core, names, views
from tracker import version as ver
from tracker import log as dbg
from tracker.log import log

st.set_page_config(page_title="Compliance Tracker", layout="wide")


@st.cache_resource
def get_conn():
    log.info("app start: version %s, python files %s", ver.VERSION,
             ", ".join(f"{f}={v}" for f, v in ver.file_versions() if v != ver.VERSION) or "all at this version")
    return core.connect()


@contextlib.contextmanager
def guard(name):
    """Show an error (details in the Debug tab) instead of a raw traceback; never swallow Streamlit control flow."""
    try:
        yield
    except Exception as e:                       # noqa: BLE001
        if type(e).__name__ in ("RerunException", "StopException"):
            raise
        log.exception("UI error in %s", name)
        st.error(f"{name} failed: {e}. The full traceback is in the Debug tab.")


try:
    conn = get_conn()
except core.SchemaOutdated as e:          # database from an older version: it must be upgraded first
    st.error(str(e))
    st.stop()
nm = names.get()                      # serial -> friendly name (display only); reloads when the file changes


def L(serial):
    return names.label(serial, nm)


@st.cache_resource(max_entries=2, show_spinner="Loading data (happens once after each import or rule change)…")
def get_df(_conn, fw, version):
    return analysis.load_df(_conn, fw)


@st.cache_data(max_entries=4, show_spinner=False)
def changes_table(_conn, fw, version):
    return analysis.changes(_conn, fw)


def get_opts(fw, version):
    o = views.get_options(conn, fw, version)
    if o is None:
        o = analysis.filter_options(get_df(conn, fw, version))
        views.put_options(conn, fw, version, o)
    return o


# ------------------------------------------------------------------ sidebar: framework
frameworks = [r[0] for r in conn.execute("SELECT DISTINCT framework FROM imports ORDER BY 1")]
st.sidebar.title("Compliance Tracker")
st.sidebar.caption(f"Version {ver.VERSION}")
fw = st.sidebar.selectbox("Framework", frameworks) if frameworks else None
if not frameworks:
    st.sidebar.info("No imports yet. Use the Import tab.")
version = views.data_version(conn, fw) if fw else ""

tab_import, tab_trend, tab_review, tab_data, tab_debug = st.tabs(
    ["Import", "Trends", "Review queue", "Imports & rules", "Debug"])

# ------------------------------------------------------------------ import
DECISIONS = {"ignore": "_ignore", "keep as extra column": "_keep", **{f: f for f in core.CANON}}


@st.cache_data(max_entries=4, show_spinner="Reading the file (once per file and set of choices)…")
def cached_preflight(_conn, fw_name, as_of_iso, src_id, extra_json, db_version, _source):
    """The file is scanned in a stream; the result is cached so clicking around doesn't re-read it."""
    return core.preflight(_conn, fw_name, as_of_iso, _source, json.loads(extra_json))


with tab_import, guard("Import"):
    st.subheader("Import a CSV export")
    gen = st.session_state.setdefault("imp_gen", 0)      # bumped after a successful import to empty the inputs
    done = st.session_state.pop("imp_done", None)
    if done:
        st.success(done)
    c1, c2 = st.columns(2)
    fw_name = c1.text_input("Framework name", value=fw or "NIST CSF 2.0",
                            help="Exports are matched only within the same framework name.")
    up = c2.file_uploader("Export CSV (browser upload; the default limit is 200 MB)", type=["csv"], key=f"imp_up_{gen}")
    path_txt = c2.text_input("…or a file path on this computer (no size limit, nothing is uploaded)", key=f"imp_path_{gen}",
                             placeholder="C:\\exports\\export.csv   or   /home/me/export.csv")
    source, src_name, src_id, src_mb = None, "", "", 0.0
    if up is not None:
        source, src_name, src_mb = up.getvalue(), up.name, len(up.getvalue()) / 1e6
        src_id = f"up:{getattr(up, 'file_id', up.name)}:{len(source)}"
    elif path_txt.strip():
        pth = Path(path_txt.strip().strip('"'))
        if pth.is_file():
            stt = pth.stat()
            source, src_name, src_mb = str(pth), pth.name, stt.st_size / 1e6
            src_id = f"path:{pth}:{stt.st_size}:{stt.st_mtime_ns}"
        else:
            c2.error("That path isn't a file.")
    if source is not None:
        as_of = c1.date_input("As-of date (when the data was true, not when you import it)",
                              value=core.guess_date(src_name) or dt.date.today())
        if core.guess_date(src_name):
            c1.caption("Date guessed from the file name; change it if wrong.")
        dbv = views.data_version(conn, fw_name)
        pf0 = cached_preflight(conn, fw_name, as_of.isoformat(), src_id, "{}", dbv, source)
        warn = analysis.schema_warning(pf0["headers"], as_of)
        if warn:
            c1.warning(warn)

        # columns without a decision: ignored by default; choose to map or keep. The choice is remembered.
        extra_map = {}
        if pf0["undecided"]:
            st.warning(f"{len(pf0['undecided'])} column(s) have no saved decision yet. They are ignored (not stored) "
                       "unless you map or keep them. Wide text columns cost a lot of space, so keep only what you "
                       "will filter or group by.")
            tbl = pd.DataFrame({"Column": pf0["undecided"], "avg chars per row": [pf0["avg_len"].get(h, 0) for h in pf0["undecided"]],
                                "Decision": "ignore"})
            edited = st.data_editor(tbl, hide_index=True, disabled=["Column", "avg chars per row"], key=f"dec_{src_id}",
                                    column_config={"Decision": st.column_config.SelectboxColumn(options=list(DECISIONS), required=True)})
            extra_map = {r.Column: DECISIONS[r.Decision] for r in edited.itertuples()}
        pf = cached_preflight(conn, fw_name, as_of.isoformat(), src_id, json.dumps(extra_map, sort_keys=True), dbv, source) \
            if extra_map else pf0
        st.write(f"**{pf['rows']:,} rows, {len(pf['headers'])} columns, {src_mb:,.0f} MB.**")

        # 1) column structure
        s = pf["structure"]
        if s["kind"] == "first":
            st.info("Column structure: first import for this framework.")
        elif s["kind"] == "identical":
            st.success(f"Column structure: identical to the {s['ref']} import.")
        elif s["kind"] == "reordered":
            st.info(f"Column structure: same columns as the {s['ref']} import in a different order. "
                    "Columns are matched by name, so this is handled automatically.")
        else:
            st.warning(f"Column structure changed since the {s['ref']} import. "
                       f"Added: {', '.join(s['added']) or 'none'}. Removed: {', '.join(s['removed']) or 'none'}. "
                       "Check the column decisions above.")
        with st.expander("How each column will be used"):
            use = {**{h: f"→ {f}" for h, f in pf["mapping"].items()}, **{h: "kept as extra column" for h in pf["keep"]}}
            st.dataframe(pd.DataFrame({"CSV header": pf["headers"], "Use": [use.get(h, "ignored") for h in pf["headers"]]}),
                         hide_index=True)
        if pf["missing_required"]:
            st.error(f"Required fields not found: {', '.join(pf['missing_required'])}. Map them in the table above.")

        # 2) is this DATA already stored?
        allow_dup = False
        if pf["file_dup"]:
            for d in pf["file_dup"]:
                kind = "The same file" if d["match"] == "file" else "A file with different bytes but identical data"
                st.error(f"Already imported: {kind} was imported on {d['imported_at']} as the {d['as_of_date']} "
                         f"export ({d['filename'] or 'unnamed'}).")
            allow_dup = st.checkbox("Import it again anyway (creates a duplicate)")

        # 3) is there DIFFERENT data for this as-of date? Always ask.
        same = list(pf["same_date"])
        if allow_dup:
            same += [d for d in pf["file_dup"] if d["as_of_date"] == as_of.isoformat()]
        action = None
        if same:
            st.warning("Other data already exists for the as-of date " + as_of.isoformat() + ": " + "; ".join(
                f"{d['filename'] or 'unnamed'} ({d['row_count']} rows, imported {d['imported_at']})" for d in same))
            choice = st.radio("What should happen to the existing import(s)?",
                              ["— choose —", "Replace them with this file", "Keep both (both appear on this date)"],
                              key=f"same_date_choice_{src_id}_{as_of}")
            action = {"Replace them with this file": "replace", "Keep both (both appear on this date)": "keep_both"}.get(choice)

        blocked = bool(pf["missing_required"]) or (bool(pf["file_dup"]) and not allow_dup) or (bool(same) and action is None)
        if st.button("Import", type="primary", disabled=blocked):
            with st.spinner("Importing and matching…"):
                try:
                    core.import_csv(conn, fw_name, as_of, source, src_name, extra_map, allow_duplicate=allow_dup,
                                    same_date_action=action)
                    st.session_state["imp_done"] = (f"Imported {pf['rows']:,} rows from {src_name} as of {as_of}. "
                                                    "See the Review queue for lines that need a decision.")
                    st.session_state["imp_gen"] = gen + 1
                    cached_preflight.clear()
                    st.rerun()
                except core.ImportProblem as e:
                    st.error(str(e))

# ------------------------------------------------------------------ trends
MEASURE_BY_LABEL = {v: k for k, v in analysis.MEASURES.items()}
STACKS = {"Off (side by side)": "off", "By group": "group", "By status": "status", "By both": "both"}
EXC = {"left out of the %": "exclude", "count as pass": "pass", "count as fail": "fail"}
EXD = {"left out of the %": "exclude", "included in the %": "include"}
CHARTS = ["Line", "Bar", "Bar: first vs latest"]
DEFAULTS = {"tv_measure": "% complete", "tv_group": "(none)", "tv_chart": "Line", "tv_stack": "Off (side by side)",
            "tv_norm": False, "tv_facet": False, "tv_exc": "left out of the %", "tv_exd": "left out of the %",
            "tv_cohort": False, "tv_dates": [], "tv_maxg": 25}
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)


def apply_cfg(cfg, opts):
    """Push a saved view's options into the widgets (values no longer available are dropped)."""
    c = {**analysis.DEFAULT_CFG, **cfg}
    inv = {col: label for label, col in opts["fields"].items()}
    ss = st.session_state
    ss["tv_measure"] = analysis.MEASURES.get(c["measure"], "% complete")
    ss["tv_group"] = inv.get(c["group"], "(none)")
    ss["tv_chart"] = c["chart"] if c["chart"] in CHARTS else "Line"
    ss["tv_stack"] = next((k for k, v in STACKS.items() if v == c["stack"]), "Off (side by side)")
    ss["tv_norm"], ss["tv_facet"], ss["tv_cohort"] = c["norm100"], c["facet"], c["cohort"]
    ss["tv_exc"] = next((k for k, v in EXC.items() if v == c["exceptions"]), "left out of the %")
    ss["tv_exd"] = next((k for k, v in EXD.items() if v == c["excluded"]), "left out of the %")
    ss["tv_dates"], ss["tv_maxg"] = [d for d in c["dates"] if d in opts["dates"]], int(c["max_groups"])
    for col in opts["values"]:
        ss[f"tv_f_{col}"] = [v for v in c["filters"].get(col, []) if v in opts["values"][col]]


with tab_trend, guard("Trends"):
    if not fw:
        st.info("Import a file first.")
    else:
        opts = get_opts(fw, version)
        fields = opts["fields"]
        ss = st.session_state
        if ss.get("tv_fw") != fw:                  # switched framework: start from defaults
            for k_ in [k_ for k_ in ss if str(k_).startswith("tv_f_")]:
                del ss[k_]
            ss.update(DEFAULTS)
            ss["tv_fw"] = fw
        ss["tv_dates"] = [d for d in ss["tv_dates"] if d in opts["dates"]]      # drop values that no longer exist
        for col_, vals_ in opts["values"].items():
            ss[f"tv_f_{col_}"] = [v for v in ss.get(f"tv_f_{col_}", []) if v in vals_]
        if ss["tv_group"] != "(none)" and ss["tv_group"] not in fields:
            ss["tv_group"] = "(none)"
        saved = views.list_views(conn, fw)
        v1, v2, v3, v4 = st.columns([3, 1, 1, 3])
        view_by_name = {s["name"]: s for s in saved}
        pick = v1.selectbox("Saved views", ["(none)"] + list(view_by_name), label_visibility="collapsed")
        if v2.button("Load", disabled=pick == "(none)"):
            apply_cfg(json.loads(view_by_name[pick]["config_json"]), opts)
            st.rerun()
        if v3.button("Delete", disabled=pick == "(none)"):
            views.delete_view(conn, view_by_name[pick]["id"])
            st.rerun()
        up_views = v4.file_uploader("Import views (.json)", type=["json"], label_visibility="collapsed")
        if up_views is not None and v4.button("Add these views"):
            st.success(f"Added {views.import_views(conn, fw, up_views.getvalue().decode())} view(s).")
            st.rerun()

        with st.form("trend_form"):
            a, b, c = st.columns(3)
            a.selectbox("Measure", list(MEASURE_BY_LABEL), key="tv_measure")
            b.selectbox("Group by", ["(none)"] + list(fields), key="tv_group")
            c.radio("Chart", CHARTS, key="tv_chart", horizontal=True)
            d, e, f = st.columns(3)
            d.selectbox("Stack", list(STACKS), key="tv_stack",
                        help="Stacking applies to counts, not % complete. A stacked line is drawn as a stacked area.")
            e.checkbox("100% stacked", key="tv_norm",
                       help="Segments add up to 100% of lines; tooltips show the line count and the %. Switches Measure to "
                            "Status breakdown and Stack to by status (or by group and status if Group by is set).")
            f.checkbox("Facet by group (status breakdown)", key="tv_facet")
            g, h, i = st.columns(3)
            g.selectbox("Exceptions", list(EXC), key="tv_exc")
            h.selectbox("User-excluded lines", list(EXD), key="tv_exd")
            i.checkbox("Only lines present in every import", key="tv_cohort")
            st.multiselect("Imports to include (empty = all)", opts["dates"], key="tv_dates")
            filt = {}
            with st.expander("Filters"):
                cols = st.columns(3)
                for n, (label, col) in enumerate(fields.items()):
                    if col in opts["values"]:
                        filt[col] = cols[n % 3].multiselect(label, opts["values"][col], key=f"tv_f_{col}",
                                                            format_func=L if col == "serial_key" else str)
                st.number_input("Max groups before combining the rest into '(other)'", 5, 200, key="tv_maxg")
            st.form_submit_button("Update chart", type="primary")

        raw = dict(group=fields.get(ss["tv_group"]), measure=MEASURE_BY_LABEL[ss["tv_measure"]], chart=ss["tv_chart"],
                   stack=STACKS[ss["tv_stack"]], norm100=ss["tv_norm"], facet=ss["tv_facet"], exceptions=EXC[ss["tv_exc"]],
                   excluded=EXD[ss["tv_exd"]], cohort=ss["tv_cohort"], filters={k: v for k, v in filt.items() if v},
                   dates=ss["tv_dates"], max_groups=int(ss["tv_maxg"]))
        cfg, notes = analysis.normalize_cfg(raw)
        cfg_json = views.cfg_key(cfg)
        match = views.find_by_cfg(conn, fw, cfg_json)

        t0 = time.perf_counter()
        res, source = None, "computed"
        if match:
            res = views.get_cached(conn, match["id"], cfg_json, version)
            source = f"saved view “{match['name']}” (stored result)" if res else source
        if res is None:
            res = analysis.build_plot_data(get_df(conn, fw, version), cfg)
            if match:
                views.put_cached(conn, match["id"], cfg_json, version, res, time.perf_counter() - t0)
                source = f"saved view “{match['name']}” (recomputed after data changed, now stored)"
        elapsed = time.perf_counter() - t0
        if cfg["group"] == "serial_key" and nm and not res["plot"].empty:
            view = {**res, "plot": res["plot"].copy(), "table": res["table"].copy()}
            for df_ in (view["plot"], view["table"]):
                for col_ in ("series", "group", "x"):
                    if col_ in df_:
                        df_[col_] = df_[col_].map(lambda v: names.label_series(v, nm))
            res = view
        for n_ in dict.fromkeys(notes + res["notes"]):
            st.caption("ℹ️ " + n_)
        if res["plot"].empty:
            st.warning("No rows match the filters.")
        else:
            st.plotly_chart(charts.make_figure(res), width="stretch")
            st.dataframe(res["table"].round({"pct_complete": 1}), hide_index=True)
        st.caption(f"{source} · {elapsed:.2f}s · {len(res['plot'])} plotted points")

        s1, s2, s3 = st.columns([3, 1, 2])
        name = s1.text_input("Save these options as a view", value=match["name"] if match else "", placeholder="view name")
        if s2.button("Save view", disabled=not name.strip() or res["plot"].empty):
            vid = views.save_view(conn, fw, name, cfg)
            views.put_cached(conn, vid, cfg_json, version, res, elapsed)
            st.rerun()
        if saved:
            if s3.button("Refresh all saved views"):
                with st.spinner("Recomputing saved views…"):
                    df_ = get_df(conn, fw, version)
                    for sv in saved:
                        t1 = time.perf_counter()
                        c_ = json.loads(sv["config_json"])
                        r_ = analysis.build_plot_data(df_, c_)
                        views.put_cached(conn, sv["id"], sv["config_json"], version, r_, time.perf_counter() - t1)
                st.success(f"Refreshed {len(saved)} view(s).")
            s3.download_button("Export views (.json)", views.export_views(conn, fw), file_name=f"views_{fw}.json")
        st.markdown("#### What changed between imports")
        st.dataframe(changes_table(conn, fw, version), hide_index=True)

# ------------------------------------------------------------------ review queue
with tab_review, guard("Review queue"):
    if not fw:
        st.info("Import a file first.")
    else:
        imps = core.list_imports(conn, fw)
        if len(imps) < 2:
            st.info("Need at least two imports to compare.")
        else:
            labels = {i["id"]: f"{imps[k-1]['as_of_date']} → {i['as_of_date']}" for k, i in enumerate(imps) if k}
            gone_id = st.selectbox("Transition", list(labels)[::-1], format_func=labels.get)
            k = next(n for n, i in enumerate(imps) if i["id"] == gone_id)
            prev_id = imps[k - 1]["id"]
            asof = imps[k]["as_of_date"]
            gone = core.unresolved(conn, fw, gone_id)
            added = core.new_lines(conn, fw, gone_id, prev_id)
            m1, m2 = st.columns(2)
            m1.metric("Removed, unexplained", len(gone))
            m2.metric("New lines", len(added))
            if not gone:
                st.success("Nothing unresolved for this transition.")
            sa = core.suggest_serial_aliases(gone, added, names=nm)
            if sa:
                st.markdown("##### Likely replaced devices (serial changed)")
                for s_ in sa:
                    a, b, c = st.columns([4, 1, 1])
                    a.write(f"`{L(s_['old_serial'])}` → `{L(s_['new_serial'])}` — {s_['pct_old']:.0%} of the old device's "
                            f"{s_['gone_lines']:,} lines and {s_['pct_new']:.0%} of the new device's lines match"
                            + (", **same name**" if s_["same_name"] else "")
                            + (" (name only, little line evidence)" if s_["basis"] == "name only" else ""))
                    if b.button("Accept", key=f"al_{s_['old_serial']}"):
                        core.apply_rules(conn, fw, [("serial_alias", asof, "accepted suggestion",
                                                     {"old_serial": s_["old_serial"], "new_serial": s_["new_serial"]})])
                        st.rerun()
            rn = core.suggest_rem_renames(conn, fw, gone_id)
            if rn:
                st.markdown("##### Remediation section renamed?")
                st.caption("The same section changed between the same two values on several lines and devices. "
                           "A change seen on only one device or line is left as removed + added.")
                for r_ in rn:
                    a, b = st.columns([5, 1])
                    a.write(f"`{r_['section']}`: `{r_['old']}` → `{r_['new']}` — {r_['lines']} lines on {r_['devices']} devices")
                    if b.button("Accept", key=f"rn_{r_['section']}_{r_['old']}_{r_['new']}"):
                        core.apply_rules(conn, fw, [("rem_rename", asof, "accepted suggestion", {
                            "section": r_["section"], "old_value": r_["old"], "new_value": r_["new"]})])
                        st.rerun()
            by_serial = pd.Series([g["serial_key"] for g in gone]).value_counts()
            if len(by_serial):
                st.markdown("##### Removed lines by device serial")
                new_serials = sorted({a["serial_key"] for a in added})
                for ser, n in by_serial.items():
                    a, b, c, d = st.columns([3, 3, 1, 1])
                    a.write(f"`{L(ser)}` — {n} lines")
                    target = b.selectbox("replaced by", ["—"] + new_serials, key=f"t_{ser}", label_visibility="collapsed", format_func=L)
                    if c.button("Replaced", key=f"rp_{ser}", disabled=target == "—"):
                        core.apply_rules(conn, fw, [("serial_alias", asof, "manual", {"old_serial": ser, "new_serial": target})])
                        st.rerun()
                    if d.button("Retire", key=f"rt_{ser}"):
                        core.apply_rules(conn, fw, [("retire_serial", asof, "device retired", {"old_serial": ser})])
                        st.rerun()
            sl = core.suggest_line_links(gone, added)
            if sl:
                st.markdown("##### Likely same line, one identifier changed")
                st.dataframe(pd.DataFrame([{"match on": s_["tier"], "bpc_id": s_["old"]["bpc_id"], "location": s_["old"]["location"],
                                            "old device": L(s_["old"]["serial_key"]), "new device": L(s_["new"]["serial_key"]),
                                            "old object": s_["old"]["object_id"], "new object": s_["new"]["object_id"]}
                                           for s_ in sl]), hide_index=True)
                if st.button("Accept all suggested links"):
                    core.apply_rules(conn, fw, [("link", asof, "suggested " + s_["tier"], {
                        "old_serial": s_["old"]["serial_key"], "old_bpc_id": s_["old"]["bpc_id"], "old_object_id": s_["old"]["object_id"],
                        "new_serial": s_["new"]["serial_key"], "new_bpc_id": s_["new"]["bpc_id"], "new_object_id": s_["new"]["object_id"]})
                        for s_ in sl])
                    st.rerun()
            if gone:
                st.markdown("##### Retire individual lines (intentionally removed)")
                g = pd.DataFrame(gone)
                g["title"] = g["bpc_id"].map(core.titles(conn, fw)).fillna("")
                g = g[["serial_key", "bpc_id", "title", "object_id", "location", "status"]]
                g.insert(1, "device name", g["serial_key"].map(lambda x: nm.get(x, "")))
                g.insert(0, "retire", False)
                edited = st.data_editor(g, hide_index=True, disabled=[c for c in g.columns if c != "retire"])
                if st.button("Retire selected") and edited["retire"].any():
                    sel = edited[edited["retire"]]
                    core.apply_rules(conn, fw, [("retire_line", asof, "manual", {
                        "old_serial": r.serial_key, "old_bpc_id": r.bpc_id, "old_object_id": r.object_id})
                        for r in sel.itertuples()])
                    st.rerun()
                st.markdown("##### Link a removed line to a new line manually")
                gl = {f"{L(x['serial_key'])} · {x['bpc_id']} · {x['object_id']}": x for x in gone}
                al = {f"{L(x['serial_key'])} · {x['bpc_id']} · {x['object_id']}": x for x in added}
                lc1, lc2, lc3 = st.columns([3, 3, 1])
                o = lc1.selectbox("Removed line", list(gl))
                n_ = lc2.selectbox("New line", list(al) or ["(none)"])
                if lc3.button("Link", disabled=not al):
                    x, y = gl[o], al[n_]
                    core.apply_rules(conn, fw, [("link", asof, "manual", {
                        "old_serial": x["serial_key"], "old_bpc_id": x["bpc_id"], "old_object_id": x["object_id"],
                        "new_serial": y["serial_key"], "new_bpc_id": y["bpc_id"], "new_object_id": y["object_id"]})])
                    st.rerun()

# ------------------------------------------------------------------ data & rules
with tab_data, guard("Imports & rules"):
    if not fw:
        st.info("Nothing imported yet.")
    else:
        st.subheader("Imports")
        imps = core.list_imports(conn, fw)
        st.dataframe(pd.DataFrame([{"id": i["id"], "as_of_date": i["as_of_date"], "imported_at": i["imported_at"],
                                    "file": i["filename"], "rows": i["row_count"]} for i in imps]), hide_index=True)
        e1, e2, e3 = st.columns(3)
        pick_i = e1.selectbox("Import id", [i["id"] for i in imps])
        new_date = e2.date_input("Change as-of date", value=dt.date.fromisoformat(
            next(i["as_of_date"] for i in imps if i["id"] == pick_i)))
        if e2.button("Save date"):
            core.set_as_of(conn, pick_i, new_date)
            st.rerun()
        if e3.button("Delete this import"):
            core.delete_import(conn, pick_i)
            st.rerun()
        st.subheader("Header history")
        st.dataframe(analysis.header_history(conn, fw), hide_index=True)
        st.subheader("Saved decisions (rules)")
        rules = [dict(r) for r in conn.execute("SELECT * FROM rules WHERE framework=? ORDER BY id", (fw,))]
        if rules:
            st.dataframe(pd.DataFrame(rules).drop(columns=["framework"]), hide_index=True)
            rid = st.selectbox("Remove rule id", [r["id"] for r in rules])
            if st.button("Remove rule"):
                core.delete_rule(conn, rid, fw)
                st.rerun()
        else:
            st.caption("No decisions saved yet.")
        st.subheader("Device names")
        st.caption("Names are display-only (columns: serial, name, notes). The file is re-read automatically when it changes.")
        st.markdown(f"**File in use:** `{nm.path}`")
        st.write(f"{len(nm)} name(s) loaded from {nm.rows_read} row(s) ({nm.size:,} bytes, last saved "
                 f"{dt.datetime.fromtimestamp(nm.path.stat().st_mtime):%Y-%m-%d %H:%M:%S}).")
        if nm.others:
            st.warning("Other device_names file(s) found that are NOT being used: " + "; ".join(f"`{o}`" for o in nm.others)
                       + ". Edit the one named above, or copy yours over it. This app is running from "
                       f"`{Path(__file__).resolve().parent}`.")
        for w_ in nm.warnings:
            st.warning(w_)
        seen = [r[0] for r in conn.execute("SELECT DISTINCT o.serial_key FROM obs o JOIN imports i ON i.id=o.import_id "
                                           "WHERE i.framework=?", (fw,))]
        missing = [x for x in seen if x not in nm]
        if missing:
            st.write(f"{len(missing)} of {len(seen)} serials in this framework have no name yet.")
            st.download_button("Download a template of unnamed serials (.csv)", names.template_for(seen, nm),
                               file_name="device_names_template.csv")
        if nm:
            st.dataframe(pd.DataFrame({"serial": list(nm), "name": list(nm.values())}), hide_index=True)
        st.subheader("Saved column mappings")
        st.dataframe(pd.read_sql_query("SELECT * FROM column_map ORDER BY framework, header_norm", conn), hide_index=True)

# ------------------------------------------------------------------ debug
with tab_debug, guard("Debug"):
    st.caption("Serial numbers are masked to their last 4 digits in the log. Object Ids and remediation paths are never logged.")
    d1, d2, d3 = st.columns(3)
    lvl = d1.selectbox("Log level", ["INFO", "DEBUG"], index=0 if dbg.level_name() != "DEBUG" else 1)
    if lvl != dbg.level_name():
        dbg.set_level(lvl)
    n_obs = conn.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
    d2.metric("Rows stored", f"{n_obs:,}")
    d3.metric("Data version", version or "—")
    fv = ver.file_versions()
    odd = [f for f, v in fv if v != ver.VERSION]
    st.markdown(f"##### Program version {ver.VERSION}")
    if odd:
        st.warning("Files not at " + ver.VERSION + ": " + ", ".join(f"{f} ({v or 'no version'})" for f, v in odd)
                   + ". If this is unexpected, an update may not have been fully copied over (restart the app after copying).")
    else:
        st.caption("All project files are at the same version.")
    with st.expander("File versions"):
        st.dataframe(pd.DataFrame(fv, columns=["file", "version"]), hide_index=True)
    if dbg.TIMINGS:
        st.markdown("##### Recent timings (this app session)")
        st.dataframe(pd.DataFrame(list(dbg.TIMINGS)[::-1], columns=["time", "step", "seconds", "context"]),
                     hide_index=True, height=250)
    st.markdown("##### Log (latest 300 lines)")
    st.code(dbg.tail(300) or "(empty)", language="text")
    b1, b2 = st.columns(2)
    if dbg.LOG_PATH.exists():
        b1.download_button("Download log", dbg.LOG_PATH.read_bytes(), file_name="debug.log")
    if b2.button("Clear log"):
        dbg.clear()
        st.rerun()
