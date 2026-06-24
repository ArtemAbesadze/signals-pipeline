"""Tests for ReconciliationSweeper (recommendation #2, 2026-06-22).

The sweeper fans Pipeline.reconcile_positions out across active pipelines on a
timer. Pipeline + orchestrator are mocked; reconcile_positions is the unit under
contract (its own audit behaviour is tested in test_e2e_pipeline.py)."""

from unittest.mock import MagicMock

import pytest

from src.reconciliation_sweeper import ReconciliationSweeper


def _orch(summary, paused=False, raises=False):
    pipeline = MagicMock()
    if raises:
        pipeline.reconcile_positions.side_effect = RuntimeError("boom")
    else:
        pipeline.reconcile_positions.return_value = summary
    ctx = MagicMock()
    ctx.pipeline = pipeline
    ctx.paused = paused
    orch = MagicMock()
    orch.pipelines = {"u1": ctx}
    return orch, pipeline


@pytest.mark.asyncio
async def test_sweep_counts_transitions():
    orch, pipeline = _orch(
        {"closed": [1, 2], "canceled": [3], "verified": [], "orphans": []})
    sw = ReconciliationSweeper(orchestrator=orch)
    moved = await sw.sweep_once()
    assert moved == {"u1": 3}          # 2 closed + 1 canceled
    pipeline.reconcile_positions.assert_called_once()


@pytest.mark.asyncio
async def test_sweep_skips_paused():
    orch, pipeline = _orch(
        {"closed": [1], "canceled": [], "verified": [], "orphans": []}, paused=True)
    sw = ReconciliationSweeper(orchestrator=orch)
    moved = await sw.sweep_once()
    assert moved == {}
    pipeline.reconcile_positions.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_survives_reconcile_error():
    orch, pipeline = _orch(None, raises=True)
    sw = ReconciliationSweeper(orchestrator=orch)
    moved = await sw.sweep_once()      # must not raise — one bad user can't kill the loop
    assert moved == {}


@pytest.mark.asyncio
async def test_sweep_noop_when_nothing_moved():
    orch, pipeline = _orch(
        {"closed": [], "canceled": [], "verified": [5], "orphans": []})
    sw = ReconciliationSweeper(orchestrator=orch)
    moved = await sw.sweep_once()
    assert moved == {}
    pipeline.reconcile_positions.assert_called_once()
