# Version: 1.3.2
"""Device names: an optional CSV that ties friendly names to serials.

The file lives in data/device_names.csv (so program updates never overwrite it) and is edited outside the app, in
Excel or any editor. Columns: serial, name, notes (extra columns are ignored). Names are display-only: they are never
used to match lines, never stored in the database, and never written to the debug log. They are used to label
serials in the review queue, filters and charts, and to rank replaced-device suggestions.
"""
import csv
import re
from pathlib import Path

from . import core
from .log import log

NAMES_PATH = Path(__file__).resolve().parent.parent / "data" / "device_names.csv"
TEMPLATE = "serial,name,notes\n"
SERIAL_HEADERS = {"serial", "serialnumber", "deviceserial", "serialno"}
NAME_HEADERS = {"name", "devicename", "hostname", "friendlyname", "device"}
_SCI = re.compile(r"^\d(\.\d+)?[eE]\+?\d+$")
_cache = {}


class Names(dict):
    """serial -> name, plus .warnings (problems found in the file), .path, and .rows_read / .size / .others."""
    warnings: list
    path: Path
    rows_read: int = 0
    size: int = 0
    others: list


def find_other_files(path):
    """Other device_names*.csv files near the one in use: the usual cause of 'I edited it but nothing changed'."""
    path = Path(path).resolve()
    spots = {path.parent, path.parent.parent, Path.cwd(), Path.cwd() / "data"}
    found = []
    for d in spots:
        try:
            for f in d.glob("device_names*.csv"):
                if f.resolve() != path and str(f.resolve()) not in found:
                    found.append(str(f.resolve()))
        except OSError:
            pass
    return sorted(found)


def get(path=None):
    """Load the names file (cached until the file changes). Creates an empty template if it doesn't exist."""
    path = Path(path or NAMES_PATH)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE, encoding="utf-8")
    stamp = path.stat().st_mtime_ns
    hit = _cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    out = Names()
    out.path, out.warnings, out.size, out.rows_read = path, [], stamp and path.stat().st_size, 0
    out.others = find_other_files(path)
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            hmap = {core.norm_header(h): h for h in (reader.fieldnames or [])}
            sh = next((hmap[k] for k in hmap if k in SERIAL_HEADERS), None)
            nh = next((hmap[k] for k in hmap if k in NAME_HEADERS), None)
            if not sh or not nh:
                out.warnings.append("The file needs a 'serial' column and a 'name' column in its header row.")
            else:
                for n, row in enumerate(reader, start=2):
                    out.rows_read += 1
                    raw, name = (row.get(sh) or "").strip(), (row.get(nh) or "").strip()
                    if not raw and not name:
                        continue
                    if _SCI.match(raw):
                        out.warnings.append(f"Line {n}: serial {raw!r} looks like Excel turned it into scientific "
                                            "notation. Format the serial column as Text and re-enter it.")
                        continue
                    key = core.split_serial(raw)[0]
                    if not key or not name:
                        out.warnings.append(f"Line {n}: needs both a serial and a name; skipped.")
                        continue
                    if key in out:
                        out.warnings.append(f"Line {n}: serial listed more than once; the last row is used.")
                    out[key] = name
    except Exception as e:                       # noqa: BLE001 - a bad names file must never break the app
        out.warnings.append(f"Could not read the file: {e}")
        log.exception("device names file could not be read")
    if not out.warnings and not out and out.rows_read == 0:
        out.warnings.append("The file has a header row but no names yet. If you have been adding names to a different "
                            "copy of the file, see the path above.")
    _cache[path] = (stamp, out)
    log.info("loaded %d device names from %s (%d rows, %d bytes, %d warnings%s)", len(out), path, out.rows_read, out.size,
             len(out.warnings), f"; other device_names files nearby: {len(out.others)}" if out.others else "")
    return out


def label(serial, names):
    """'serial (name)' when a name is known, else the serial."""
    n = names.get(serial) if names else None
    return f"{serial} ({n})" if n else str(serial)


def label_series(value, names):
    """Label a chart series/group value that is a serial, optionally followed by ' · <status>'."""
    if not names or not isinstance(value, str):
        return value
    head, sep, tail = value.partition(" · ")
    return label(head, names) + sep + tail


def template_for(serials, names):
    """CSV text listing serials that have no name yet, ready to fill in."""
    lines = ["serial,name,notes"] + [f"{s}," for s in sorted(set(serials)) if s not in names]
    return "\n".join(lines) + "\n"
