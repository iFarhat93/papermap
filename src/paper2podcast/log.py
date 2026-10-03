"""Logging setup: concise console output plus a full DEBUG log file per run."""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOGGER_NAME = "paper2podcast"

_COLORS = {"DEBUG": "\033[2m", "INFO": "", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[31;1m"}
_RESET = "\033[0m"


class _ConsoleFormatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", None)
        prefix = f"[{stage}] " if stage else ""
        level = "" if record.levelno == logging.INFO else f"{record.levelname.lower()}: "
        msg = f"{prefix}{level}{record.getMessage()}"
        if record.exc_info and logging.getLogger(LOGGER_NAME).isEnabledFor(logging.DEBUG):
            msg += "\n" + self.formatException(record.exc_info)
        if self.color:
            c = _COLORS.get(record.levelname, "")
            if c:
                msg = f"{c}{msg}{_RESET}"
        return msg


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def setup_logging(verbose: bool = False, quiet: bool = False, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO)
    color = sys.stderr.isatty()
    console.setFormatter(_ConsoleFormatter(color))
    logger.addHandler(console)
    if log_file:
        add_file_handler(log_file)
    logger.propagate = False
    return logger


def add_file_handler(log_file: Path) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(stage)s| %(message)s", defaults={"stage": ""}))
    logger.addHandler(fh)


class StageAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):  # type: ignore[override]
        kwargs.setdefault("extra", {})["stage"] = self.extra["stage"]  # type: ignore[index]
        return msg, kwargs


def stage_logger(stage: str) -> StageAdapter:
    return StageAdapter(get_logger("stage"), {"stage": stage})


@contextmanager
def timed(log: logging.Logger | logging.LoggerAdapter, what: str) -> Iterator[None]:
    t0 = time.perf_counter()
    yield
    log.debug("%s took %.2fs", what, time.perf_counter() - t0)
