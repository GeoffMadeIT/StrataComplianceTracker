# Version: 1.3.2
"""Saved views and their cached results.

A view = a named set of filters + chart options, saved per framework. Its computed chart data is stored in the
database together with a *data version* (a hash of the imports and saved decisions). Opening the view reuses the
stored result while the version is unchanged; after an import or a rule change the next open recomputes it once.
Unsaved option combinations are never persisted, only kept in the running app's memory.
"""
import datetime as dt
import hashlib
import io
import json
import time

import pandas as pd

from .log import log

ANALYSIS_VERSION = "3"      # bump when the calculation logic changes so old cached results are discarded
NUMERIC = ["value", "share", "s_ord", "lines", "passed", "failed", "exception", "excluded", "pct_complete"]


def data_version(conn, framework):
    imps = [tuple(r) for r in conn.execute(
        "SELECT id, as_of_date, content_hash, row_count FROM imports WHERE framework=? ORDER BY as_of_date, id", (framework,))]
    rules = [tuple(r) for r in conn.execute(
        "SELECT id, kind, old_serial, old_bpc_id, old_object_id, new_serial, new_bpc_id, new_object_id, effective_date "
        "FROM rules WHERE framework=? ORDER BY id", (framework,))]
    return hashlib.sha256(json.dumps([ANALYSIS_VERSION, imps, rules]).encode()).hexdigest()[:16]


def cfg_key(cfg):
    return json.dumps(cfg, sort_keys=True, default=str)


# ----------------------------------------------------------------------------- CRUD
def list_views(conn, framework):
    return [dict(r) for r in conn.execute("SELECT * FROM views WHERE framework=? ORDER BY name", (framework,))]


def save_view(conn, framework, name, cfg):
    now = dt.datetime.now().isoformat(timespec="seconds")
    name = name.strip()
    row = conn.execute("SELECT id FROM views WHERE framework=? AND name=?", (framework, name)).fetchone()
    if row:
        conn.execute("UPDATE views SET config_json=?, updated_at=? WHERE id=?", (cfg_key(cfg), now, row["id"]))
        vid = row["id"]
    else:
        vid = conn.execute("INSERT INTO views(framework,name,config_json,created_at,updated_at) VALUES (?,?,?,?,?)",
                           (framework, name, cfg_key(cfg), now, now)).lastrowid
    conn.commit()
    log.info("saved view %r (id %s) for %s", name, vid, framework)
    return vid


def delete_view(conn, view_id):
    conn.execute("DELETE FROM views WHERE id=?", (view_id,))
    conn.commit()
    log.info("deleted view %s", view_id)


def find_by_cfg(conn, framework, cfg_json):
    r = conn.execute("SELECT * FROM views WHERE framework=? AND config_json=?", (framework, cfg_json)).fetchone()
    return dict(r) if r else None


# ----------------------------------------------------------------------------- result cache
def _pack(df):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
    d = d.astype(object).where(d.notna(), None)
    return d.to_dict("list")


def _unpack(packed):
    d = pd.DataFrame(packed)
    if "as_of_date" in d:
        d["as_of_date"] = pd.to_datetime(d["as_of_date"])
    for c in NUMERIC:
        if c in d:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    return d


def put_cached(conn, view_id, cfg_json, version, result, seconds):
    payload = {"plot": _pack(result["plot"]), "table": _pack(result["table"]), "notes": result["notes"],
               "cfg": result["cfg"], "facet": result["facet"]}
    conn.execute("INSERT OR REPLACE INTO view_cache(view_id,config_json,data_version,payload_json,computed_at,compute_seconds)"
                 " VALUES (?,?,?,?,?,?)", (view_id, cfg_json, version, json.dumps(payload, default=str),
                                           dt.datetime.now().isoformat(timespec="seconds"), seconds))
    conn.commit()
    log.info("cached results for view %s (version %s, %.2fs to compute)", view_id, version, seconds)


def get_cached(conn, view_id, cfg_json, version):
    r = conn.execute("SELECT * FROM view_cache WHERE view_id=? AND config_json=? AND data_version=?",
                     (view_id, cfg_json, version)).fetchone()
    if not r:
        return None
    p = json.loads(r["payload_json"])
    return {"plot": _unpack(p["plot"]), "table": _unpack(p["table"]), "notes": p["notes"], "cfg": p["cfg"],
            "facet": p["facet"], "computed_at": r["computed_at"], "compute_seconds": r["compute_seconds"]}


def get_options(conn, framework, version):
    r = conn.execute("SELECT options_json FROM option_cache WHERE framework=? AND data_version=?", (framework, version)).fetchone()
    return json.loads(r["options_json"]) if r else None


def put_options(conn, framework, version, options):
    conn.execute("INSERT OR REPLACE INTO option_cache VALUES (?,?,?)", (framework, version, json.dumps(options)))
    conn.commit()


# ----------------------------------------------------------------------------- share between machines / frameworks
def export_views(conn, framework):
    return json.dumps({"framework": framework, "views": [
        {"name": v["name"], "config": json.loads(v["config_json"])} for v in list_views(conn, framework)]}, indent=1)


def import_views(conn, framework, text):
    data = json.loads(text)
    n = 0
    for v in data.get("views", []):
        save_view(conn, framework, v["name"], v["config"])
        n += 1
    return n
