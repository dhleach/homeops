"""Centralized structured logging configuration for HomeOps services.

Usage:
    from log_config import get_logger
    logger = get_logger("observer")
    logger.info("Auth OK")
"""

import json
import logging
import sys
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "timestamp": ts,
            "level": record.levelname,
            "service": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload)


def get_logger(service_name: str) -> logging.Logger:
    """Return a Logger configured to write JSON lines to stderr.

    Calling this multiple times with the same *service_name* returns the same
    logger (standard Python logger singleton behaviour), so it is safe to call
    at module level.
    """
    logger = logging.getLogger(service_name)

    # Only add a handler once — avoid duplicate output if called multiple times.
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # Prevent the root logger from also emitting these records.
        logger.propagate = False

    return logger
