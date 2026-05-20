"""Message formatting utilities for Telegram bot responses."""

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


def _format_time_et(dt: datetime | None) -> str:
    """Format a UTC datetime as New York time (e.g. 'Feb 19, 5:43 PM ET')."""
    if dt is None:
        return ""
    utc_dt = dt.replace(tzinfo=timezone.utc)
    ny_dt = utc_dt.astimezone(_NY)
    return ny_dt.strftime("%b %d, %-I:%M %p ET")


def mask_address(address: str) -> str:
    """Show first 6 and last 4 chars of an address: 0x1234...abcd."""
    if len(address) <= 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


def format_usd(value: str | float) -> str:
    """Format a USD value: $1,234.56."""
    v = float(value)
    return f"${v:,.2f}"


def format_pnl(value: str | float) -> str:
    """Format PnL with sign: +$123.45 or -$67.89."""
    v = float(value)
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.2f}"


def format_pct(value: float) -> str:
    """Format percentage with sign: +12.34% or -5.67%."""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def format_balance(balance: dict[str, str]) -> str:
    """Format balance data into a display string."""
    return (
        "💰 *Account Balance*\n\n"
        f"💵 USDC Balance: {format_usd(balance['usdc_balance'])}\n"
        f"📊 Account Value: {format_usd(balance['account_value'])}\n"
        f"📋 Margin Used: {format_usd(balance['total_margin_used'])}\n"
        f"📂 Position Value: {format_usd(balance['total_position_value'])}\n"
        f"💸 Withdrawable: {format_usd(balance['withdrawable'])}"
    )


def format_positions(positions: list[dict[str, Any]]) -> str:
    """Format open positions into a display string."""
    if not positions:
        return "📂 *Open Positions*\n\nNo open positions."

    lines = ["📂 *Open Positions*\n"]
    for p in positions:
        side = "LONG" if p["size"] > 0 else "SHORT"
        direction = "📈" if side == "LONG" else "📉"
        size = abs(p["size"])
        pnl = format_pnl(p["unrealized_pnl"])
        lines.append(
            f"{direction} *{p['coin']}* {side}\n"
            f"  Size: {size} | Entry: {p['entry_price']}\n"
            f"  PnL: {pnl} | Lev: {p['leverage']}x\n"
            f"  Liq: {p['liquidation_price']}"
        )

    return "\n\n".join(lines)


def format_account_block(credentials: dict) -> str:
    """Inline account block used inside the Config screen (wallet + network)."""
    wallet = mask_address(credentials.get("account_address", "N/A"))
    api_wallet = mask_address(credentials.get("api_wallet", "N/A"))
    network = credentials.get("network", "testnet").capitalize()
    return (
        "🔐 *Account*\n"
        f"Wallet: `{wallet}`\n"
        f"API: `{api_wallet}` ({network})"
    )


# ------------------------------------------------------------------
# Main menu dashboard helpers
# ------------------------------------------------------------------

def _format_port_status_line(
    port_usd: float | None,
    port_mode: str,
    wallet_usd: float | None,
) -> str:
    """One-line port + wallet status with bounds indicator.

    ✅ within bounds | ⚠️ near wallet (>95%) | 🛑 exceeds wallet | ⚙️ not set
    """
    if port_usd is None:
        return "💰 Port: _not set_  |  💼 Wallet: " + (format_usd(wallet_usd) if wallet_usd is not None else "—")

    port_str = format_usd(port_usd)
    wallet_str = format_usd(wallet_usd) if wallet_usd is not None else "—"
    if wallet_usd is None:
        marker = "—"
    elif port_usd > wallet_usd:
        marker = "🛑"
    elif port_usd > wallet_usd * 0.95:
        marker = "⚠️"
    else:
        marker = "✅"
    return f"💰 Port: {port_str} ({port_mode})  |  💼 Wallet: {wallet_str}  {marker}"


def _format_open_trade_line(trade: Any, position: dict | None) -> str:
    """One row in the dashboard's open-trades section."""
    side_emoji = "📈" if trade.side.upper() == "LONG" else "📉"
    if position is None:
        # Trade exists in DB but no exchange position — typically PENDING entry
        return f"  ⏳ {trade.coin} {trade.side} #{trade.trade_id}  pending entry"
    try:
        entry = float(position.get("entry_price", trade.entry_price))
        size = abs(float(position.get("size", 0)))
        unrealized = float(position.get("unrealized_pnl", 0))
        pnl_pct = (unrealized / (entry * size)) * 100 if entry > 0 and size > 0 else 0.0
    except (TypeError, ValueError):
        pnl_pct = 0.0
    return f"  🟢 {side_emoji} {trade.coin} {trade.side} #{trade.trade_id}  {pnl_pct:+.2f}%"


