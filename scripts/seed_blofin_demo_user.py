#!/usr/bin/env python3
"""Seed (or remove) a dedicated Blofin **demo** user for the Phase 6.11 soak.

Creates a user with ``exchange=blofin`` pointed at the Blofin demo environment,
using the demo credentials from ``.env`` (BLOFIN_API_KEY / BLOFIN_API_SECRET /
BLOFIN_PASSPHRASE). Validates the key with a signed ``get_balance`` read before
persisting anything — a bad demo key fails loudly here, not mid-soak.

This bypasses the Telegram ``/register`` flow on purpose: that UX is already
unit-tested (Phase 6.9), and the soak's job is to exercise the trading stack
(BlofinClient → order builder → position manager → adapter dispatch), not the
registration screens.

Neutral credential mapping (matches build_exchange_client / the adapter):
  account_address = Blofin API key · api_secret = secret · passphrase = passphrase
  api_wallet = "" (HL-only column, NOT NULL) · network = "testnet" → demo host

Usage:
    python3 scripts/seed_blofin_demo_user.py                 # create/update
    python3 scripts/seed_blofin_demo_user.py --port 5000     # custom demo port
    python3 scripts/seed_blofin_demo_user.py --user-id blofin_demo
    python3 scripts/seed_blofin_demo_user.py --list          # show all users + exchange
    python3 scripts/seed_blofin_demo_user.py --remove        # deactivate the demo user

Soak isolation — only the Blofin demo user should auto-execute during the run
(every active pipeline processes every channel message, and the synthetic
Blofin coins overlap with HL coins). Deactivate the HL users for the soak and
reactivate after (status change is reversible; nothing is deleted):
    python3 scripts/seed_blofin_demo_user.py --user-id 7441245554 --set-status inactive
    python3 scripts/seed_blofin_demo_user.py --user-id 7441245554 --set-status active

After seeding, restart the bot so the orchestrator activates the pipeline:
    launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot
  (or run `python3 main.py` in the foreground for an observed soak).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_USER_ID = "blofin_demo"
DEFAULT_PORT_USD = 5000.0


def _dotenv_value(key: str, env_path: Path | None = None) -> str | None:
    """Read *key* directly from the .env FILE (first match), ignoring the
    process environment, so the file is authoritative for demo creds. A stale
    ``export BLOFIN_API_KEY=...`` in the shell can't shadow the demo key here
    (that bit us on 2026-06-05 — see test_driver._dotenv_value)."""
    env_path = env_path or (_REPO_ROOT / ".env")
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def _demo_creds() -> dict[str, str]:
    # .env is authoritative; fall back to the environment only if absent there.
    key = _dotenv_value("BLOFIN_API_KEY") or os.environ.get("BLOFIN_API_KEY")
    secret = _dotenv_value("BLOFIN_API_SECRET") or os.environ.get("BLOFIN_API_SECRET")
    passphrase = _dotenv_value("BLOFIN_PASSPHRASE") or os.environ.get("BLOFIN_PASSPHRASE")
    if not (key and secret and passphrase):
        raise SystemExit(
            "Missing demo creds — set BLOFIN_API_KEY / BLOFIN_API_SECRET / "
            "BLOFIN_PASSPHRASE (the DEMO key) in .env or the environment."
        )
    return {"api_key": key, "api_secret": secret, "passphrase": passphrase}


def _validate_demo_key(creds: dict[str, str]) -> None:
    """Signed get_balance against the demo host — proves the key works."""
    from src.exchange.blofin import BlofinClient

    client = BlofinClient(
        api_key=creds["api_key"], api_secret=creds["api_secret"],
        passphrase=creds["passphrase"], network="demo",
    )
    bal = client.get_balance()
    print(f"  demo key OK — available={bal.get('available')} "
          f"equity={bal.get('total_equity')} USDT")


def _list_users() -> None:
    from src.state.user_db import UserDatabase

    db = UserDatabase()
    try:
        for u in db.get_active_users():
            creds = db.get_user_credentials_decrypted(u.user_id) or {}
            port = db.get_port_state(u.user_id) or {}
            print(f"  {u.user_id:<16} status={u.status:<8} "
                  f"exchange={creds.get('exchange','?'):<11} "
                  f"network={creds.get('network','?'):<8} "
                  f"port={port.get('port_usd')}")
    finally:
        db.close()


def _set_status(user_id: str, status: str) -> None:
    """Reversible status change (no hard delete). status='inactive' takes a
    user out of the orchestrator's activation set on the next restart;
    'active' puts them back. Credentials/config/port are untouched."""
    from src.state.user_db import UserDatabase

    db = UserDatabase()
    try:
        if not db.get_user(user_id):
            print(f"  user '{user_id}' not found")
            return
        db.set_user_status(user_id, status)
        print(f"  user '{user_id}' status → {status}")
    finally:
        db.close()


def _seed(user_id: str, port_usd: float) -> None:
    from src.state.user_db import UserDatabase

    creds = _demo_creds()
    print("Validating demo key...")
    _validate_demo_key(creds)

    credentials = {
        "account_address": creds["api_key"],   # neutral: holds the API key
        "api_wallet": "",                       # HL-only column (NOT NULL)
        "api_secret": creds["api_secret"],
        "passphrase": creds["passphrase"],
        "network": "testnet",                   # → demo host via build_exchange_client
        "exchange": "blofin",
    }
    config = {"active_preset": "even_split", "auto_execute": True, "max_leverage": 20}

    db = UserDatabase()
    try:
        if db.get_user(user_id):
            print(f"User '{user_id}' exists — updating creds + config + port...")
            db.update_user_credentials(
                user_id,
                account_address=credentials["account_address"],
                api_wallet=credentials["api_wallet"],
                api_secret=credentials["api_secret"],
                passphrase=credentials["passphrase"],
                network=credentials["network"],
                exchange=credentials["exchange"],
            )
            db.update_user_config(user_id, active_preset="even_split",
                                  auto_execute=True, max_leverage=20)
        else:
            print(f"Creating Blofin demo user '{user_id}'...")
            db.create_user(user_id, "Blofin Demo (6.11 soak)", credentials, config)

        db.set_port(user_id, port_usd=port_usd, port_mode="withdraw")
        print(f"  port set: ${port_usd:.0f} (withdraw mode)")
    finally:
        db.close()

    print(
        f"\nDone. User '{user_id}' is exchange=blofin, network=demo, "
        f"auto_execute=ON, even_split, port=${port_usd:.0f}.\n"
        "Restart the bot to activate the pipeline:\n"
        "  launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot\n"
        "  (or `python3 main.py` foreground for an observed soak)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--port", type=float, default=DEFAULT_PORT_USD,
                        help=f"demo port USD (default {DEFAULT_PORT_USD:.0f})")
    parser.add_argument("--remove", action="store_true",
                        help="deactivate the demo user (status→inactive) and exit")
    parser.add_argument("--set-status", choices=("active", "inactive"),
                        help="set --user-id's status (reversible; for soak isolation)")
    parser.add_argument("--list", action="store_true", help="list users + exchange and exit")
    args = parser.parse_args()

    if args.list:
        _list_users()
        return
    if args.set_status:
        _set_status(args.user_id, args.set_status)
        return
    if args.remove:
        _set_status(args.user_id, "inactive")
        return
    _seed(args.user_id, args.port)


if __name__ == "__main__":
    main()
