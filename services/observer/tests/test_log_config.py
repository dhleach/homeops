"""Tests for log_config.py — structured JSON logging module."""

import io
import json
import logging
import sys
import unittest

sys.path.insert(0, "/tmp/homeops_ralph/services/observer")
from log_config import get_logger


class TestLogConfig(unittest.TestCase):
    def test_get_logger_returns_logger(self):
        """get_logger should return a logging.Logger instance."""
        logger = get_logger("test_service")
        self.assertIsInstance(logger, logging.Logger)

    def test_output_is_valid_json(self):
        """Log output should be valid JSON."""
        buf = io.StringIO()
        logger = get_logger("test_json_output")
        # Replace handler with one writing to our buffer
        logger.handlers.clear()
        handler = logging.StreamHandler(buf)
        from log_config import JSONFormatter

        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.info("test message")
        output = buf.getvalue().strip()
        self.assertTrue(len(output) > 0, "No output produced")
        parsed = json.loads(output)  # Should not raise
        self.assertIsInstance(parsed, dict)

    def test_required_fields_present(self):
        """Log output must contain timestamp, level, service, message fields."""
        buf = io.StringIO()
        logger = get_logger("test_fields")
        logger.handlers.clear()
        handler = logging.StreamHandler(buf)
        from log_config import JSONFormatter

        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.warning("check fields")
        output = buf.getvalue().strip()
        record = json.loads(output)
        self.assertIn("timestamp", record)
        self.assertIn("level", record)
        self.assertIn("service", record)
        self.assertIn("message", record)
        self.assertEqual(record["level"], "WARNING")
        self.assertEqual(record["service"], "test_fields")
        self.assertEqual(record["message"], "check fields")


if __name__ == "__main__":
    unittest.main()
