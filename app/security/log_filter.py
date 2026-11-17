"""Logging filter & formatter sanitizing every log record through AllBoundaryDLP."""

from __future__ import annotations

import logging
from typing import Any

from app.security.dlp import AllBoundaryDLP


class SanitizingLogFormatter(logging.Formatter):
    """Wraps an existing formatter to sanitize 100% of emitted log text and tracebacks."""

    def __init__(self, original_formatter: logging.Formatter | None = None) -> None:
        self.orig_formatter = original_formatter
        super().__init__()

    def format(self, record: logging.LogRecord) -> str:
        if self.orig_formatter is not None:
            formatted = self.orig_formatter.format(record)
        else:
            formatted = super().format(record)
        return AllBoundaryDLP.sanitize_text(formatted)


class SanitizingLogFilter(logging.Filter):
    """Ensure no secret or PII is ever written to stdout/file logs, even across child loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = AllBoundaryDLP.sanitize_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: AllBoundaryDLP.sanitize_obj(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(AllBoundaryDLP.sanitize_obj(a) for a in record.args)
        if record.exc_text:
            record.exc_text = AllBoundaryDLP.sanitize_text(record.exc_text)
        return True


_SANITY_HOOK_INSTALLED = False


def install_global_log_sanitizer() -> None:
    """Attach SanitizingLogFormatter and SanitizingLogFilter idempotently."""
    global _SANITY_HOOK_INSTALLED
    root_logger = logging.getLogger()

    # 1. Attach filter to root logger
    if not any(isinstance(f, SanitizingLogFilter) for f in root_logger.filters):
        root_logger.addFilter(SanitizingLogFilter())

    # 2. Attach filter and wrap formatter on all root handlers
    for h in root_logger.handlers:
        if not any(isinstance(f, SanitizingLogFilter) for f in h.filters):
            h.addFilter(SanitizingLogFilter())
        if not isinstance(h.formatter, SanitizingLogFormatter):
            h.setFormatter(SanitizingLogFormatter(h.formatter))

    # 3. Add to known framework and application loggers
    for logger_name in (
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "fastapi",
        "sqlalchemy",
        "sqlalchemy.engine",
        "app",
        "app.workflow",
        "app.workflow.worker",
        "app.adapters",
        "app.kb",
    ):
        lg = logging.getLogger(logger_name)
        if not any(isinstance(f, SanitizingLogFilter) for f in lg.filters):
            lg.addFilter(SanitizingLogFilter())
        for h in lg.handlers:
            if not any(isinstance(f, SanitizingLogFilter) for f in h.filters):
                h.addFilter(SanitizingLogFilter())
            if not isinstance(h.formatter, SanitizingLogFormatter):
                h.setFormatter(SanitizingLogFormatter(h.formatter))

    # 4. Set LogRecordFactory so child logger records have sanitized message immediately
    if not _SANITY_HOOK_INSTALLED:
        orig_factory = logging.getLogRecordFactory()

        def _sanitizing_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            rec = orig_factory(*args, **kwargs)
            if isinstance(rec.msg, str):
                rec.msg = AllBoundaryDLP.sanitize_text(rec.msg)
            return rec

        logging.setLogRecordFactory(_sanitizing_factory)
        _SANITY_HOOK_INSTALLED = True
