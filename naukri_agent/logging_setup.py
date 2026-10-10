"""
Structured logging.

Every module in the agent does `log = get_logger(__name__)` and then emits
key/value events (`log.info("apply.success", job_id=...)`) rather than formatted
sentences. That is a deliberate choice:

- A run produces hundreds of events across six subsystems. Key/value events are
  greppable (`grep apply.no_confirmation`) and machine-parsable, so the same
  logs drive both debugging and any future dashboard.
- `bind_context()` uses structlog's contextvars, so `run_id`, `account`,
  `profile` and `job_id` are attached to EVERY subsequent event automatically.
  Without it each call site would have to re-pass them and would forget.
- structlog is routed THROUGH stdlib logging (`ProcessorFormatter`) instead of
  printing directly. That is what makes Playwright's, asyncpg's and APScheduler's
  own log records come out in the same shape as ours, and it gives the rotating
  file handler for free.
- Two renderers: a human console renderer for interactive/CLI use, and JSON for
  containers (`LOG_JSON=true`), where the log driver ships lines to a collector.
- A rotating file handler is added when a log directory is writable, so a crashed
  scheduled run leaves evidence even if the container's stdout was lost. Failing
  to open that file is never fatal.

`setup_logging()` is idempotent: the CLI calls it once per process, but tests and
ad-hoc imports can call `get_logger()` with no setup at all.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

_configured = False

# Chatty third-party loggers. asyncio's debug output and httpx's per-request
# INFO line would otherwise dominate a run's logs.
_NOISY_LOGGERS = (
    "asyncio",
    "httpx",
    "httpcore",
    "hpack",
    "apscheduler.executors.default",
    "apscheduler.scheduler",
)


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def setup_logging(
    level: str = "INFO",
    json_logs: bool = False,
    log_dir: Path | str | None = None,
    *,
    force: bool = False,
) -> None:
    """Configure structlog + stdlib logging once per process."""
    global _configured
    if _configured and not force:
        return

    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    shared = _shared_processors()

    if json_logs:
        # format_exc_info turns exc_info into a string field; ConsoleRenderer
        # does its own (prettier) traceback handling, so it is JSON-only.
        renderer: Any = structlog.processors.JSONRenderer()
        final: list[Any] = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ]
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        final = [structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer]

    structlog.configure(
        processors=[
            *shared,
            # Hands the event dict to the stdlib formatter below instead of
            # rendering here, so foreign log records go through the same chain.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=final,
        foreign_pre_chain=shared,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    root.setLevel(numeric_level)

    if log_dir:
        try:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                directory / "agent.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            # The file is for post-mortems, so never colourise it.
            file_handler.setFormatter(
                structlog.stdlib.ProcessorFormatter(
                    processors=[
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.processors.format_exc_info,
                        structlog.processors.JSONRenderer(),
                    ],
                    foreign_pre_chain=shared,
                )
            )
            root.addHandler(file_handler)
        except OSError as exc:  # read-only mount, missing volume: not fatal
            root.warning("could not open log file in %s: %s", log_dir, exc)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric_level, logging.WARNING))

    _configured = True


def get_logger(name: str = "naukri_agent") -> Any:
    return structlog.get_logger(name)


def bind_context(**values: Any) -> None:
    """Attach key/values to every event emitted later in this task/thread."""
    structlog.contextvars.bind_contextvars(**values)


def unbind_context(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
