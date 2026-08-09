"""
equity_tracker.py (multi-instrument)

Tracks estimated equity across MULTIPLE simultaneously-held instruments
(e.g. MGC and MNQ at once). Same core limitation as before: TradersPost
has no read API for account balance, so this is a local ESTIMATE built
from trades this bot places plus live prices. Reconcile against the
real Tradeify balance regularly via POST /reconcile.

Each instrument has its OWN contract multiplier (dollars per $1/point
move) -- MGC and MNQ are not interchangeable here, mixing them up would
silently misstate risk.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OpenPosition:
    side: str          # "Buy" or "Sell"
    qty: int
    entry_price: float
    multiplier: float  # $ per 1-unit price move, per contract
    fee_round_trip: float


@dataclass
class EquityTracker:
    starting_balance: float
    realized_pnl: float = 0.0
    positions: dict[str, OpenPosition] = field(default_factory=dict)  # keyed by instrument name

    def reconcile(self, real_balance_now: float) -> None:
        """Call after checking the real Tradeify dashboard balance. Resets
        the baseline so estimation drift (fees, slippage) doesn't compound.
        Open positions are left as-is; entry prices still anchor unrealized
        P&L correctly relative to the new baseline."""
        self.starting_balance = real_balance_now
        self.realized_pnl = 0.0

    def open(self, instrument: str, side: str, qty: int, entry_price: float,
              multiplier: float, fee_round_trip: float) -> None:
        self.positions[instrument] = OpenPosition(
            side=side, qty=qty, entry_price=entry_price,
            multiplier=multiplier, fee_round_trip=fee_round_trip,
        )

    def close(self, instrument: str, exit_price: float) -> float:
        pos = self.positions.get(instrument)
        if pos is None:
            return 0.0
        direction = 1 if pos.side == "Buy" else -1
        pnl = direction * (exit_price - pos.entry_price) * pos.multiplier * pos.qty
        pnl -= pos.fee_round_trip * pos.qty
        self.realized_pnl += pnl
        del self.positions[instrument]
        return pnl

    def current_equity(self, live_prices: dict[str, float]) -> float:
        """live_prices: {instrument_name: current_price}. Any open
        position whose instrument isn't in live_prices is skipped for
        the unrealized component (its last-known contribution is simply
        not updated that cycle -- log a warning upstream if this happens
        repeatedly, it means a price feed is failing)."""
        equity = self.starting_balance + self.realized_pnl
        for name, pos in self.positions.items():
            price = live_prices.get(name)
            if price is None:
                continue
            direction = 1 if pos.side == "Buy" else -1
            equity += direction * (price - pos.entry_price) * pos.multiplier * pos.qty
        return round(equity, 2)
