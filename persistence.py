"""
persistence.py

Tiny SQLite persistence layer for RiskState. Same pattern as your other
bots' SQLite usage (Aura FX trade persistence). Critical here because a
Railway redeploy or crash must NOT reset drawdown/consistency tracking --
that would let the bot accidentally re-risk buffer it already used.
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from risk_engine import RiskState

DB_PATH = Path(__file__).parent / "state.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price REAL,
            reason TEXT,
            risk_check_passed INTEGER NOT NULL,
            risk_check_note TEXT
        )
        """
    )
    conn.commit()
    return conn


def save_state(state: RiskState) -> None:
    conn = _connect()
    payload = json.dumps({
        "high_water_mark": state.high_water_mark,
        "current_equity": state.current_equity,
        "trailing_floor": state.trailing_floor,
        "daily_pnl": state.daily_pnl,
        "daily_start_equity": state.daily_start_equity,
        "cumulative_profit_since_reset": state.cumulative_profit_since_reset,
        "best_single_day_profit": state.best_single_day_profit,
        "last_daily_reset": state.last_daily_reset.isoformat(),
        "trading_halted_reason": state.trading_halted_reason,
    })
    conn.execute(
        "INSERT INTO risk_state (id, payload, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
        (payload, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def load_state() -> RiskState | None:
    conn = _connect()
    row = conn.execute("SELECT payload FROM risk_state WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return None
    d = json.loads(row[0])
    return RiskState(
        high_water_mark=d["high_water_mark"],
        current_equity=d["current_equity"],
        trailing_floor=d["trailing_floor"],
        daily_pnl=d["daily_pnl"],
        daily_start_equity=d["daily_start_equity"],
        cumulative_profit_since_reset=d["cumulative_profit_since_reset"],
        best_single_day_profit=d["best_single_day_profit"],
        last_daily_reset=datetime.fromisoformat(d["last_daily_reset"]),
        trading_halted_reason=d["trading_halted_reason"],
    )


def log_trade(symbol: str, side: str, qty: int, price: float | None,
              reason: str, risk_check_passed: bool, risk_check_note: str) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO trade_log (ts, symbol, side, qty, price, reason, risk_check_passed, risk_check_note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), symbol, side, qty, price,
         reason, int(risk_check_passed), risk_check_note),
    )
    conn.commit()
    conn.close()
