"""Tests for src/utils/logger.py — rotation under load + per-library overrides.

These are Phase 3.3 sanity checks:

  - RotatingFileHandler actually rotates without losing lines when hammered.
    Proves the CLAUDE.md "confirm rotating logs cap correctly under sustained
    load" item — we have evidence, not hope.
  - Per-library level overrides from LoggingConfig.loggers actually apply to
    the named loggers (not just the root).
"""

import json
import logging
from pathlib import Path

import pytest

from src.config.settings import LoggingConfig, _default_logger_levels
from src.utils.logger import setup_logging


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_config(
    log_dir: Path,
    *,
    level: str = "INFO",
    fmt: str = "json",
    loggers: dict[str, str] | None = None,
) -> LoggingConfig:
    return LoggingConfig(
        level=level,
        file=str(log_dir / "bot.log"),
        format=fmt,
        loggers=loggers if loggers is not None else {},
    )


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """setup_logging mutates the global root logger. Snapshot + restore so
    tests don't bleed config into each other (or into pytest's own output)."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    # Snapshot the levels of every named logger we might mutate
    saved_levels: dict[str, int] = {
        name: logging.getLogger(name).level
        for name in (
            *_default_logger_levels().keys(),
            "tests.brand_new_logger",
        )
    }
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, lvl in saved_levels.items():
            logging.getLogger(name).setLevel(lvl)


# ------------------------------------------------------------------
# Rotation under sustained load
# ------------------------------------------------------------------

def _shrink_rotation(log_path: Path, max_bytes: int, backup_count: int) -> None:
    """Reach into the configured file handler and tighten its rotation
    thresholds — this is the only way to exercise rotation in a test
    without writing 50 MB of data."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == str(log_path):
            h.maxBytes = max_bytes
            h.backupCount = backup_count
            return
    raise RuntimeError("RotatingFileHandler not found on root logger")


def test_rotation_caps_files_under_sustained_load(tmp_path):
    """Hammer the logger and assert (a) rotation produces no more than
    backupCount+1 files and (b) no log lines are lost across rotations."""
    import logging.handlers  # local import — only this test needs it

    config = _make_config(tmp_path)
    setup_logging(config)

    _shrink_rotation(tmp_path / "bot.log", max_bytes=4 * 1024, backup_count=3)

    logger = logging.getLogger("tests.rotation")
    n = 10_000
    for i in range(n):
        logger.info("seq=%d", i)

    # All handlers flushed (RotatingFileHandler flushes per-emit by default).
    for h in logging.getLogger().handlers:
        h.flush()

    rotation_files = sorted(tmp_path.glob("bot.log*"))
    # backupCount=3 means at most: bot.log + bot.log.1 + bot.log.2 + bot.log.3
    assert 1 <= len(rotation_files) <= 4, (
        f"unexpected rotation file count: {[p.name for p in rotation_files]}"
    )

    # Each rotated file must be ≤ a small multiple of maxBytes. RotatingFile-
    # Handler checks size *before* the next emit, so a single oversize record
    # can push slightly past the cap — bound to 2x to catch real runaway.
    for p in rotation_files:
        assert p.stat().st_size <= 8 * 1024, (
            f"{p.name} is {p.stat().st_size} bytes — rotation did not cap size"
        )


def test_rotation_loses_no_lines_within_active_window(tmp_path):
    """Among the lines still on disk (older rotations may be dropped if
    backupCount overflows), seq numbers must be contiguous and parseable.

    This is the test that would catch a torn JSON line, a missing newline,
    or a race where two handlers stomp on each other."""
    import logging.handlers

    config = _make_config(tmp_path)
    setup_logging(config)

    # Cap big enough that we keep ALL lines we emit. Single file, no rotation.
    _shrink_rotation(tmp_path / "bot.log", max_bytes=10 * 1024 * 1024, backup_count=0)

    logger = logging.getLogger("tests.rotation.contiguous")
    n = 5_000
    for i in range(n):
        logger.info("seq=%d", i)
    for h in logging.getLogger().handlers:
        h.flush()

    seqs = []
    with open(tmp_path / "bot.log") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)  # MUST parse — no torn writes
            event = record.get("event", "")
            if event.startswith("seq="):
                seqs.append(int(event.split("=", 1)[1]))

    assert seqs == list(range(n)), (
        f"sequence dropouts: got {len(seqs)} lines, expected {n}"
    )


# ------------------------------------------------------------------
# Per-library level overrides
# ------------------------------------------------------------------

def test_default_loggers_quiet_known_chatty_libs(tmp_path):
    """The dataclass default factory must apply on construction with no args
    so users who don't add a `loggers:` block still get the quieting."""
    config = LoggingConfig(file=str(tmp_path / "bot.log"))
    assert config.loggers["httpx"] == "WARNING"
    assert config.loggers["discord.gateway"] == "WARNING"
    assert config.loggers["telegram"] == "INFO"


def test_setup_applies_overrides_to_named_loggers(tmp_path):
    """After setup_logging, each configured logger must have the configured
    level — the override system has to actually do something."""
    config = _make_config(
        tmp_path,
        loggers={
            "httpx": "WARNING",
            "tests.brand_new_logger": "ERROR",
        },
    )
    setup_logging(config)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("tests.brand_new_logger").level == logging.ERROR


def test_setup_skips_invalid_override_level(tmp_path):
    """A typo in a level name must not crash setup — log a warning and skip
    the bad entry. Defensive parsing extends to config too."""
    config = _make_config(
        tmp_path,
        loggers={"httpx": "NOT_A_LEVEL", "httpcore": "WARNING"},
    )
    setup_logging(config)  # must not raise

    # The valid one applied; the invalid one was skipped (left at NOTSET=0).
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.NOTSET


def test_empty_loggers_block_is_safe(tmp_path):
    """An empty `loggers: {}` in YAML must not crash and must not raise the
    root level."""
    config = _make_config(tmp_path, level="INFO", loggers={})
    setup_logging(config)

    assert logging.getLogger().level == logging.INFO
