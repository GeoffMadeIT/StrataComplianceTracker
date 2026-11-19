# Version: 1.3.2
"""Debug logging with serial masking and an in-memory timing buffer.

- Log file: data/debug.log (rotating, 1 MB x 3). Level defaults to INFO; switch to DEBUG in the Debug tab.
- Any 14-16 digit run (device serials, also inside remediation paths) is masked to its last 4 digits.
- Object Ids and Remediation Locations are never logged; only counts, bpc ids and timings.
"""
import collections
import contextlib
import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "debug.log"
TIMINGS = collections.deque(maxlen=500)      # (iso time, label, seconds, context string)
_SERIAL = re.compile(r"\d{14,16}")


def mask(text):
    return _SERIAL.sub(lambda m: "…" + m.group()[-4:], str(text))


class _MaskFilter(logging.Filter):
    def filter(self, record):
        record.msg = mask(record.getMessage())
        record.args = ()
        return True


def get_logger():
    logger = logging.getLogger("tracker")
    if not logger.handlers:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        h.addFilter(_MaskFilter())
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


log = get_logger()


def set_level(name):
    log.setLevel(getattr(logging, name.upper(), logging.INFO))
    log.info("log level set to %s", name.upper())


def level_name():
    return logging.getLevelName(log.level)


@contextlib.contextmanager
def timed(label, **ctx):
    """Log and record how long a block takes. Failures are logged with a traceback and re-raised."""
    t0 = time.perf_counter()
    cs = " ".join(f"{k}={v}" for k, v in ctx.items())
    log.debug("start %s %s", label, cs)
    try:
        yield
    except Exception:
        log.exception("FAILED %s %s", label, cs)
        raise
    finally:
        dt_ = time.perf_counter() - t0
        TIMINGS.append((time.strftime("%H:%M:%S"), label, round(dt_, 3), mask(cs)))
        log.info("%s %s took %.3fs", label, cs, dt_)


def tail(n=300):
    if not LOG_PATH.exists():
        return ""
    return "".join(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-n:])


def clear():
    for h in log.handlers:
        h.flush()
    LOG_PATH.write_text("", encoding="utf-8")
    log.info("log cleared")
