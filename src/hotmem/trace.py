"""HotMem tracing — structured, component-tagged logging for agent observability.

Purpose:
    Provide structured JSON-line logs to stderr so agents can grep by component
    and correlate operations across the sidecar lifecycle.

Interface:
    get_tracer(component: str) -> Tracer
    new_trace_id() -> str

Deps: none (stdlib only)
Extension: swap the formatter or add sinks (file, HTTP) by subclassing Tracer.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid


def new_trace_id() -> str:
    """Generate a short unique trace ID."""
    return uuid.uuid4().hex[:12]


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "component": getattr(record, "component", "unknown"),
            "level": record.levelname,
            "op": getattr(record, "op", ""),
            "msg": record.getMessage(),
        }
        detail = getattr(record, "detail", None)
        if detail is not None:
            entry["detail"] = detail
        trace_id = getattr(record, "trace_id", None)
        if trace_id is not None:
            entry["trace_id"] = trace_id
        return json.dumps(entry, default=str)


class Tracer:
    """Component-scoped structured logger.

    Usage:
        tracer = get_tracer("db")
        tracer.info("init", "database opened", detail={"path": "/tmp/hotmem.sqlite"})
    """

    def __init__(self, component: str) -> None:
        self.component = component
        self._logger = logging.getLogger(f"hotmem.{component}")
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(_JsonFormatter())
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.DEBUG)
            self._logger.propagate = False

    def _log(
        self,
        level: int,
        op: str,
        msg: str,
        *,
        detail: dict | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._logger.log(
            level,
            msg,
            extra={
                "component": self.component,
                "op": op,
                "detail": detail,
                "trace_id": trace_id,
            },
        )

    def info(
        self, op: str, msg: str, *, detail: dict | None = None, trace_id: str | None = None
    ) -> None:
        self._log(logging.INFO, op, msg, detail=detail, trace_id=trace_id)

    def debug(
        self, op: str, msg: str, *, detail: dict | None = None, trace_id: str | None = None
    ) -> None:
        self._log(logging.DEBUG, op, msg, detail=detail, trace_id=trace_id)

    def error(
        self, op: str, msg: str, *, detail: dict | None = None, trace_id: str | None = None
    ) -> None:
        self._log(logging.ERROR, op, msg, detail=detail, trace_id=trace_id)

    def warn(
        self, op: str, msg: str, *, detail: dict | None = None, trace_id: str | None = None
    ) -> None:
        self._log(logging.WARNING, op, msg, detail=detail, trace_id=trace_id)


_tracers: dict[str, Tracer] = {}


def get_tracer(component: str) -> Tracer:
    """Get or create a tracer for the given component name."""
    if component not in _tracers:
        _tracers[component] = Tracer(component)
    return _tracers[component]


class Timer:
    """Context manager that measures elapsed milliseconds."""

    def __init__(self) -> None:
        self.ms: float = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        self.ms = (time.perf_counter() - self._start) * 1000
