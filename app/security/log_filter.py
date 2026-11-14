"""Logging filter that automatically sanitizes every log record through AllBoundaryDLP."""

from __future__ import annotations

import logging

from app.security.dlp import AllBoundaryDLP


class SanitizingLogFilter(logging.Filter):
    """Ensure no secret or patient PII is ever written to stdout/file logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = AllBoundaryDLP.sanitize_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: AllBoundaryDLP.sanitize_obj(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(AllBoundaryDLP.sanitize_obj(a) for a in record.args)
        return True


def install_global_log_sanitizer() -> None:
    """Attach SanitizingLogFilter to root logger and common framework loggers idempotently."""
    root_logger = logging.getLogger()
    if not any(isinstance(f, SanitizingLogFilter) for f in root_logger.filters):
        root_logger.addFilter(SanitizingLogFilter())
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "sqlalchemy"):
        lg = logging.getLogger(logger_name)
        if not any(isinstance(f, SanitizingLogFilter) for f in lg.filters):
            lg.addFilter(SanitizingLogFilter())
