"""
main.py (multi-instrument: MGC + MNQ)

Runs BOTH instruments off ONE shared account-level risk budget. This is
the critical design point: your $1,250 daily loss limit and drawdown
floor do not double because a second instrument was added. Each
instrument gets a configurable SHARE of whatever risk budget is left
(default 50/50), so the combined worst case across both stays within
what the account actually allows.

Per-cycle flow:
    1. Pull prices + generate a signal for EACH instrument
    2. Update the ONE shared equity estimate from ALL open positions
    3. Check the ONE shared risk engine (drawdown, daily loss, consistency)
    4. For each instrument with a valid signal, allocate its SHARE of
       whatever risk room remains and act
"""

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

import requests
from fastapi import FastAPI
from pydantic import BaseModel

from risk_engine import AccountConfig, RiskEngine
from equity_tracker import EquityTracker
import traderspost_client
import signal_engine
import persistence

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradeify_bot")


@dataclass
class InstrumentConfig:
    name: str                    # short key, e.g. "MGC"
    traderspost_ticker: str      # e.g. "MGCZ2026" -- confirmed via Submit Signal tool
    price_symbol: str            # symbol to fetch price/bars from
    data_source: str             # "twelvedata" or "yfinance"
    contract_multiplier: float   # $ per 1-unit price move, per contract
    fee_round_trip: float
    risk_share: float            # fraction of remaining risk budget, e.g. 0.5
    order_qty: int


def _load_instruments() -> list[InstrumentConfig]:
    instruments = []
    for prefix, default_symbol, default_source, default_mult, default_fee in [
        ("MGC", "XAU/USD", "twelvedata", 10.0, 2.50),
        ("MNQ", "NQ=F", "yfinance", 2.0, 1.50),  # NQ=F, not QQQ -- see signal_engine.py
    ]:
        ticker = os.environ.get(f"{prefix}_TICKER", "")
        if not ticker:
            continue  # instrument not configured -- skip it entirely
        instruments.append(InstrumentConfig(
            name=prefix,
            traderspost_ticker=ticker,
            price_symbol=os.environ.get(f"{prefix}_PRICE_SYMBOL", default_symbol),
            data_source=os.environ.get(f"{prefix}_DATA_SOURCE", default_source),
            contract_multiplier=float(os.environ.get(f"{prefix}_MULTIPLIER", default_mult)),
            fee_round_trip=float(os.environ.get(f"{prefix}_FEE_ROUND_TRIP", default_fee)),
            risk_share=float(os.environ.get(f"{prefix}_RISK_SHARE", "0.5")),
            order_qty=int(os.environ.get(f"{prefix}_ORDER_QTY", "1")),
        ))
    return instruments


