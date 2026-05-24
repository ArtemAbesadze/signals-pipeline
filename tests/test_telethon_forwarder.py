"""Tests for scripts/telethon_forwarder.py — env validation surface.

The forwarder is mostly a thin Telethon wrapper (event handler + send loop)
that's hard to unit-test without running a real Telegram session. What we
CAN test cheaply is the config-loading path: required vars, numeric
parsing, session-path resolution, defaults. That catches the most common
deployment-time failure mode (missing/typo'd env var).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FORWARDER_PATH = REPO_ROOT / "scripts" / "telethon_forwarder.py"


def _load_forwarder():
    """Import the forwarder script as a module (it lives in scripts/, not src/)."""
    spec = importlib.util.spec_from_file_location(
        "telethon_forwarder", FORWARDER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["telethon_forwarder"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def forwarder():
    return _load_forwarder()


@pytest.fixture
def valid_env(monkeypatch):
    """Set all required env vars to plausible values."""
    monkeypatch.setenv("TG_API_ID", "12345678")
    monkeypatch.setenv("TG_API_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("TG_PHONE", "+15551234567")
    monkeypatch.setenv("TG_SOURCE_BOT", "@PotionScannerBot")
    monkeypatch.setenv("TG_DEST_CHANNEL_ID", "-1001234567890")
    yield
    # monkeypatch cleans up


class TestReadConfig:
    def test_all_required_vars_parse(self, forwarder, valid_env):
        cfg = forwarder.read_config()
        assert cfg["api_id"] == 12345678
        assert cfg["api_hash"] == "0123456789abcdef0123456789abcdef"
        assert cfg["phone"] == "+15551234567"
        assert cfg["source_bot"] == "@PotionScannerBot"
        assert cfg["dest_channel_id"] == -1001234567890

    def test_session_file_default(self, forwarder, valid_env, monkeypatch):
        monkeypatch.delenv("TG_SESSION_FILE", raising=False)
        cfg = forwarder.read_config()
        assert cfg["session_file"] == "data/.telethon_session"

    def test_session_file_override(self, forwarder, valid_env, monkeypatch):
        monkeypatch.setenv("TG_SESSION_FILE", "/tmp/custom_session")
        cfg = forwarder.read_config()
        assert cfg["session_file"] == "/tmp/custom_session"

    @pytest.mark.parametrize("missing_var", [
        "TG_API_ID", "TG_API_HASH", "TG_PHONE", "TG_SOURCE_BOT", "TG_DEST_CHANNEL_ID",
    ])
    def test_missing_var_aborts(self, forwarder, valid_env, monkeypatch, missing_var):
        monkeypatch.delenv(missing_var, raising=False)
        with pytest.raises(SystemExit, match="missing env vars"):
            forwarder.read_config()

    def test_non_integer_api_id_aborts(self, forwarder, valid_env, monkeypatch):
        monkeypatch.setenv("TG_API_ID", "not_a_number")
        with pytest.raises(SystemExit, match="TG_API_ID"):
            forwarder.read_config()

    def test_non_integer_channel_id_aborts(self, forwarder, valid_env, monkeypatch):
        """Channel IDs are negative ints like -1001234567890. Anything else
        means the user pasted a @channelname or copy-pasted with quotes —
        we want a clear error, not a runtime crash later."""
        monkeypatch.setenv("TG_DEST_CHANNEL_ID", "@some_channel")
        with pytest.raises(SystemExit, match="TG_DEST_CHANNEL_ID"):
            forwarder.read_config()


class TestSecureSession:
    def test_chmod_600_on_existing_file(self, forwarder, tmp_path):
        session = tmp_path / ".session"
        session.write_text("dummy")
        session.chmod(0o644)  # start permissive
        forwarder._secure_session(session)
        mode = session.stat().st_mode & 0o777
        assert mode == 0o600

    def test_no_op_when_file_missing(self, forwarder, tmp_path):
        session = tmp_path / "does_not_exist"
        # Must not raise
        forwarder._secure_session(session)
        assert not session.exists()