def _format_recent_event_line(event: Any, now_local: datetime, tz: ZoneInfo) -> str:
    """One row in the dashboard's 'Recent CP' section."""
    icon = {
        "tp_hit": "🎯",
        "breakeven": "⚖️",
        "stop_hit": "🛑",
        "trade_closed": "🏁",
        "cancel": "🚫",
        "order_pending": "⌛",
        "trade_live": "✅",
    }.get(event.event_type.value, "•")

    occurred = event.occurred_at
    if occurred is not None:
        occurred_local = occurred.replace(tzinfo=timezone.utc).astimezone(tz)
        if occurred_local.date() == now_local.date():
            time_str = occurred_local.strftime("%-I:%M %p")
        else:
            time_str = occurred_local.strftime("%b %-d, %-I:%M %p")
    else:
        time_str = "—"

    action = event.action_taken or event.event_type.value
    # Truncate long actions to keep the dashboard tight
    if len(action) > 70:
        action = action[:67] + "..."
    return f"  {icon} {time_str}  {action}"


def format_main_menu(
    display_name: str,
    port_state: dict,
    wallet_usd: float | None,
    pipeline_active: bool,
    auto_execute: bool,
    preset_name: str,
    open_trades: list[tuple[Any, dict | None]],
    today_count_closed: int,
    today_total_pnl_pct: float,
    today_wins: int,
    today_losses: int,
    recent_events: list[Any],
    tz: ZoneInfo = _NY,
) -> str:
    """Build the condensed dashboard shown as the main menu.

    Args:
        display_name: User's display name for the greeting line.
        port_state: ``{"port_usd", "port_mode", "port_watermark"}``.
        wallet_usd: Current wallet USDC balance, or None if unavailable.
        pipeline_active: True if the user's pipeline is not paused.
        auto_execute: True if auto-execute is on.
        preset_name: Active strategy preset name.
        open_trades: List of ``(TradeRecord, position dict | None)`` pairs
            for trades currently open or pending. Positions are matched by
            coin from ``client.get_open_positions()``.
        today_count_closed: Number of trades closed since local midnight.
        today_total_pnl_pct: Sum of pnl_pct for those trades.
        today_wins / today_losses: Splits within today_count_closed.
        recent_events: TradeEvent rows (most recent first) filtered to
            actionable types.
        tz: Local timezone for timestamps.
    """
    port_line = _format_port_status_line(
        port_state.get("port_usd"),
        port_state.get("port_mode", "withdraw"),
        wallet_usd,
    )
    pipeline_text = "▶️ Active" if pipeline_active else "⏸ Paused"
    auto_text = "ON" if auto_execute else "OFF"

    # Open trades section
    if open_trades:
        lines = [_format_open_trade_line(t, pos) for t, pos in open_trades]
        open_section = f"📂 *Open trades ({len(open_trades)}):*\n" + "\n".join(lines)
    else:
        open_section = "📂 *Open trades:* none"

    # Today section
    if today_count_closed > 0:
        today_section = (
            f"📈 *Today:* {format_pct(today_total_pnl_pct)} "
            f"({today_count_closed} closed: {today_wins}W / {today_losses}L)"
        )
    else:
        today_section = "📈 *Today:* no closed trades"

    # Recent CP section
    if recent_events:
        now_local = datetime.now(timezone.utc).astimezone(tz)
        event_lines = [_format_recent_event_line(e, now_local, tz) for e in recent_events]
        cp_section = "📡 *Recent CP:*\n" + "\n".join(event_lines)
    else:
        cp_section = "📡 *Recent CP:* nothing yet"

    return (
        f"🧪 *Potion Perps* — Hey {display_name}!\n\n"
        f"{port_line}\n"
        f"{pipeline_text}  |  ⚡ Auto: {auto_text}  |  🎯 {preset_name}\n\n"
        f"{open_section}\n\n"
        f"{today_section}\n\n"
        f"{cp_section}"
    )


def _status_badge(status_value: str) -> str:
    """Return an emoji + label for a trade status."""
    mapping = {
        "open": ("✅", "TAKEN"),
        "closed": ("🏁", "CLOSED"),
        "canceled": ("❌", "PASSED"),
    }
    icon, label = mapping.get(status_value, ("⏳", "PENDING"))
    return f"{icon} {label}"


