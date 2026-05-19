"""Shadow-mode capture sink.

Writes verbatim messages to a date-organized directory plus an append-only
JSONL index log. Used by ``run_shadow()`` in main.py to grow a sample
corpus from CryptoPrinter's Discord channel without executing trades.

This module has no Discord coupling. The Discord adapter (or any source)
feeds it strings through its asyncio.Queue, and ShadowCapture writes them
to disk.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


class ShadowCapture:
    """Persists raw signal messages to a date-organized directory.

    Layout::

        <root>/YYYY-MM-DD/HH-MM-SS-NNN.txt   verbatim message body
        <root>/YYYY-MM-DD/HH-MM-SS-NNN.json  sidecar metadata
        <root>/index.jsonl                    append-only capture log

    ``NNN`` is a per-second counter so multiple messages in the same
    wall-clock second don't collide. The sidecar and index entries carry
    ISO timestamp, byte length, sha256, and the relative file path.

    Args:
        root: Captures directory. Created if missing.
        clock: Optional callable returning the current UTC datetime.
            Defaults to ``datetime.now(timezone.utc)``. Injectable for
            tests.
    """

    def __init__(
        self,
        root: str | Path,
        clock: Callable[[], datetime] | None = None,
    ):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._index_path = self._root / "index.jsonl"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._captured = 0

    @property
    def captured(self) -> int:
        """Number of messages written since construction."""
        return self._captured

    def write(self, text: str) -> Path | None:
        """Persist *text* to disk if it has content.

        Empty or whitespace-only messages are skipped (logged at DEBUG).
        Returns the .txt path on success, ``None`` if skipped.
        """
        body = text.rstrip()
        if not body:
            logger.debug("ShadowCapture: skipping empty message")
            return None

        now = self._clock()
        date_dir = self._root / now.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)

        # Per-second counter — find the next free NNN suffix.
        time_prefix = now.strftime("%H-%M-%S")
        counter = 1
        while True:
            txt_path = date_dir / f"{time_prefix}-{counter:03d}.txt"
            if not txt_path.exists():
                break
            counter += 1

        body_bytes = body.encode("utf-8")
        txt_path.write_bytes(body_bytes)

        sidecar = {
            "ts": now.isoformat(),
            "path": str(txt_path.relative_to(self._root)),
            "bytes": len(body_bytes),
            "sha256": hashlib.sha256(body_bytes).hexdigest(),
        }
        json_path = txt_path.with_suffix(".json")
        json_path.write_text(json.dumps(sidecar, indent=2))

        with self._index_path.open("a") as f:
            f.write(json.dumps(sidecar) + "\n")

        self._captured += 1
        logger.info(
            "ShadowCapture: wrote %d bytes -> %s",
            len(body_bytes), txt_path.name,
        )
        return txt_path
