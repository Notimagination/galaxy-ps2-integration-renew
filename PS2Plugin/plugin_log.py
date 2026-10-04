"""Writes the plugin log to PS2Plugin.log.

Galaxy creates its own log file named plugin-<platform>-<guid>.log. That name is
derived from the manifest (platform + guid), so it cannot be renamed from inside
the plugin without changing the guid and losing the stored library. Instead the
plugin opens PS2Plugin.log next to it and, unless told otherwise, stops writing
to (and removes) Galaxy's file so only one log exists.

config.ini:
    [Logging]
    keep_galaxy_log = False   ; True = keep Galaxy's own log file as well
"""
import logging
import logging.handlers
import os

import config

LOG_FILE_NAME = "PS2Plugin.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 2
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class QuietKeepAliveFilter(logging.Filter):
    """Drop Galaxy's keep-alive chatter (a ping every 5 s and its empty reply)."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "method=ping" in message:
            return False
        return not (message.startswith("Sending data:") and message.endswith('"result": null}'))


def _fallback_dir():
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        candidate = os.path.join(program_data, "GOG.com", "Galaxy", "logs")
        if os.path.isdir(candidate):
            return candidate
    return os.path.expandvars(config.PLUGIN_DIR)


def setup_logging(keep_galaxy_log=False):
    """Attach PS2Plugin.log to the root logger. Returns the handler, or None on failure."""
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_ps2plugin", False):
            return handler

    galaxy_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    template = galaxy_handlers[0] if galaxy_handlers else None
    directory = os.path.dirname(template.baseFilename) if template else _fallback_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(directory, LOG_FILE_NAME),
            mode="w", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8",
        )
    except OSError:
        return None  # Galaxy's own handler is left untouched

    handler.setLevel(template.level if template else logging.DEBUG)
    handler.setFormatter(template.formatter if template and template.formatter else logging.Formatter(DEFAULT_FORMAT))
    handler.addFilter(QuietKeepAliveFilter())
    handler._ps2plugin = True
    root.addHandler(handler)

    if galaxy_handlers and not keep_galaxy_log:
        for old in galaxy_handlers:
            root.removeHandler(old)
            path = old.baseFilename
            try:
                old.close()
                os.remove(path)
            except OSError:
                pass
    return handler
