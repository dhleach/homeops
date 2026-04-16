import json
import logging
import sys
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(log_record)


def get_logger(service_name: str) -> logging.Logger:
    logger = logging.getLogger(service_name)
    if (
        not logger.handlers
    ):  # Avoid adding duplicate handlers if get_logger is called multiple times
        handler = logging.StreamHandler(sys.stderr)
        formatter = JSONFormatter()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
