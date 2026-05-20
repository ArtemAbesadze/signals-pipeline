"""Inline keyboard builders for Telegram bot.

Menu graph (Phase 2.2 redesign):

    Main (dashboard)
    ├── Calls
    ├── Trading (hub) → Balance / Positions / Trades / History / Stats
    ├── Port → state + history (Phase 2.2 Commit B)
    ├── Config (absorbs Account + Dashboard)
    └── Pause (toggle)
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ------------------------------------------------------------------
# Navigation helpers
# ------------------------------------------------------------------

def _back_refresh_close(back_target: str, back_label: str = "⬅️ Menu") -> list[list[InlineKeyboardButton]]:
    """Standard navigation rows: [Back | Refresh] + [Close]."""
    return [
        [
            InlineKeyboardButton(back_label, callback_data=back_target),
            InlineKeyboardButton("🔄 Refresh", callback_data="menu:refresh"),
        ],
        [InlineKeyboardButton("✖ Close", callback_data="menu:close")],
    ]


# ------------------------------------------------------------------
# Main menu — condensed dashboard with 4 drill-downs + pause/refresh
# ------------------------------------------------------------------

def main_menu_keyboard(is_active: bool = True) -> InlineKeyboardMarkup:
    """Main menu — 4 drill-downs + Pause toggle + Refresh + Close.

    ``is_active`` controls the Pause/Resume button label.
    """
    pause_btn = (
        InlineKeyboardButton("⏸ Pause", callback_data="menu:pause")
        if is_active
        else InlineKeyboardButton("▶️ Resume", callback_data="menu:resume")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📡 Calls", callback_data="menu:calls"),
            InlineKeyboardButton("📊 Trading", callback_data="menu:trading"),
        ],
        [
            InlineKeyboardButton("🛡 Port", callback_data="menu:port"),
            InlineKeyboardButton("⚙️ Config", callback_data="menu:config"),
        ],
        [
            pause_btn,
            InlineKeyboardButton("🔄 Refresh", callback_data="menu:refresh"),
        ],
        [InlineKeyboardButton("✖ Close", callback_data="menu:close")],
    ])


# ------------------------------------------------------------------
# Submenu keyboards
# ------------------------------------------------------------------

def calls_view_keyboard() -> InlineKeyboardMarkup:
    """Calls view — Back + Refresh + Close."""
    return InlineKeyboardMarkup(_back_refresh_close("menu:main"))


def trading_hub_keyboard() -> InlineKeyboardMarkup:
    """Trading hub — 5 sub-buttons (incl. Stats) + nav."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Balance", callback_data="trading:balance"),
            InlineKeyboardButton("📂 Positions", callback_data="trading:positions"),
        ],
        [
            InlineKeyboardButton("📋 Trades", callback_data="trading:trades"),
            InlineKeyboardButton("📜 History", callback_data="trading:history"),
        ],
        [InlineKeyboardButton("📈 Stats", callback_data="trading:stats")],
        *_back_refresh_close("menu:main"),
    ])


def trading_sub_keyboard() -> InlineKeyboardMarkup:
    """Trading sub-view (balance, positions, etc.) — Back to Trading + nav."""
    return InlineKeyboardMarkup(_back_refresh_close("menu:trading", "⬅️ Trading"))


def port_keyboard() -> InlineKeyboardMarkup:
    """Port management — state + history view (Phase 2.2 Commit B)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Set Amount", callback_data="port:set_amount"),
            InlineKeyboardButton("🔄 Change Mode", callback_data="port:change_mode"),
        ],
        [InlineKeyboardButton("📜 Full History", callback_data="port:full_history")],
        *_back_refresh_close("menu:main"),
    ])


def config_menu_keyboard() -> InlineKeyboardMarkup:
    """Configuration submenu — preset / auto / leverage / risk + nav.

    Pipeline pause/resume now lives on the main menu, not here.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 Preset", callback_data="cfg:strategy"),
            InlineKeyboardButton("⚡ Auto-Execute", callback_data="cfg:auto"),
        ],
        [
            InlineKeyboardButton("📊 Leverage", callback_data="cfg:leverage"),
            InlineKeyboardButton("🛡 Risk Limits", callback_data="cfg:risk"),
        ],
        *_back_refresh_close("menu:main"),
    ])


def preset_keyboard() -> InlineKeyboardMarkup:
    """Strategy preset selection keyboard.

    Names mirror CryptoPrinter's weekly performance report rows so a
    user's report rows line up directly with CP's (D2). ``even_split``
    is the default.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛡 tp1_only", callback_data="cfg:preset:tp1_only"),
            InlineKeyboardButton("🎯 tp2_only", callback_data="cfg:preset:tp2_only"),
            InlineKeyboardButton("📈 tp3_only", callback_data="cfg:preset:tp3_only"),
        ],
        [
            InlineKeyboardButton("🎯 tp2_be", callback_data="cfg:preset:tp2_be"),
            InlineKeyboardButton("📈 tp3_be", callback_data="cfg:preset:tp3_be"),
        ],
        [
            InlineKeyboardButton("⚖️ hybrid", callback_data="cfg:preset:hybrid"),
            InlineKeyboardButton("🟰 even_split", callback_data="cfg:preset:even_split"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="cfg:back")],
    ])


def risk_keyboard() -> InlineKeyboardMarkup:
    """Risk limits adjustment keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Max Positions", callback_data="cfg:risk:max_open_positions")],
        [InlineKeyboardButton("💰 Max Position Size", callback_data="cfg:risk:max_position_size_usd")],
        [InlineKeyboardButton("📈 Max Exposure", callback_data="cfg:risk:max_total_exposure_usd")],
        [InlineKeyboardButton("🛡 Daily Loss Limit", callback_data="cfg:risk:max_daily_loss_pct")],
        [InlineKeyboardButton("⬅️ Back", callback_data="cfg:back")],
    ])
