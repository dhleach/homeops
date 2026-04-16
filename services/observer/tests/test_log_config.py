"""Tests for services/observer/log_config.py."""

import io
import json
import logging
import sys
from pathlib import Path

# Make the observer package importable from this test file.
sys.path.insert(0, str(Path(__file__).parent.parent))

from log_config import get_logger  # noqa: E402


def _capture_log_output(logger: logging.Logger, message: str) -> str:
    """Emit *message* at INFO level and return the raw string written to stderr."""
    buf = io.StringIO()
    # Temporarily replace the handler's stream with our buffer.
    handler = logger.handlers[0]
    old_stream = handler.stream
    handler.stream = buf
    try:
        logger.info(message)
    finally:
        handler.stream = old_stream
    return buf.getvalue().strip()


def test_get_logger_returns_logger():
    """get_logger should return a standard logging.Logger instance."""
    logger = get_logger("test_returns")
    assert isinstance(logger, logging.Logger)


def test_output_is_valid_json():
    """A log call should produce output that is valid JSON."""
    logger = get_logger("test_json")
    raw = _capture_log_output(logger, "hello json")
    parsed = json.loads(raw)  # raises if invalid JSON
    assert isinstance(parsed, dict)


def test_required_fields_present():
    """Each log record must include timestamp, level, service, and message."""
    logger = get_logger("test_fields")
    raw = _capture_log_output(logger, "checking fields")
    parsed = json.loads(raw)
    assert "timestamp" in parsed, "missing 'timestamp'"
    assert "level" in parsed, "missing 'level'"
    assert "service" in parsed, "missing 'service'"
    assert "message" in parsed, "missing 'message'"
    assert parsed["service"] == "test_fields"
    assert parsed["message"] == "checking fields"
