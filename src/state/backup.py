"""Daily SQLite backup (D9).

Background asyncio task that snapshots the trades DB to a `backups/` directory
once per day at a configured UTC time, then prunes files older than the
retention window.

Uses ``sqlite3.Connection.backup()`` — the SQLite online backup API — so the
pipeline's writers are not blocked while the snapshot is being taken. The
destination file is written under a ``.partial`` suffix and atomically renamed
on success, so no half-written file is ever visible at the final name.

Server-portable by construction: stdlib only, no subprocess, no OS-specific
scheduling. The same code runs unchanged on a VPS — only ``directory:`` in
``config.yaml`` would change.

Same-disk caveat: backups land on the same disk as the source DB. For real
disaster recovery, an offsite copy step (rsync/scp) is the next layer — out
of scope for Phase 3.1.
"""

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------

@dataclass
class BackupConfig:
    """Daily backup configuration (D9)."""

    enabled: bool = True
    directory: str = "backups"
    retention_days: int = 30
    daily_at_utc: str = "06:00"  # HH:MM


def _parse_hhmm(value: str) -> dt_time:
    """Parse 'HH:MM' into a tz-aware datetime.time (UTC). Raises ValueError."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"daily_at_utc must be 'HH:MM', got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"daily_at_utc must be 'HH:MM', got {value!r}") from exc
    if not (0 <= hour < 24) or not (0 <= minute < 60):
        raise ValueError(f"daily_at_utc out of range: {value!r}")
    return dt_time(hour=hour, minute=minute, tzinfo=timezone.utc)


# ------------------------------------------------------------------
# Core operations
# ------------------------------------------------------------------

def perform_backup(
    db_path: Path,
    backup_dir: Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Path:
    """Snapshot ``db_path`` to ``backup_dir/trades-YYYYMMDD-HHMMSS.db``.

    Uses the SQLite online backup API. Writes to a ``.partial`` name first
    and atomically renames on success.

    Raises:
        FileNotFoundError: source DB does not exist.
        sqlite3.Error: backup itself failed.
    """
    if clock is None:
        clock = lambda: datetime.now(timezone.utc)

    if not db_path.exists():
        raise FileNotFoundError(f"Source DB does not exist: {db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)

    now = clock()
    name = f"trades-{now.strftime('%Y%m%d-%H%M%S')}.db"
    final_path = backup_dir / name
    partial_path = backup_dir / (name + ".partial")

    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(partial_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    os.replace(partial_path, final_path)
    return final_path


def prune_old_backups(
    backup_dir: Path,
    retention_days: int,
    *,
    clock: Callable[[], datetime] | None = None,
) -> list[Path]:
    """Delete ``*.db`` files in ``backup_dir`` older than ``retention_days``.

    Pruning is by file mtime — manual renames/copies don't break retention.
    Non-.db files, subdirectories, and ``.partial`` files are left alone.

    Returns the list of deleted paths.
    """
    if clock is None:
        clock = lambda: datetime.now(timezone.utc)

    if not backup_dir.exists():
        return []

    cutoff = clock() - timedelta(days=retention_days)
    deleted: list[Path] = []
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".db":
            continue
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            try:
                entry.unlink()
                deleted.append(entry)
            except OSError as e:
                logger.warning("Failed to delete %s: %s", entry, e)
    return deleted


# ------------------------------------------------------------------
# Scheduler
# ------------------------------------------------------------------

def _seconds_until_next(now: datetime, target: dt_time) -> float:
    """Seconds from ``now`` until the next occurrence of UTC time-of-day ``target``."""
    candidate = now.replace(
        hour=target.hour, minute=target.minute,
        second=0, microsecond=0,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


async def _default_sleep_until_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    """Sleep for ``seconds`` or until ``stop_event`` is set. Returns True iff stopped."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def run_daily_backup_loop(
    db_path: Path | str,
    config: BackupConfig,
    stop_event: asyncio.Event,
    *,
    clock: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], Awaitable[bool]] | None = None,
) -> None:
    """Run a daily backup at ``config.daily_at_utc``. Exits when ``stop_event`` is set.

    On failure (backup or prune): logs the exception and continues looping.
    The next attempt is at the next scheduled window — no catch-up, no retry
    storms.

    Args:
        db_path: source SQLite DB.
        config: backup settings.
        stop_event: set this to exit cleanly (wired to SIGTERM/SIGINT in main).
        clock: injectable UTC-now callable for tests.
        sleep_fn: injectable ``async def(seconds) -> bool`` returning True iff
            stopped. Defaults to ``asyncio.wait_for(stop_event.wait(), ...)``.
    """
    if not config.enabled:
        logger.info("Backups disabled — daily backup loop will not run")
        return

    if clock is None:
        clock = lambda: datetime.now(timezone.utc)

    if sleep_fn is None:
        sleep_fn = lambda secs: _default_sleep_until_or_stop(stop_event, secs)

    try:
        target = _parse_hhmm(config.daily_at_utc)
    except ValueError as e:
        logger.error("Invalid daily_at_utc, aborting backup loop: %s", e)
        return

    db_path = Path(db_path)
    backup_dir = Path(config.directory)
    logger.info(
        "Daily backup loop started: db=%s dir=%s at=%sZ retention=%dd",
        db_path, backup_dir, config.daily_at_utc, config.retention_days,
    )

    while not stop_event.is_set():
        sleep_seconds = _seconds_until_next(clock(), target)
        stopped = await sleep_fn(sleep_seconds)
        if stopped:
            logger.info("Daily backup loop stopped")
            return

        try:
            path = perform_backup(db_path, backup_dir, clock=clock)
            logger.info("Backup written: %s", path)
        except Exception:
            logger.exception("Backup failed")

        try:
            deleted = prune_old_backups(backup_dir, config.retention_days, clock=clock)
            if deleted:
                logger.info(
                    "Pruned %d backup(s) older than %d days",
                    len(deleted), config.retention_days,
                )
        except Exception:
            logger.exception("Backup prune failed")
