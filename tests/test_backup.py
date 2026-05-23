"""Tests for the daily SQLite backup task (D9)."""

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.state.backup import (
    BackupConfig,
    _parse_hhmm,
    _seconds_until_next,
    perform_backup,
    prune_old_backups,
    run_daily_backup_loop,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_db(path: Path, row_count: int = 3) -> None:
    """Create a small SQLite DB with a known schema and row count."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO widgets (name) VALUES (?)",
            [(f"w{i}",) for i in range(row_count)],
        )
        conn.commit()
    finally:
        conn.close()


def _read_row_count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0]
    finally:
        conn.close()


# ------------------------------------------------------------------
# _parse_hhmm
# ------------------------------------------------------------------

def test_parse_hhmm_valid() -> None:
    t = _parse_hhmm("06:00")
    assert t.hour == 6 and t.minute == 0 and t.tzinfo == timezone.utc


def test_parse_hhmm_rejects_bad_input() -> None:
    for bad in ["", "6", "06", "06:00:00", "25:00", "06:60", "ab:cd"]:
        with pytest.raises(ValueError):
            _parse_hhmm(bad)


# ------------------------------------------------------------------
# _seconds_until_next
# ------------------------------------------------------------------

def test_seconds_until_next_today() -> None:
    now = datetime(2026, 5, 23, 5, 0, tzinfo=timezone.utc)
    target = _parse_hhmm("06:00")
    assert _seconds_until_next(now, target) == 3600.0


def test_seconds_until_next_tomorrow_when_past() -> None:
    now = datetime(2026, 5, 23, 6, 1, tzinfo=timezone.utc)
    target = _parse_hhmm("06:00")
    # Next 06:00 UTC is tomorrow → 23h59m
    assert _seconds_until_next(now, target) == (23 * 3600 + 59 * 60)


def test_seconds_until_next_exact_match_jumps_a_day() -> None:
    # If now == target, we just ran (or are about to), wait until next day.
    now = datetime(2026, 5, 23, 6, 0, tzinfo=timezone.utc)
    target = _parse_hhmm("06:00")
    assert _seconds_until_next(now, target) == 24 * 3600


# ------------------------------------------------------------------
# perform_backup
# ------------------------------------------------------------------

def test_perform_backup_preserves_data(tmp_path: Path) -> None:
    src = tmp_path / "trades.db"
    _make_db(src, row_count=5)
    backup_dir = tmp_path / "backups"

    fixed_now = datetime(2026, 5, 23, 6, 0, 0, tzinfo=timezone.utc)
    out = perform_backup(src, backup_dir, clock=lambda: fixed_now)

    assert out.name == "trades-20260523-060000.db"
    assert out.exists()
    assert _read_row_count(out) == 5


def test_perform_backup_creates_dir(tmp_path: Path) -> None:
    src = tmp_path / "trades.db"
    _make_db(src)
    backup_dir = tmp_path / "nested" / "backups"
    assert not backup_dir.exists()

    perform_backup(src, backup_dir)
    assert backup_dir.is_dir()


def test_perform_backup_leaves_no_partial_on_success(tmp_path: Path) -> None:
    src = tmp_path / "trades.db"
    _make_db(src)
    backup_dir = tmp_path / "backups"

    perform_backup(src, backup_dir)

    partials = list(backup_dir.glob("*.partial"))
    assert partials == []


def test_perform_backup_raises_if_source_missing(tmp_path: Path) -> None:
    src = tmp_path / "missing.db"
    backup_dir = tmp_path / "backups"
    with pytest.raises(FileNotFoundError):
        perform_backup(src, backup_dir)


def test_perform_backup_does_not_lock_source(tmp_path: Path) -> None:
    """Source remains writable during/after backup (online backup API)."""
    src = tmp_path / "trades.db"
    _make_db(src, row_count=2)
    backup_dir = tmp_path / "backups"

    perform_backup(src, backup_dir)

    # Source still writable after the snapshot.
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("INSERT INTO widgets (name) VALUES ('after')")
        conn.commit()
    finally:
        conn.close()
    assert _read_row_count(src) == 3


# ------------------------------------------------------------------
# prune_old_backups
# ------------------------------------------------------------------

def _touch_with_mtime(path: Path, age_days: float, *, now: datetime) -> None:
    """Create a file and set its mtime to ``age_days`` ago relative to ``now``."""
    path.write_text("x")
    target_dt = now - timedelta(days=age_days)
    target_ts = target_dt.timestamp()
    os.utime(path, (target_ts, target_ts))


def test_prune_deletes_old_keeps_new(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = datetime(2026, 5, 23, 6, 0, tzinfo=timezone.utc)

    old = backup_dir / "trades-old.db"
    new = backup_dir / "trades-new.db"
    _touch_with_mtime(old, age_days=45, now=now)
    _touch_with_mtime(new, age_days=10, now=now)

    deleted = prune_old_backups(backup_dir, retention_days=30, clock=lambda: now)

    assert deleted == [old] or [p.name for p in deleted] == ["trades-old.db"]
    assert not old.exists()
    assert new.exists()


def test_prune_ignores_non_db_and_partials(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = datetime(2026, 5, 23, 6, 0, tzinfo=timezone.utc)

    db_old = backup_dir / "trades-old.db"
    partial_old = backup_dir / "trades-old.db.partial"
    note_old = backup_dir / "note.txt"
    _touch_with_mtime(db_old, age_days=60, now=now)
    _touch_with_mtime(partial_old, age_days=60, now=now)
    _touch_with_mtime(note_old, age_days=60, now=now)

    prune_old_backups(backup_dir, retention_days=30, clock=lambda: now)

    assert not db_old.exists()
    assert partial_old.exists()
    assert note_old.exists()


def test_prune_handles_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert prune_old_backups(missing, retention_days=30) == []


def test_prune_does_not_recurse(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    sub = backup_dir / "sub"
    sub.mkdir(parents=True)
    now = datetime(2026, 5, 23, 6, 0, tzinfo=timezone.utc)

    nested = sub / "trades-nested.db"
    _touch_with_mtime(nested, age_days=60, now=now)

    prune_old_backups(backup_dir, retention_days=30, clock=lambda: now)
    assert nested.exists()


# ------------------------------------------------------------------
# run_daily_backup_loop
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_disabled_returns_immediately(tmp_path: Path) -> None:
    cfg = BackupConfig(enabled=False)
    stop = asyncio.Event()
    # If this hangs the test will time out — finishing fast is the assertion.
    await asyncio.wait_for(
        run_daily_backup_loop(tmp_path / "trades.db", cfg, stop),
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_loop_exits_when_stop_event_set_before_window(tmp_path: Path) -> None:
    src = tmp_path / "trades.db"
    _make_db(src)
    cfg = BackupConfig(directory=str(tmp_path / "backups"), daily_at_utc="06:00")
    stop = asyncio.Event()
    stop.set()

    async def sleep_fn(seconds: float) -> bool:
        return stop.is_set()

    await asyncio.wait_for(
        run_daily_backup_loop(src, cfg, stop, sleep_fn=sleep_fn),
        timeout=1.0,
    )
    # No backup should have been written — we stopped before the window.
    assert list((tmp_path / "backups").glob("*.db")) == []


@pytest.mark.asyncio
async def test_loop_runs_backup_then_prune_then_stops(tmp_path: Path) -> None:
    src = tmp_path / "trades.db"
    _make_db(src, row_count=4)
    backup_dir = tmp_path / "backups"

    cfg = BackupConfig(
        directory=str(backup_dir),
        retention_days=30,
        daily_at_utc="06:00",
    )
    stop = asyncio.Event()
    clock_val = [datetime(2026, 5, 23, 5, 59, tzinfo=timezone.utc)]
    call_count = [0]

    async def sleep_fn(seconds: float) -> bool:
        call_count[0] += 1
        if call_count[0] == 1:
            # First wait: pretend the window arrived; advance the clock.
            clock_val[0] = datetime(2026, 5, 23, 6, 0, tzinfo=timezone.utc)
            return False
        # Second wait: simulate stop being set.
        stop.set()
        return True

    await asyncio.wait_for(
        run_daily_backup_loop(
            src, cfg, stop,
            clock=lambda: clock_val[0],
            sleep_fn=sleep_fn,
        ),
        timeout=1.0,
    )

    backups = list(backup_dir.glob("*.db"))
    assert len(backups) == 1
    assert _read_row_count(backups[0]) == 4


@pytest.mark.asyncio
async def test_loop_continues_after_backup_failure(tmp_path: Path, caplog) -> None:
    """If perform_backup fails, the loop logs and continues — does not crash."""
    missing_src = tmp_path / "missing.db"  # backup will raise FileNotFoundError
    cfg = BackupConfig(directory=str(tmp_path / "backups"), daily_at_utc="06:00")
    stop = asyncio.Event()
    iterations = [0]

    async def sleep_fn(seconds: float) -> bool:
        iterations[0] += 1
        if iterations[0] >= 3:
            stop.set()
            return True
        return False  # proceed to attempt backup, which will fail

    await asyncio.wait_for(
        run_daily_backup_loop(missing_src, cfg, stop, sleep_fn=sleep_fn),
        timeout=1.0,
    )
    # Two failed attempts before stop — loop survived both.
    assert iterations[0] == 3


@pytest.mark.asyncio
async def test_loop_aborts_on_invalid_time(tmp_path: Path) -> None:
    cfg = BackupConfig(directory=str(tmp_path / "backups"), daily_at_utc="not-a-time")
    stop = asyncio.Event()
    await asyncio.wait_for(
        run_daily_backup_loop(tmp_path / "trades.db", cfg, stop),
        timeout=1.0,
    )
    # No backup, no crash.
    assert not (tmp_path / "backups").exists() or list((tmp_path / "backups").glob("*.db")) == []
