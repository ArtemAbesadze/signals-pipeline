"""Background sweeper for expired mainnet-confirmation trades (Phase 3.5).

When a mainnet trade above ``risk.mainnet_confirm_above_usd`` is opened with
``auto_execute=ON``, the pipeline holds it PENDING with
``requires_confirmation=1`` and pushes Approve/Reject buttons to Telegram.
If the user doesn't respond within ``risk.mainnet_confirm_timeout_min``
minutes, this sweeper auto-declines the trade — perp signals go stale fast
and we don't want a sleeping user to fire a confirmation 4 hours later on a
completely different market state.

Pattern mirrors src/telegram/pnl_monitor.py: a periodic asyncio task that
fans out across all active pipelines, owned by main.py. The sweep is
DB-driven (TradeDatabase.get_expired_confirmations) — survives bot restarts
without losing track of pending confirmations.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from src.orchestrator import Orchestrator
from src.state.models import EventType, TradeStatus

logger = logging.getLogger(__name__)


class ConfirmationSweeper:
    """Periodic background task that auto-declines expired confirmations."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        interval_sec: float = 30.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._interval_sec = interval_sec
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "ConfirmationSweeper started (interval=%.0fs)", self._interval_sec,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ConfirmationSweeper stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("ConfirmationSweeper tick failed")
            await asyncio.sleep(self._interval_sec)

    async def sweep_once(self) -> list[tuple[str, int]]:
        """Single sweep pass. Returns (user_id, trade_id) of expired trades.

        Each user's timeout comes from their own config — different users
        may have different ``mainnet_confirm_timeout_min`` values.
        """
        expired: list[tuple[str, int]] = []
        now = datetime.now(timezone.utc)

        for user_id, ctx in self._orchestrator.pipelines.items():
            if ctx.paused:
                continue

            timeout_min = ctx.config.risk.mainnet_confirm_timeout_min
            cutoff_iso = (now - timedelta(minutes=timeout_min)).isoformat()

            try:
                expired_ids = ctx.db.get_expired_confirmations(cutoff_iso)
            except Exception:
                logger.exception(
                    "ConfirmationSweeper: query failed for user %s", user_id,
                )
                continue

            for trade_id in expired_ids:
                try:
                    ctx.db.update_trade_status(
                        trade_id,
                        TradeStatus.CANCELED,
                        close_reason="confirmation_timeout",
                    )
                    ctx.db.record_event(
                        trade_id=trade_id,
                        event_type=EventType.CONFIRMATION_TIMEOUT,
                        raw_text=None,
                        action_taken=(
                            f"auto-declined after {timeout_min}m: "
                            "no Telegram response"
                        ),
                    )
                    expired.append((user_id, trade_id))
                    logger.info(
                        "Auto-declined trade #%d for user %s "
                        "(confirmation timeout after %dm)",
                        trade_id, user_id, timeout_min,
                    )

                    notifier = getattr(ctx.pipeline, "_notifier", None)
                    if notifier is not None:
                        try:
                            await notifier.notify_confirmation_timeout(
                                trade_id=trade_id,
                                timeout_min=timeout_min,
                            )
                        except Exception:
                            logger.exception(
                                "ConfirmationSweeper: notify failed for trade #%d",
                                trade_id,
                            )
                except Exception:
                    logger.exception(
                        "ConfirmationSweeper: failed to expire trade #%d (user %s)",
                        trade_id, user_id,
                    )

        if expired:
            logger.info("ConfirmationSweeper: expired %d trade(s)", len(expired))

        return expired
