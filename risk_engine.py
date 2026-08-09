"""
risk_engine.py

Gatekeeper for the Tradeify Lightning Funded 50K account.
No order reaches Tradovate without passing through RiskEngine.can_trade()
and having its size clamped by RiskEngine.max_risk_dollars().

Account rules encoded here (from account dashboard, 2026-08-06):
    - Trailing max drawdown: hard floor, trails the account's high-water
      mark upward, NEVER moves back down. Breach = account terminated.
    - Daily loss limit: $1,250. Hard stop for the trading day once hit.
    - Consistency limit: 20%. No single day's profit may exceed 20% of
      total cumulative profit toward the $3,000 target. This is checked
      BEFORE a trade closes profitable, not just after -- we cap how much
      profit the bot is allowed to bank in one day.
    - Profit target: $3,000 (evaluation phase).

ASSUMPTIONS THAT NEED CONFIRMING WITH MO:
    1. CONFIRMED (2026-08-09): the trailing drawdown floor recalculates
       only at end-of-day session close, not continuously intraday. It
       IS enforced in real time against live equity between recalcs --
       a breach halts trading immediately even though the floor itself
       only moves once per day. Implemented in update_equity() /
       maybe_roll_daily_reset() below.
    2. The trail amount is currently inferred from the two account
       snapshots as ~$1,910.30 (balance - floor). This may not be the
       fixed trail Tradeify uses long-term (some firms stop trailing once
       balance clears the initial deposit by X). Confirm the actual fixed
       trailing amount in Tradeify's rules PDF before going live -- see
       TRAIL_AMOUNT below.
    3. Daily boundary uses UTC midnight by default. Tradeify's actual
       reset time (often 5pm CT / 6pm ET for futures prop firms) should
       be confirmed and set via `daily_reset_hour_utc`.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Account configuration -- confirm/adjust these against Tradeify's rules doc
# ---------------------------------------------------------------------------

@dataclass
class AccountConfig:
    account_size: float = 50_000.00
    daily_loss_limit: float = 1_250.00
    profit_target: float = 3_000.00
    consistency_cap_pct: float = 0.20          # 20% max single-day share
    trail_amount: float = 1_910.30             # ASSUMPTION 2 above -- confirm
    daily_reset_hour_utc: int = 22             # ASSUMPTION 3 above -- confirm

    # Safety margins -- the bot stops BEFORE the hard rule, not at it.
    # These are deliberately conservative for a funded account.
    drawdown_safety_buffer: float = 300.00      # stop trading $300 above floor
    daily_loss_safety_buffer: float = 150.00    # stop $150 before daily limit
    consistency_safety_margin_pct: float = 0.03  # target 17%, not 20%, cap


@dataclass
class RiskState:
    """Live state the engine tracks. Persist this across restarts (e.g. to
    a small SQLite table like your other bots use) so a Railway redeploy
    doesn't reset drawdown tracking."""
    high_water_mark: float
    current_equity: float
    trailing_floor: float
    daily_pnl: float = 0.0
    daily_start_equity: float = 0.0
    cumulative_profit_since_reset: float = 0.0
    best_single_day_profit: float = 0.0
    last_daily_reset: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    trading_halted_reason: Optional[str] = None


