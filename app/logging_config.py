"""
app/logging_config.py
---------------------
Centralised logging configuration for Project Maestro.

Call configure_logging() once at application startup (in main.py).
All other modules just use logging.getLogger(__name__) as normal.
"""

import logging
import logging.handlers
import os

# Endpoints polled at high frequency by the frontend — suppress from access log
_NOISY_PATHS = (
    "/api/scheduler/status",
    "/api/projects/",   # catches /api/projects/{name}/tasks auto-refresh
)


class _AccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in _NOISY_PATHS)


def configure_logging(
    level: str = "INFO",
    log_file: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """
    Configure the root logger with a stream handler and optional rotating
    file handler.

    Args:
        level:        Log level string (DEBUG / INFO / WARNING / ERROR).
        log_file:     Path to the rotating log file.  None disables file logging.
        max_bytes:    Maximum size of a single log file before rotation.
        backup_count: Number of rotated backup files to keep.
    """
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Avoid adding duplicate handlers if called more than once
    if not root.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if log_file and not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Suppress high-frequency frontend poll requests from the uvicorn access log
    logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())
