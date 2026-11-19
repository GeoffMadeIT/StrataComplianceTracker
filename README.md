# Compliance Export Tracker

Version: 1.3.2

Imports Palo Alto Strata compliance-center CSV exports (built around NIST CSF 2.0, works for any framework),
matches each export to the previous one, and charts progress over time.

## Run it
```
pip install -r requirements.txt
streamlit run app.py
```
Data lives in `data/tracker.db` (SQLite, local only). Back that file up. The debug log is `data/debug.log`.

**Upgrading an existing database:** close the dashboard and run `python remediate_db.py` once (`--dry-run` first to see
what it would do). It makes a backup copy, stores each control title once, drops wide per-row text, re-matches every
import with the current rules (your saved decisions are kept) and compacts the file. Until then the app refuses to open
the old database and tells you so. Because the matching key changed, some earlier pairings may differ afterwards.

Try it with fake data: `python make_sample_data.py`, then import the three files in `samples/`.
Checks: `test_charts.py`, `test_core.py`, `test_import.py`, `test_names.py`, `test_matching.py`, `test_remediate.py`, `smoke_app.py` (runs
the UI script against a stand-in Streamlit; it does not check real rendering). Timing: `bench.py` (about 150k rows),
`bench_wide.py` (62k rows, ~190 MB file: about 7 s and ~100-200 MB of memory per import).

## Versions
Every file carries a `Version: X.Y.Z` line (the JSON config has a `_version` key). The sidebar shows the program
version, the Debug tab lists each file's version on disk and warns if any differ (a quick way to tell whether an update
was copied over completely), and the app logs the versions at start-up. `python remediate_db.py` prints it too.
Release 1.3.2: the Imports & rules tab shows the exact device-names file in use and warns about other copies. Release 1.3.1: Streamlit 1.67 `width="stretch"` replaces `use_container_width`. Release 1.3.0: after a successful import the inputs clear (so the finished file is no longer re-checked and flagged as
"already imported"); 100% stacked now works from the defaults; stacked status charts are ordered and coloured by severity.

## Importing
**Large files.** Files are streamed row by row, never loaded whole. Browser uploads are capped at 200 MB by Streamlit;
either type the file's path into the Import tab ("File on this computer", no limit), use
`python import_cli.py export.csv --framework "NIST CSF 2.0" --date 2026-10-01` (add `--check` to only run the checks), or
start with `streamlit run app.py --server.maxUploadSize 2000`.

**Columns.** Columns the app doesn't recognize are *ignored by default* (that is where the very wide text usually is).
The Import tab lists them with the average characters per row; choose ignore, keep as an extra column, or map to a field.
Decisions are remembered. The BPC Title is stored once per control (it is 1:1 with Bpc Id), not on every row.

The Import tab runs three separate checks before anything is stored:
1. **Column structure**: identical to a previous import, same columns reordered, or changed (lists what was added or
   removed). Headers are matched by name, never position. Unknown headers are mapped once (saved) or kept as extra
   columns you can still filter and group by.
2. **Is this data already stored?** Detects the same file *and* a re-saved copy with identical data (compares what the
   rows mean, not the bytes). Blocked unless you tick "Import it again anyway".
3. **Different data for the same as-of date?** You are asked every time: replace the existing import(s) or keep both.

As-of date is set per import (guessed from a date in the file name). Matching follows as-of order, so back-dated
imports are fine. `config/field_map.json` holds header aliases, the matching tiers, and the known export formats
(v1 first seen 2026-08-13, v2 first seen 2026-10-01; a v2-shaped file dated before 10-01 gets a warning).

## Matching
A line is **Device Serial (the 14-16 digit part) + Bpc Id + Object Id + Location**. That is the only truly unique key.
1. If one line in the old export and one in the new share that key, they are the same line, even if the remediation
   location text changed.
2. If several lines share a key, they are paired by **remediation location** (the part after tsgid/Device/Manager).
   Leftovers with no exact remediation match are never paired by order: the old ones count as removed and the new as added.