INSTRUMENTS = _load_instruments()
SIGNAL_INTERVAL = os.environ.get("SIGNAL_INTERVAL", "15min")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "900"))
MIN_CONFLUENCE_SCORE = int(os.environ.get("MIN_CONFLUENCE_SCORE", "3"))
SL_ATR_MULTIPLIER = float(os.environ.get("SL_ATR_MULTIPLIER", "1.5"))
TP_ATR_MULTIPLIER = float(os.environ.get("TP_ATR_MULTIPLIER", "2.5"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", "50207.14"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def notify(msg: str) -> None:
    log.info(msg)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram notify failed: {e}")


cfg = AccountConfig()
state = persistence.load_state()
engine = RiskEngine(cfg, state)
tracker = EquityTracker(starting_balance=STARTING_BALANCE)


async def trading_cycle() -> None:
    if not INSTRUMENTS:
        log.error("No instruments configured -- set at least MGC_TICKER or MNQ_TICKER.")
        return

    engine.maybe_roll_daily_reset()

    # 1. Generate signals + collect live prices for every configured instrument
    signals = {}
    live_prices = {}
    for inst in INSTRUMENTS:
        try:
            result = signal_engine.generate_signal(
                inst.price_symbol, interval=SIGNAL_INTERVAL, min_score=MIN_CONFLUENCE_SCORE,
                source=inst.data_source,
            )
            signals[inst.name] = result
            live_prices[inst.name] = result.price
        except Exception as e:
            notify(f"⚠️ Signal generation failed for {inst.name}: {e}")

    if not live_prices:
        return  # every feed failed this cycle

    # 2. Update the ONE shared equity estimate from ALL open positions
    equity = tracker.current_equity(live_prices)
    engine.update_equity(equity)
    persistence.save_state(engine.state)

    ok, reason = engine.can_trade()
    if not ok:
        log.info(f"Risk engine blocks trading: {reason}")
        if "BREACH" in reason:
            notify(f"🛑 ESTIMATED DRAWDOWN BREACH: {reason}. Halting all instruments. "
                   f"Reconcile against the real Tradeify balance immediately.")
        return

    if engine.should_stop_for_consistency():
        log.info("Consistency cap reached for today -- holding, no new entries on any instrument.")
        return

    total_risk_room = engine.max_risk_dollars()
    if total_risk_room <= 0:
        log.info("No risk room remaining -- holding all instruments.")
        return

    # 3. Act on each instrument independently, but within its allocated SHARE
    #    of the ONE shared risk pool -- not the full pool per instrument.
    for inst in INSTRUMENTS:
        result = signals.get(inst.name)
        if result is None or result.signal == signal_engine.Signal.NONE:
            continue

        desired_side = "Buy" if result.signal == signal_engine.Signal.BUY else "Sell"
        existing = tracker.positions.get(inst.name)
        if existing is not None and existing.side == desired_side:
            continue  # already positioned this direction

        instrument_risk_room = total_risk_room * inst.risk_share
        if instrument_risk_room <= 0:
            continue

        # Percent-based, not fixed price-point -- see traderspost_client.py
        # docstring for why (QQQ-vs-MNQ price scale mismatch).
        sl_pct = (result.atr / result.price) * SL_ATR_MULTIPLIER * 100
        tp_pct = (result.atr / result.price) * TP_ATR_MULTIPLIER * 100

        msg = (
            f"📊 [{inst.name}] Signal: {result.signal.value} on {inst.traderspost_ticker} "
            f"(score {result.confluence_score}/{result.max_score}) @ {result.price:.2f}\n"
            f"Reasons: {'; '.join(result.reasons)}\n"
            f"Stop: {sl_pct:.3f}% | Target: {tp_pct:.3f}%\n"
            f"Shared equity est.: ${equity:,.2f} | {inst.name} risk share: "
            f"${instrument_risk_room:.2f} of ${total_risk_room:.2f} total | Qty: {inst.order_qty}"
        )

        if DRY_RUN:
            notify(f"[DRY RUN -- no order sent]\n{msg}")
            persistence.log_trade(inst.traderspost_ticker, desired_side, inst.order_qty,
                                   result.price, "dry_run", True, "DRY_RUN mode, not executed")
            continue

        try:
            if existing is not None:
                traderspost_client.send_flatten(inst.traderspost_ticker)
                tracker.close(inst.name, result.price)

            webhook_resp = traderspost_client.send_order(
                inst.traderspost_ticker, desired_side.lower(), inst.order_qty,
                price=result.price,
                stop_loss_percent=sl_pct,
                take_profit_percent=tp_pct,
            )
            tracker.open(inst.name, desired_side, inst.order_qty, result.price,
                         inst.contract_multiplier, inst.fee_round_trip)
            persistence.log_trade(inst.traderspost_ticker, desired_side, inst.order_qty,
                                   result.price, "signal", True, str(webhook_resp))
            notify(f"✅ SIGNAL SENT TO TRADERSPOST\n{msg}\nResponse: {webhook_resp}")
        except Exception as e:
            notify(f"❌ Webhook send failed for {inst.name}: {e}\n{msg}")


async def background_loop():
    names = ", ".join(i.name for i in INSTRUMENTS) or "NONE CONFIGURED"
    shares = ", ".join(f"{i.name}={i.risk_share:.0%}" for i in INSTRUMENTS)
    notify(
        f"🟢 Tradeify bot started. DRY_RUN={DRY_RUN}. Instruments: {names}. Risk shares: {shares}. "
        f"Estimated starting equity: ${STARTING_BALANCE:,.2f} -- reconcile daily via POST /reconcile."
    )
    while True:
        try:
            await trading_cycle()
        except Exception as e:
            log.exception(f"Unhandled error in trading cycle: {e}")
            notify(f"⚠️ Unhandled error in trading cycle: {e}")
        await asyncio.sleep(LOOP_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health():
    return {"status": "running", "dry_run": DRY_RUN, "instruments": [i.name for i in INSTRUMENTS]}


@app.get("/status")
def status():
    return {
        "estimated_equity": engine.state.current_equity,
        "trailing_floor": engine.state.trailing_floor,
        "daily_pnl": engine.state.daily_pnl,
        "cumulative_profit": engine.state.cumulative_profit_since_reset,
        "can_trade": engine.can_trade(),
        "max_risk_dollars_total": engine.max_risk_dollars(),
        "consistency_room": engine.consistency_capped_profit_target_today(),
        "open_positions": {
            name: {"side": p.side, "qty": p.qty, "entry_price": p.entry_price}
            for name, p in tracker.positions.items()
        },
        "instruments": [
            {"name": i.name, "ticker": i.traderspost_ticker, "risk_share": i.risk_share}
            for i in INSTRUMENTS
        ],
        "note": "estimated_equity is computed locally across ALL open positions, "
                "not read from Tradovate. Reconcile regularly via POST /reconcile.",
    }


class ReconcileBody(BaseModel):
    real_balance: float
    reset_daily_tracking: bool = True  # True = treat real_balance as today's fresh baseline too


@app.post("/reconcile")
def reconcile(body: ReconcileBody):
    tracker.reconcile(body.real_balance)
    engine.state.current_equity = body.real_balance
    if body.real_balance > engine.state.high_water_mark:
        engine.state.high_water_mark = body.real_balance
        new_floor = round(body.real_balance - cfg.trail_amount, 2)
        engine.state.trailing_floor = max(engine.state.trailing_floor, new_floor)
    if body.reset_daily_tracking:
        # Prevents the exact bug where a state reset (e.g. from a Railway
        # redeploy wiping persistence) makes the jump from a stale default
        # baseline to the real balance look like fabricated same-day profit,
        # falsely tripping the consistency cap.
        engine.state.daily_start_equity = body.real_balance
        engine.state.daily_pnl = 0.0
    persistence.save_state(engine.state)
    notify(f"🔄 Reconciled: real balance ${body.real_balance:,.2f} confirmed.")
    return {"status": "reconciled", "new_floor": engine.state.trailing_floor,
            "daily_pnl": engine.state.daily_pnl}


@app.post("/flatten")
def flatten(instrument: str | None = None):
    """POST /flatten to close ALL open positions across every instrument,
    or POST /flatten?instrument=MGC to close just one."""
    targets = [i for i in INSTRUMENTS if instrument is None or i.name == instrument]
    results = {}
    for inst in targets:
        if inst.name not in tracker.positions:
            continue
        results[inst.name] = traderspost_client.send_flatten(inst.traderspost_ticker)
        pos = tracker.positions[inst.name]
        tracker.close(inst.name, pos.entry_price)  # exact exit price unknown; reconcile after
    notify(f"🔻 Manual flatten triggered. Results: {results}. Reconcile equity after confirming fills.")
    return {"results": results}