def _format_signal_card(t: Any) -> str:
    """Render one trade as a full signal card (matches provider format)."""
    status = t.status.value if hasattr(t.status, "value") else str(t.status)
    badge = _status_badge(status)
    side_emoji = "📈" if t.side.upper() == "LONG" else "📉"
    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(t.risk_level.upper(), "⚪")

    # SL distance %
    if t.entry_price:
        sl_pct = ((t.stop_loss - t.entry_price) / t.entry_price) * 100
    else:
        sl_pct = 0.0

    # TP distances %
    tp_pcts = []
    for tp_val in (t.tp1, t.tp2, t.tp3):
        if t.entry_price:
            tp_pcts.append(((tp_val - t.entry_price) / t.entry_price) * 100)
        else:
            tp_pcts.append(0.0)

    return (
        f"{'─' * 28}\n"
        f"🔔 *SIGNAL #{t.trade_id}*  —  {badge}\n\n"
        f"💱 Pair: `{t.pair}`\n"
        f"{risk_emoji} Risk: {t.risk_level.upper()}\n"
        f"📋 Type: {t.trade_type.upper()}  |  Size: {t.size_hint}\n"
        f"{side_emoji} Side: *{t.side.upper()}*\n\n"
        f"🎯 Entry: `{t.entry_price}`\n"
        f"🛡 SL: `{t.stop_loss}`  ({sl_pct:+.2f}%)\n\n"
        f"🏆 *Take Profit Targets:*\n"
        f"  TP1: `{t.tp1}`  ({tp_pcts[0]:+.2f}%)\n"
        f"  TP2: `{t.tp2}`  ({tp_pcts[1]:+.2f}%)\n"
        f"  TP3: `{t.tp3}`  ({tp_pcts[2]:+.2f}%)\n\n"
        f"📊 Leverage: {t.leverage}x\n"
        f"🕐 {_format_time_et(getattr(t, 'created_at', None))}"
    )


def format_calls_view(trades: list) -> str:
    """Format recent trades for the calls view with full signal details."""
    if not trades:
        return (
            "📡 *Calls View*\n\n"
            "_Signal approval mode — incoming signals appear here._\n\n"
            "No recent signals."
        )

    header = (
        "📡 *Calls View*\n\n"
        "_Signal approval mode — approve or reject incoming trades._"
    )
    cards = [_format_signal_card(t) for t in trades]
    return header + "\n\n" + "\n\n".join(cards)


def format_trading_hub(balance: dict[str, str] | None, positions: list | None, open_trades: int = 0) -> str:
    """Format the trading hub summary."""
    text = "📊 *Trading*\n\n"

    if balance:
        text += f"💰 Balance: {format_usd(balance.get('account_value', '0'))}\n"
    else:
        text += "💰 Balance: _Unavailable_\n"

    # Calculate unrealized PnL from positions
    if positions:
        total_unrealized = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
        text += f"📈 Unrealized PnL: {format_pnl(total_unrealized)}\n"

    pos_count = len(positions) if positions else 0
    text += f"📂 Open Positions: {pos_count}\n"
    text += f"📋 Active Trades: {open_trades}"

    return text


def format_stats(closed_trades: list, open_count: int) -> str:
    """Format trading statistics."""
    if not closed_trades and open_count == 0:
        return "📈 *Trading Statistics*\n\nNo trades yet."

    total = len(closed_trades)
    pnls = [t.pnl_pct for t in closed_trades if t.pnl_pct is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    breakeven = sum(1 for p in pnls if p == 0)

    total_pnl = sum(pnls) if pnls else 0
    avg_pnl = total_pnl / len(pnls) if pnls else 0
    best = max(pnls) if pnls else 0
    worst = min(pnls) if pnls else 0
    win_rate = (wins / total * 100) if total > 0 else 0

    return (
        "📈 *Trading Statistics*\n\n"
        "📊 *Overview*\n"
        f"Total Closed: {total}\n"
        f"Currently Open: {open_count}\n"
        f"Win Rate: {win_rate:.1f}%\n\n"
        "💹 *Results*\n"
        f"Wins: {wins} | Losses: {losses} | BE: {breakeven}\n"
        f"Total PnL: {'+' if total_pnl >= 0 else ''}{total_pnl:.2f}%\n"
        f"Avg: {'+' if avg_pnl >= 0 else ''}{avg_pnl:.2f}% | "
        f"Best: {'+' if best >= 0 else ''}{best:.2f}%\n"
        f"Worst: {'+' if worst >= 0 else ''}{worst:.2f}%"
    )


