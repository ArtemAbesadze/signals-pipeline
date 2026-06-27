"""Background reconciliation sweeper (2026-06-22 — recommendation #2).

CP lifecycle messages can be missed or delayed (laptop sleep, dropped DMs),
which silently leaves a trade OPEN locally after the exchange already closed
it. ADA #2262 (2026-06-19) sat 'open' in the DB for days while flat on Blofin,
because the bot only reconciled at startup and on CP events. This periodic
sweep is the missing between-events sync (D10/D11: the exchange is the source
of truth for position state).

Pattern mirrors src/telegram/confirmation_sweeper.py: a periodic asyncio task
that fans out across all active pipelines, owned by main.py. The per-pipeline
work (``Pipeline.reconcile_positions``) makes blocking HTTP calls, so it runs in
a worker thread to keep the event loop responsive.
"""

import asyncio
import logging

from src.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class ReconciliationSweeper:
    """Periodic task that reconciles local trades against the exchange's real
    position state, so a missed CP close can't leave the DB stale."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        interval_sec: float = 300.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._interval_sec = interval_sec
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "ReconciliationSweeper started (interval=%.0fs)", self._interval_sec,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ReconciliationSweeper stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("ReconciliationSweeper tick failed")
            await asyncio.sleep(self._interval_sec)

    async def sweep_once(self) -> dict[str, int]:
        """One reconciliation pass across all active pipelines.

        ``reconcile_positions`` is called INLINE (not via a worker thread): the
        pipeline's SQLite connection is bound to this event-loop thread, so
        offloading to ``asyncio.to_thread`` raised ``sqlite3.ProgrammingError``
        on every tick (the 48h soak, 2026-06-26 — the sweeper never actually
        ran). The blocking exchange reads (~1-3s every 5 min) are the same shape
        PnLMonitor already runs inline. Returns a per-user count of trades
        transitioned (closed + canceled).
        """
        moved: dict[str, int] = {}
        for user_id, ctx in list(self._orchestrator.pipelines.items()):
            if ctx.paused:
                continue
            try:
                summary = ctx.pipeline.reconcile_positions()
            except Exception:
                logger.exception(
                    "ReconciliationSweeper: reconcile failed for user %s", user_id,
                )
                continue
            n = len(summary.get("closed", [])) + len(summary.get("canceled", []))
            if n:
                moved[user_id] = n

        if moved:
            logger.info("ReconciliationSweeper: reconciled %s", moved)
        return moved
