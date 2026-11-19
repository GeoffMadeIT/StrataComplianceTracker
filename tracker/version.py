# Version: 1.3.2
"""Project and per-file versions.

Every project file carries a line `# Version: X.Y.Z` (Markdown/text files: `Version: X.Y.Z`; the JSON config: a
"_version" key). The Debug tab lists what is actually on disk, so you can see at a glance whether every file
belongs to the same release. When a file changes, bump its own version; bump VERSION here for each release.
"""
import json
import re
from pathlib import Path

VERSION = "1.3.2"
ROOT = Path(__file__).resolve().parent.parent
_PAT = re.compile(r"^\s*(?:#\s*|<!--\s*)?Version:\s*([0-9][0-9A-Za-z.\-]*)", re.M)
FILES = ["app.py", "import_cli.py", "remediate_db.py", "requirements.txt", "README.md", "config/field_map.json",
         "tracker/core.py", "tracker/analysis.py", "tracker/charts.py", "tracker/views.py", "tracker/names.py",
         "tracker/log.py", "tracker/version.py"]


def file_versions(root=ROOT):
    """[(file, version or None)] as found on disk."""
    out = []
    for f in FILES:
        p = Path(root) / f
        v = None
        if p.exists():
            head = p.read_text(encoding="utf-8", errors="replace")[:600]
            if f.endswith(".json"):
                try:
                    v = json.loads(p.read_text(encoding="utf-8")).get("_version")
                except ValueError:
                    v = None
            else:
                m = _PAT.search(head)
                v = m.group(1) if m else None
            out.append((f, v))
        else:
            out.append((f, "missing"))
    return out