3. **Renamed remediation sections** are only *suggested*: the same `label:value` section must change between the same two
   values on at least 2 lines on at least 2 different devices (`rename_suggestions` in `config/field_map.json`). A lone
   change is treated as removed and re-added. Adding or removing a whole section is not a rename. Accepting saves a rule.
4. **Replaced devices** are only suggested when a vanished serial's lines largely reappear under a new serial: by default
   at least 70% of the old device's lines and 50% of the new device's lines must match (`alias_suggestions`). Devices with
   fewer than 200 lines need 95% both ways, since small devices look alike. Names (see below) only break ties or, with no
   better candidate, are offered on their own.
5. A line that disappears and later returns keeps its history; it simply isn't plotted where it is absent.

The Review queue handles lines that disappeared without explanation: mark a serial replaced or retired, accept a
remediation rename, retire lines, or link two lines. Decisions are saved as rules keyed by values (not ids), so any
rebuild re-applies them. New imports that are chronologically last are matched incrementally; back-dated imports,
replacements, and rule changes trigger a full rematch.

## Trends
Controls sit in a form: nothing recalculates until you press **Update chart**.
- **Measure**: % complete, lines, pass, fail, exception, excluded (flagged), or status breakdown.
- **Group by** any field (severity, device type/serial/group, object type, control, control group, location, extra columns).
- **Stack**: off, by group, by status, or both; optional **100% stacked** (segments add to 100% of lines; tooltips show the
  line count and the %; ticking it from the defaults switches Measure to Status breakdown and Stack to by status, or by
  group and status when a Group by is set, and says so). Stacked status charts run bottom to top fail, exception, pass,
  other; when grouped by severity each status is split Critical to Warning (darkest = most severe; red fail, amber
  exception, blue pass); optional facets by group. A stacked line is
  drawn as a stacked area. Combinations that don't make sense adjust themselves, with a note.
- **Exceptions** and **User-excluded lines** can be left out of the %, counted, or (excluded) included. Excluded lines
  are left out of % by default but always available as counts.
- Filters, import selection, "only lines present in every import", and a group limit that folds the rest into "(other)".

### Saved views and speed
- Save any set of options as a named view (per framework; export/import as a `.json` file).
- A saved view's computed chart data is stored in the database with a **data version** (hash of imports + rules).
  Opening it reuses the stored result; after an import or rule change it is recomputed once on next open
  (or press *Refresh all saved views*). Unsaved combinations are only held in memory.
- The loaded data is cached in memory per data version; each chart recalculation is a single group-by on a small
  count table. At ~150k rows: load about 1.3 s once, each recalculation about 0.05 s, rematch after a rule change about 3 s.

## Device names
Tie friendly names to serials in `data/device_names.csv` (created empty on first start; edit it in Excel or any editor).
Columns: `serial`, `name`, `notes`. The file lives in `data/` so program updates never overwrite it, and it is re-read
automatically when it changes. Format the serial column as **Text** (Excel turns long numbers into scientific notation;
the app warns if it sees that).
- Names are display-only: never used for matching, never stored in the database, never written to the debug log.
- Shown as `serial (name)` in the review queue, in the serial filter, and in chart legends when grouping by device serial.
- In the review queue, a replaced-device suggestion marks **same name**. A shared name raises a candidate's rank when
  they also share lines; a name match with no shared lines is offered only when nothing else qualifies. Names don't
  need to be unique, and nothing is ever accepted automatically.
- Imports & rules tab: shows loaded names and file problems, and downloads a template of serials that have no name yet.
- If a serial is listed twice, the last row is used.

## Debug
The Debug tab shows recent step timings, the log tail, and lets you switch INFO/DEBUG, download, or clear the log.
Serials are masked to the last 4 digits; Object Ids and remediation paths are never written to the log.

## Known limits
- "Keep both" for the same date puts two points on that date.
- A full rematch replays every import, so its time grows with total rows (about 3 s per 150k).
- Defaults are tuned to the v1/v2 schemas you provided; edit `tiers` in the config if Palo Alto adds fields.
- The dashboard hasn't been run under real Streamlit/Plotly in the environment it was built in.
