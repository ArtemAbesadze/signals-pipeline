"""Tests for ShadowCapture — sample archiving with no Discord coupling."""

import hashlib
import json
from datetime import datetime, timezone

import pytest

from src.input.shadow_capture import ShadowCapture


def _fixed_clock(dt: datetime):
    return lambda: dt


@pytest.fixture
def tmp_root(tmp_path):
    """Captures root in a fresh tmpdir."""
    return tmp_path / "captures"


def test_writes_txt_and_sidecar(tmp_root):
    """A non-empty message produces .txt + .json files with correct contents."""
    clock = _fixed_clock(datetime(2026, 5, 19, 14, 32, 5, tzinfo=timezone.utc))
    capture = ShadowCapture(root=tmp_root, clock=clock)

    body = "TRADING SIGNAL ALERT\n\nPAIR: BTC/USDT #1\nENTRY: 100"
    path = capture.write(body)

    expected_txt = tmp_root / "2026-05-19" / "14-32-05-001.txt"
    expected_json = tmp_root / "2026-05-19" / "14-32-05-001.json"
    assert path == expected_txt
    assert expected_txt.exists()
    assert expected_json.exists()

    raw = expected_txt.read_bytes()
    assert raw == body.encode("utf-8")

    sidecar = json.loads(expected_json.read_text())
    assert sidecar["ts"] == "2026-05-19T14:32:05+00:00"
    assert sidecar["path"] == "2026-05-19/14-32-05-001.txt"
    assert sidecar["bytes"] == len(raw)
    assert sidecar["sha256"] == hashlib.sha256(raw).hexdigest()


def test_index_jsonl_append_only(tmp_root):
    """Two messages produce two index lines; existing lines untouched."""
    clock_a = _fixed_clock(datetime(2026, 5, 19, 14, 32, 5, tzinfo=timezone.utc))
    capture = ShadowCapture(root=tmp_root, clock=clock_a)
    capture.write("msg one")

    first_lines = (tmp_root / "index.jsonl").read_text().splitlines()
    assert len(first_lines) == 1

    capture._clock = _fixed_clock(datetime(2026, 5, 19, 14, 32, 10, tzinfo=timezone.utc))
    capture.write("msg two")

    lines = (tmp_root / "index.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] == first_lines[0], "first index line was modified"

    a = json.loads(lines[0])
    b = json.loads(lines[1])
    assert a["path"] == "2026-05-19/14-32-05-001.txt"
    assert b["path"] == "2026-05-19/14-32-10-001.txt"


def test_counter_handles_same_second_collision(tmp_root):
    """Three messages in the same wall-clock second get distinct NNN suffixes."""
    clock = _fixed_clock(datetime(2026, 5, 19, 14, 32, 5, tzinfo=timezone.utc))
    capture = ShadowCapture(root=tmp_root, clock=clock)

    a = capture.write("first")
    b = capture.write("second")
    c = capture.write("third")

    assert a.name == "14-32-05-001.txt"
    assert b.name == "14-32-05-002.txt"
    assert c.name == "14-32-05-003.txt"


def test_skips_empty_messages(tmp_root):
    """Empty / whitespace-only messages are skipped — no files, no index entry."""
    clock = _fixed_clock(datetime(2026, 5, 19, 14, 32, 5, tzinfo=timezone.utc))
    capture = ShadowCapture(root=tmp_root, clock=clock)

    assert capture.write("") is None
    assert capture.write("   ") is None
    assert capture.write("\n\n\n") is None
    assert capture.captured == 0
    assert not (tmp_root / "2026-05-19").exists()
    assert not (tmp_root / "index.jsonl").exists()


def test_unicode_preserved(tmp_root):
    """Emoji-heavy messages round-trip byte-exact."""
    clock = _fixed_clock(datetime(2026, 5, 19, 14, 32, 5, tzinfo=timezone.utc))
    capture = ShadowCapture(root=tmp_root, clock=clock)

    text = "🔥 TP TARGET 1 HIT 📈\n💰 PROFIT: 16.03%"
    path = capture.write(text)
    assert path.read_text() == text


def test_directory_rolls_at_midnight_utc(tmp_root):
    """Messages on either side of midnight UTC land in the correct date dirs."""
    end_of_day = datetime(2026, 5, 19, 23, 59, 58, tzinfo=timezone.utc)
    start_of_next = datetime(2026, 5, 20, 0, 0, 1, tzinfo=timezone.utc)

    capture = ShadowCapture(root=tmp_root, clock=_fixed_clock(end_of_day))
    capture.write("last of day")

    capture._clock = _fixed_clock(start_of_next)
    capture.write("first of next day")

    assert (tmp_root / "2026-05-19" / "23-59-58-001.txt").exists()
    assert (tmp_root / "2026-05-20" / "00-00-01-001.txt").exists()


def test_captured_counter_reflects_writes(tmp_root):
    """``captured`` increments only on real writes, not on skips."""
    clock = _fixed_clock(datetime(2026, 5, 19, 14, 32, 5, tzinfo=timezone.utc))
    capture = ShadowCapture(root=tmp_root, clock=clock)

    capture.write("one")
    capture.write("")  # skipped
    capture.write("two")

    assert capture.captured == 2


def test_root_dir_auto_created(tmp_path):
    """Captures root is created on construction if it doesn't exist."""
    nested = tmp_path / "deeper" / "captures"
    assert not nested.exists()

    ShadowCapture(root=nested)
    assert nested.exists()
    assert nested.is_dir()


def test_trailing_whitespace_stripped(tmp_root):
    """Only trailing whitespace is stripped; internal whitespace preserved."""
    clock = _fixed_clock(datetime(2026, 5, 19, 14, 32, 5, tzinfo=timezone.utc))
    capture = ShadowCapture(root=tmp_root, clock=clock)

    body = "line one\n\n  indented line two  \n\n"
    path = capture.write(body)

    expected = "line one\n\n  indented line two"
    assert path.read_text() == expected
