"""Logging for services run by systemd.

Everything goes to stdout through the standard `logging` module. Under systemd
each line starts with "<N>", which the journal reads as the priority, so
`journalctl -u <unit> -p warning` works. Run by hand, lines are plain text
with the time and level.

    from radar_runner.common.log import setup_logging
    setup_logging("info")
    log = logging.getLogger("telemetry")
    log.warning("OLA not answering")
"""
import logging
import os
import sys

# syslog priorities, as sd-daemon(3) defines them
_PRIORITY = ((logging.CRITICAL, 2), (logging.ERROR, 3), (logging.WARNING, 4),
             (logging.INFO, 6), (logging.DEBUG, 7))
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
          "error": logging.ERROR}


class JournalFormatter(logging.Formatter):
    """Prefixes every line, including each line of a traceback, with its priority."""

    def format(self, record):
        priority = next((p for level, p in _PRIORITY if record.levelno >= level), 7)
        text = super().format(record)
        return "\n".join(f"<{priority}>{line}" for line in text.split("\n"))


class _Handler(logging.StreamHandler):
    def handleError(self, record):
        pass    # a broken stdout must not stop the service or flood stderr


def setup_logging(level="info", *, stream=None, environ=os.environ):
    """Send all logging to stdout (or `stream`). Returns a warning text, or "".

    Safe to call again: it replaces the handler it installed before.
    """
    under_systemd = bool(environ.get("JOURNAL_STREAM"))
    handler = _Handler(stream if stream is not None else sys.stdout)
    if under_systemd:
        handler.setFormatter(JournalFormatter("%(name)s: %(message)s"))   # the journal adds the time
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                               datefmt="%H:%M:%S"))
    root = logging.getLogger()
    for old in [h for h in root.handlers if isinstance(h, _Handler)]:
        root.removeHandler(old)
    root.addHandler(handler)
    problem = ""
    if level not in LEVELS:
        problem = f"unknown logging level {level!r}: using info"
    root.setLevel(LEVELS.get(level, logging.INFO))
    logging.captureWarnings(True)     # warnings.warn() from libraries, e.g. gpiozero

    def excepthook(kind, value, traceback):
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, traceback)
        else:
            root.critical("uncaught exception", exc_info=(kind, value, traceback))
    sys.excepthook = excepthook
    return problem
