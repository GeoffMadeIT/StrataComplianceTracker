# Version: 1.3.2
"""Import a CSV export from the command line (handy for very large files, no browser upload).

    python import_cli.py export.csv --framework "NIST CSF 2.0" --date 2026-10-01
    python import_cli.py export.csv --framework "NIST CSF 2.0"            # date guessed from the file name
    python import_cli.py export.csv --framework F --keep "Some Column"    # keep an extra column (repeatable)
    python import_cli.py export.csv --framework F --same-date replace     # or keep_both
    python import_cli.py export.csv --framework F --check                 # run the checks, change nothing

Unrecognized columns are ignored unless you name them with --keep, or map them with --map "Header=field".
Decisions are remembered for next time.
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from tracker import core


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--framework", required=True)
    ap.add_argument("--date", help="as-of date, YYYY-MM-DD (default: guessed from the file name)")
    ap.add_argument("--keep", action="append", default=[], metavar="HEADER", help="keep this extra column")
    ap.add_argument("--map", action="append", default=[], metavar="HEADER=FIELD", help="map a column to a field")
    ap.add_argument("--same-date", choices=["replace", "keep_both"])
    ap.add_argument("--allow-duplicate", action="store_true")
    ap.add_argument("--check", action="store_true", help="only run the checks")
    ap.add_argument("--db")
    a = ap.parse_args()
    path = Path(a.file)
    if not path.exists():
        sys.exit(f"File not found: {path}")
    as_of = dt.date.fromisoformat(a.date) if a.date else core.guess_date(path.name)
    if not as_of:
        sys.exit("No date in the file name; pass --date YYYY-MM-DD.")
    extra = {h: "_keep" for h in a.keep}
    for m in a.map:
        h, _, f = m.partition("=")
        if f not in core.CANON:
            sys.exit(f"Unknown field {f!r}. Fields: {', '.join(core.CANON)}")
        extra[h] = f
    try:
        conn = core.connect(a.db)
    except core.SchemaOutdated as e:
        sys.exit(str(e))
    t0 = time.perf_counter()
    pf = core.preflight(conn, a.framework, as_of, str(path), extra)
    print(f"{path.name}: {pf['rows']:,} rows, {len(pf['headers'])} columns, as of {as_of}")
    s = pf["structure"]
    print("Column structure:", {"first": "first import for this framework", "identical": f"identical to {s.get('ref')}",
                                "reordered": f"same columns reordered vs {s.get('ref')}",
                                "changed": f"changed since {s.get('ref')} (added {s.get('added')}, removed {s.get('removed')})"}[s["kind"]])
    if pf["missing_required"]:
        sys.exit(f"Required fields not found: {pf['missing_required']}. Use --map.")
    if pf["undecided"]:
        print("Ignored columns (no decision yet; use --keep or --map to change):")
        for h in pf["undecided"]:
            print(f"   {h}  (avg {pf['avg_len'].get(h, 0)} chars per row)")
    for d in pf["file_dup"]:
        print(f"ALREADY IMPORTED ({'same file' if d['match'] == 'file' else 'identical data'}): {d['as_of_date']} {d['filename']}")
    for d in pf["same_date"]:
        print(f"OTHER DATA EXISTS for this date: {d['filename']} ({d['row_count']} rows)")
    if a.check:
        return
    try:
        core.import_csv(conn, a.framework, as_of, str(path), path.name, extra, a.allow_duplicate, a.same_date)
    except core.DuplicateFile:
        sys.exit("Not imported: this data is already stored (use --allow-duplicate to force).")
    except core.SameDateConflict:
        sys.exit("Not imported: different data exists for this date. Re-run with --same-date replace or keep_both.")
    except core.ImportProblem as e:
        sys.exit(f"Not imported: {e}")
    print(f"Imported in {time.perf_counter() - t0:.1f}s. Open the dashboard's Review queue to check lines that disappeared.")


if __name__ == "__main__":
    main()
