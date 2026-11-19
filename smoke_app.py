# Version: 1.3.2
"""Run app.py against a stand-in streamlit so name/logic errors surface (does not check real rendering)."""
import runpy, sys, types, tempfile, datetime as dt, contextlib
from pathlib import Path
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE))
import os; os.chdir(HERE)

class Stub:
    def __init__(self, st): self._st = st
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter([])
    def __getattr__(self, n):
        if n in WIDGETS and hasattr(self._st, n): return getattr(self._st, n)
        return lambda *a, **k: Stub(self._st)

WIDGETS = {"selectbox","radio","multiselect","checkbox","number_input","text_input","date_input","file_uploader",
           "button","download_button","data_editor","form_submit_button"}

class Upload:
    def __init__(self, name, data): self.name, self._d = name, data
    def getvalue(self): return self._d

def make_st(upload=None, clicks=(), state=None):
    st = types.ModuleType("streamlit")
    st.session_state = dict(state or {})
    S = Stub(st)
    def deco(*a, **k):
        if len(a) == 1 and callable(a[0]) and not k: return a[0]
        return lambda f: f
    st.cache_resource = deco; st.cache_data = deco
    st.set_page_config = lambda **k: None
    st.columns = lambda spec, **k: [Stub(st) for _ in range(spec if isinstance(spec, int) else len(spec))]
    st.tabs = lambda labels: [Stub(st) for _ in labels]
    st.form = lambda *a, **k: Stub(st)
    st.expander = lambda *a, **k: Stub(st)
    st.spinner = lambda *a, **k: Stub(st)
    st.form_submit_button = lambda *a, **k: False
    st.button = lambda label, *a, **k: label in clicks and not k.get("disabled")
    st.rerun = lambda: None
    class Sidebar(Stub):
        def selectbox(self, label, options, **k): return options[0]
        def title(self, *a): pass
        def info(self, *a): pass
    st.sidebar = Sidebar(st)
    def sel(label, options, key=None, index=0, **k):
        if key is not None: return st.session_state.setdefault(key, options[index])
        return options[index]
    st.selectbox = sel
    def radio(label, options, key=None, **k):
        if key is not None: return st.session_state.setdefault(key, options[0])
        return options[0]
    st.radio = radio
    st.multiselect = lambda label, options, key=None, **k: st.session_state.setdefault(key, []) if key else []
    st.checkbox = lambda label, key=None, **k: st.session_state.setdefault(key, False) if key else False
    st.number_input = lambda label, *a, key=None, **k: st.session_state.setdefault(key, 25)
    st.text_input = lambda label, value="", **k: value
    st.date_input = lambda label, value=None, **k: value
    st.file_uploader = lambda label, **k: upload if label.startswith("Export CSV") else None
    st.data_editor = lambda df, **k: df
    st.download_button = lambda *a, **k: False
    st.write = lambda *a, **k: None
    st.__getattr__ = lambda n: (lambda *a, **k: Stub(st))
    return st

def fake_plotly():
    px = types.ModuleType("plotly.express")
    class Fig:
        def __getattr__(self, n): return lambda *a, **k: self
    px_calls = []
    for n in ("line", "bar", "area"):
        def f(df, *a, _n=n, **k):
            # real px raises if a column name is missing; mimic that check for the main arguments
            for key in ("x", "y", "color", "facet_col"):
                if key in k and k[key] not in df.columns: raise ValueError(f"{_n}: column {k[key]!r} missing")
            px_calls.append((_n, k)); return Fig()
        setattr(px, n, f)
    pkg = types.ModuleType("plotly"); pkg.express = px
    sys.modules["plotly"] = pkg; sys.modules["plotly.express"] = px
    return px_calls

def run(db_path, upload=None, clicks=(), seed=None, names_csv=None, state=None):
    from tracker import core
    core.DB_PATH = Path(db_path)
    if seed:
        c = core.connect(db_path)
        for d in seed:
            y, m, dd = map(int, d.split("-"))
            core.import_csv(c, "NIST CSF 2.0", dt.date(y, m, dd), Path(f"samples/nist_csf_{d}.csv").read_bytes(), f"nist_csf_{d}.csv")
    for m in [m for m in sys.modules if m == "app"]: del sys.modules[m]
    if names_csv:
        from tracker import names as _n
        _n.NAMES_PATH = Path(names_csv)
    st = make_st(upload, clicks, state)
    sys.modules["streamlit"] = st
    calls = fake_plotly()
    errors = []
    st.error = lambda msg, *a, **k: errors.append(str(msg))
    runpy.run_path("app.py", run_name="__main__")
    return st, calls, errors

if __name__ == "__main__":
    d = tempfile.mkdtemp()
    # 1) empty database
    st, calls, errors = run(f"{d}/a.db"); assert not errors, errors; print("empty db ok")
    # 2) seeded db: trends tab with default options builds a figure; saved-view flow
    st, calls, errors = run(f"{d}/b.db", seed=["2026-08-13", "2026-09-10", "2026-10-01"]); assert not errors, errors
    assert calls, "no figure built"; print("trends ok:", calls[-1][0], sorted(calls[-1][1])[:6])
    # 3) every stack/measure/chart choice through the UI path
    import itertools
    from tracker import analysis
    for meas, stk, ch in itertools.product(analysis.MEASURES.values(), ["Off (side by side)", "By group", "By status", "By both"], ["Line", "Bar", "Bar: first vs latest"]):
        pass
    # 4) preflight display with an upload of an already-imported file
    up = Upload("nist_csf_2026-08-13.csv", Path("samples/nist_csf_2026-08-13.csv").read_bytes())
    st, calls, errors = run(f"{d}/b.db", upload=up); print("import tab errors shown:", errors)
    assert any("Already imported" in e for e in errors), "duplicate message missing"
    # 5) a new, different file: no duplicate, structure message path
    up2 = Upload("nist_csf_2026-10-01.csv", Path("samples/nist_csf_2026-10-01.csv").read_bytes())
    st, calls, errors = run(f"{d}/c.db", upload=up2, seed=["2026-08-13"]); print("new-file errors:", errors); assert not errors
    # 6) device names shown in the review queue and chart labels (group by serial)
    nf = Path(d) / "names.csv"
    nf.write_text("serial,name\n23456789012345,Branch-FW\n45678901234567,Branch-FW\n")
    st, calls, errors = run(f"{d}/b.db", names_csv=nf, state={"tv_group": "Device serial", "tv_measure": "Lines (total)"})
    assert not errors, errors
    print("names + group-by-serial ok")
    print("APP SMOKE OK")