class RiskEngine:
    def __init__(self, cfg: AccountConfig, state: Optional[RiskState] = None):
        self.cfg = cfg
        if state is not None:
            self.state = state
        else:
            floor = round(cfg.account_size - cfg.trail_amount, 2)
            self.state = RiskState(
                high_water_mark=cfg.account_size,
                current_equity=cfg.account_size,
                trailing_floor=floor,
                daily_start_equity=cfg.account_size,
            )

    # -- equity / drawdown tracking -----------------------------------

    def update_equity(self, live_equity: float) -> None:
        """Call this on every price tick / position mark-to-market update.

        CORRECTED (2026-08-09): Tradeify's floor only RECALCULATES at
        end-of-day session close, not continuously through the day as
        new intraday highs are made. It is, however, ENFORCED in real
        time -- a breach against the current (fixed-for-the-day) floor
        halts trading immediately. This method now only updates
        current_equity, tracks the session's peak for use at the next
        EOD rollover, and checks breach -- it does NOT move the floor
        intraday. The floor itself only moves in maybe_roll_daily_reset()."""
        self.state.current_equity = live_equity
        self.state.daily_pnl = live_equity - self.state.daily_start_equity

        if live_equity > self.state.high_water_mark:
            self.state.high_water_mark = live_equity  # peak tracked, NOT applied to floor yet

        if live_equity <= self.state.trailing_floor:
            self.state.trading_halted_reason = (
                f"BREACH: equity {live_equity:.2f} at/below trailing floor "
                f"{self.state.trailing_floor:.2f}"
            )

    def maybe_roll_daily_reset(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        last = self.state.last_daily_reset
        boundary_today = now.replace(
            hour=self.cfg.daily_reset_hour_utc, minute=0, second=0, microsecond=0
        )
        crossed = (now >= boundary_today) and (last < boundary_today)
        if crossed:
            # Floor recalculation happens HERE, once per day, from the
            # session's high-water mark -- not intraday. Still never moves
            # down, per the "One Rule."
            new_floor = round(self.state.high_water_mark - self.cfg.trail_amount, 2)
            self.state.trailing_floor = max(self.state.trailing_floor, new_floor)

            if self.state.daily_pnl > 0:
                self.state.cumulative_profit_since_reset += self.state.daily_pnl
                self.state.best_single_day_profit = max(
                    self.state.best_single_day_profit, self.state.daily_pnl
                )
            self.state.daily_pnl = 0.0
            self.state.daily_start_equity = self.state.current_equity
            self.state.last_daily_reset = now
            # daily loss halt clears on a new day; drawdown halt does not
            if self.state.trading_halted_reason and "BREACH" not in (
                self.state.trading_halted_reason
            ):
                self.state.trading_halted_reason = None

    # -- gatekeeping -----------------------------------------------------

    def can_trade(self) -> tuple[bool, str]:
        s, c = self.state, self.cfg

        if s.trading_halted_reason:
            return False, s.trading_halted_reason

        buffer_to_floor = s.current_equity - s.trailing_floor
        if buffer_to_floor <= c.drawdown_safety_buffer:
            return False, (
                f"Drawdown buffer too thin: {buffer_to_floor:.2f} remaining "
                f"above floor (safety cutoff {c.drawdown_safety_buffer:.2f})"
            )

        daily_loss_remaining = c.daily_loss_limit + s.daily_pnl  # daily_pnl negative when losing
        if daily_loss_remaining <= c.daily_loss_safety_buffer:
            return False, (
                f"Daily loss buffer too thin: {daily_loss_remaining:.2f} "
                f"remaining before hitting the ${c.daily_loss_limit:.2f} limit"
            )

        return True, "OK"

    def max_risk_dollars(self) -> float:
        """Largest $ amount safe to risk on the NEXT trade, after clamping
        to both the drawdown buffer and the remaining daily loss allowance,
        each with its safety margin already subtracted. Position sizing
        should never risk more than this on a single trade -- and in
        practice should risk a fraction of it (e.g. 20-30%) to survive a
        losing streak."""
        s, c = self.state, self.cfg
        room_to_floor = max(0.0, s.current_equity - s.trailing_floor - c.drawdown_safety_buffer)
        room_to_daily = max(
            0.0, c.daily_loss_limit + s.daily_pnl - c.daily_loss_safety_buffer
        )
        return round(min(room_to_floor, room_to_daily), 2)

    def consistency_capped_profit_target_today(self) -> float:
        """How much MORE profit the bot is allowed to bank today without
        breaching the (safety-margined) consistency cap. Once a winning
        trade would push today's total profit past this, the bot should
        stop opening new positions for the day -- even with drawdown and
        daily-loss buffer still available."""
        s, c = self.state, self.cfg
        effective_cap_pct = c.consistency_cap_pct - c.consistency_safety_margin_pct  # 0.17
        # Projected cumulative profit if today closes at current daily_pnl
        projected_cumulative = s.cumulative_profit_since_reset + max(0.0, s.daily_pnl)
        if projected_cumulative <= 0:
            return float("inf")  # no profit banked yet, cap doesn't bind
        max_today_allowed = effective_cap_pct * projected_cumulative / (1 - effective_cap_pct)
        remaining = max_today_allowed - max(0.0, s.daily_pnl)
        return round(max(0.0, remaining), 2)

    def should_stop_for_consistency(self) -> bool:
        return self.consistency_capped_profit_target_today() <= 0


# ---------------------------------------------------------------------------
# Demo using Mo's actual current account numbers
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cfg = AccountConfig()
    state = RiskState(
        high_water_mark=50_207.14,
        current_equity=50_207.14,
        trailing_floor=48_296.84,
        daily_pnl=0.0,
        daily_start_equity=50_207.14,
        cumulative_profit_since_reset=207.14,
        best_single_day_profit=169.24,
    )
    engine = RiskEngine(cfg, state)

    print("=== Current account state ===")
    print(f"Equity:              {engine.state.current_equity:,.2f}")
    print(f"Trailing floor:      {engine.state.trailing_floor:,.2f}")
    ok, reason = engine.can_trade()
    print(f"Can trade?           {ok} ({reason})")
    print(f"Max risk next trade: ${engine.max_risk_dollars():,.2f}")
    print(f"Consistency room:    ${engine.consistency_capped_profit_target_today():,.2f} more profit allowed today")

    print("\n=== Simulating a $150 winning trade today ===")
    engine.update_equity(50_207.14 + 150)
    ok, reason = engine.can_trade()
    print(f"Can trade?           {ok} ({reason})")
    print(f"Consistency room:    ${engine.consistency_capped_profit_target_today():,.2f} more profit allowed today")
    print(f"Should stop (consistency)? {engine.should_stop_for_consistency()}")
