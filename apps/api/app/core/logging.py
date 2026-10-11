import logging
import re
import sys

# Regex pattern for masking tokens in URLs (e.g. /s/<token>, /shares/<token>, etc.)
PATH_REDACT_REGEX = re.compile(r"(/s/|/shares/)[A-Za-z0-9_\-]+")


def redact_url_path(url_path: str) -> str:
    """Mask tokens in URL paths to prevent secret leakage in logs."""
    return PATH_REDACT_REGEX.sub(r"\1***", url_path)


class RedactingFormatter(logging.Formatter):
    """Logging formatter that sanitizes token strings and URLs from log messages."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return redact_url_path(msg)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure application logging with redaction filters."""
    root_logger = logging.getLogger()
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    # Avoid duplicate handlers if reconfigured
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = RedactingFormatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
    else:
        for handler in root_logger.handlers:
            formatter = RedactingFormatter(
                fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            handler.setFormatter(formatter)

    # Configure uvicorn loggers to use redacting formatter
    for uvicorn_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        for h in uv_logger.handlers:
            h.setFormatter(
                RedactingFormatter(
                    fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
