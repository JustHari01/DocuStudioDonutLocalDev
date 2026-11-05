from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
import traceback
from typing import Mapping

from .storage import app_data_dir


_INSTALLED_CHANNELS: set[str] = set()
_INSTALL_LOCK = threading.Lock()

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(/mcp/)[A-Za-z0-9_-]{12,}"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(bearer\s+)[^\s,;]+"), r"\1<REDACTED>"),
    (
        re.compile(
            r"(?i)([\"']?(?:access_token|refresh_token|api_key|flowKey|token|password|secret)"
            r"[\"']?\s*[:=]\s*)(?:[\"'][^\"']*[\"']|[^\s,;}]+)"
        ),
        r"\1<REDACTED>",
    ),
    (re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+"), r"\1<REDACTED>"),
)


def sanitize_diagnostic_text(value: object, *, limit: int = 120_000) -> str:
    """Remove credentials while keeping paths, stages and stack frames useful."""

    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = text[:limit] + "\n[diagnostic detail truncated]"
    return text


class _SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = sanitize_diagnostic_text(rendered)
        record.args = ()
        return True


def format_exception_report(
    context: str,
    error: BaseException,
    *,
    traceback_object: object | None = None,
    metadata: Mapping[str, object] | None = None,
) -> str:
    code = type(error).__name__ or "UnknownError"
    details = [f"[EXCEPTION] context={sanitize_diagnostic_text(context)} type={code}"]
    if metadata:
        fields = " ".join(
            f"{key}={sanitize_diagnostic_text(value, limit=2_000)!r}"
            for key, value in sorted(metadata.items())
        )
        if fields:
            details.append(f"metadata: {fields}")
    tb = traceback_object if traceback_object is not None else error.__traceback__
    details.append(
        "".join(traceback.format_exception(type(error), error, tb)).strip()
    )
    return sanitize_diagnostic_text("\n".join(details))


def record_exception(
    context: str,
    error: BaseException,
    *,
    traceback_object: object | None = None,
    metadata: Mapping[str, object] | None = None,
) -> str:
    report = format_exception_report(
        context,
        error,
        traceback_object=traceback_object,
        metadata=metadata,
    )
    logging.getLogger("docurandom.exception").error(report)
    return report


def install_runtime_diagnostics(channel: str = "application") -> Path:
    """Install bounded process logging and global Python exception hooks."""

    clean_channel = re.sub(r"[^a-z0-9_-]+", "-", str(channel).casefold()).strip("-")
    clean_channel = clean_channel or "application"
    with _INSTALL_LOCK:
        log_dir = app_data_dir() / "Logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{clean_channel}.log"
        if clean_channel in _INSTALLED_CHANNELS:
            return log_path

        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=1,
            encoding="utf-8",
            delay=True,
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)s "
                "pid=%(process)d thread=%(threadName)s "
                "%(name)s %(filename)s:%(lineno)d %(funcName)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        handler.addFilter(_SecretRedactionFilter())
        root_logger = logging.getLogger()
        # Third-party HTTP clients may include request bodies or cookies in
        # DEBUG records. Keep the global floor at INFO and enable DEBUG only
        # for our own namespaces so diagnostic detail never becomes a secret
        # collection mechanism.
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        logging.getLogger("docurandom").setLevel(logging.DEBUG)
        logging.getLogger("flow_server").setLevel(logging.DEBUG)
        logging.captureWarnings(True)

        previous_sys_hook = sys.excepthook

        def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
            record_exception("uncaught-main-thread", exc, traceback_object=tb)
            if previous_sys_hook is not sys_hook:
                previous_sys_hook(exc_type, exc, tb)

        sys.excepthook = sys_hook

        if hasattr(threading, "excepthook"):
            previous_thread_hook = threading.excepthook

            def thread_hook(args: threading.ExceptHookArgs) -> None:
                record_exception(
                    "uncaught-worker-thread",
                    args.exc_value,
                    traceback_object=args.exc_traceback,
                    metadata={"thread": getattr(args.thread, "name", "unknown")},
                )
                if previous_thread_hook is not thread_hook:
                    previous_thread_hook(args)

            threading.excepthook = thread_hook

        previous_unraisable_hook = getattr(sys, "unraisablehook", None)
        if previous_unraisable_hook is not None:

            def unraisable_hook(args: object) -> None:
                error = getattr(args, "exc_value", None)
                if isinstance(error, BaseException):
                    record_exception(
                        "unraisable-python-error",
                        error,
                        traceback_object=getattr(args, "exc_traceback", None),
                    )
                if previous_unraisable_hook is not unraisable_hook:
                    previous_unraisable_hook(args)

            sys.unraisablehook = unraisable_hook

        _INSTALLED_CHANNELS.add(clean_channel)
        logging.getLogger("docurandom.lifecycle").info(
            "runtime diagnostics started version=%s executable=%s cwd=%s",
            _runtime_version(),
            sanitize_diagnostic_text(sys.executable),
            sanitize_diagnostic_text(os.getcwd()),
        )
        return log_path


def _runtime_version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:
        return "unknown"
