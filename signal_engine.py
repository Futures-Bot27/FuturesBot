"""
signal_engine.py

Confluence-based signal generator, same architectural pattern as your
other bots (EMA trend filter, RSI, ATR volatility gate, session filter).
Price data comes from Twelve Data (XAU/USD spot as the signal proxy for
MGC futures) -- reusing your existing working pipeline rather than
building an untested Tradovate market-data subscription.

NOTE: XAU/USD spot and MGC futures track closely but are not identical
(futures carry basis/contango vs spot). This is a reasonable proxy for
signal DIRECTION but execution price will differ from the spot price
used to generate the signal. Fine for swing-style entries; would need
tightening for anything scalping-timeframe on the "microscalping
compliant" allowance.
"""

import os
import requests
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"


@dataclass
class SignalResult:
    signal: Signal
    confluence_score: int
    max_score: int
    reasons: list[str]
    price: float


TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")


def fetch_bars(symbol: str, interval: str = "15min", outputsize: int = 100) -> list[dict]:
    resp = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")
    bars = list(reversed(data["values"]))  # oldest first
    return bars


def _closes(bars: list[dict]) -> np.ndarray:
    return np.array([float(b["close"]) for b in bars])


def _highs(bars: list[dict]) -> np.ndarray:
    return np.array([float(b["high"]) for b in bars])


def _lows(bars: list[dict]) -> np.ndarray:
    return np.array([float(b["low"]) for b in bars])


def ema(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2 / (period + 1)
    out = np.zeros_like(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(values: np.ndarray, period: int = 14) -> float:
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    trs = np.zeros(len(closes))
    trs[0] = highs[0] - lows[0]
    for i in range(1, len(closes)):
        trs[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    out = np.zeros_like(trs)
    out[:period] = trs[:period].mean()
    for i in range(period, len(trs)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


# -- session filter ----------------------------------------------------
# Avoid thin/illiquid hours. Adjust to taste -- these are UTC hour bounds
# roughly covering London + NY overlap, same idea as your session filter
# on AURUM/CIPHER.
ACTIVE_SESSION_HOURS_UTC = range(7, 20)


def in_active_session(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now.hour in ACTIVE_SESSION_HOURS_UTC


# -- confluence scoring --------------------------------------------------

def generate_signal(symbol: str, interval: str = "15min", min_score: int = 3) -> SignalResult:
    bars = fetch_bars(symbol, interval=interval, outputsize=100)
    closes = _closes(bars)
    highs = _highs(bars)
    lows = _lows(bars)

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    current_rsi = rsi(closes, 14)
    atr_series = atr(highs, lows, closes, 14)
    current_atr = atr_series[-1]
    atr_percentile = float((atr_series[-50:] < current_atr).mean() * 100)

    price = closes[-1]
    reasons = []
    long_score = 0
    short_score = 0
    max_score = 4

    # 1. Trend: EMA20 vs EMA50
    if ema20[-1] > ema50[-1]:
        long_score += 1
        reasons.append("Trend up (EMA20 > EMA50)")
    elif ema20[-1] < ema50[-1]:
        short_score += 1
        reasons.append("Trend down (EMA20 < EMA50)")

    # 2. Price vs fast EMA (pullback / participation check)
    if price > ema20[-1]:
        long_score += 1
    elif price < ema20[-1]:
        short_score += 1

    # 3. RSI not extreme against the trade direction
    if 45 < current_rsi < 70:
        long_score += 1
        reasons.append(f"RSI {current_rsi:.1f} supports longs")
    elif 30 < current_rsi < 55:
        short_score += 1
        reasons.append(f"RSI {current_rsi:.1f} supports shorts")

    # 4. Volatility gate: avoid dead/illiquid ATR regimes and avoid
    #    outlier spikes (news events) -- stay in the middle band.
    if 20 <= atr_percentile <= 85:
        long_score += 1
        short_score += 1
        reasons.append(f"ATR percentile {atr_percentile:.0f} acceptable")
    else:
        reasons.append(f"ATR percentile {atr_percentile:.0f} outside acceptable band -- skip")

    if not in_active_session():
        reasons.append("Outside active session hours -- skip")
        return SignalResult(Signal.NONE, 0, max_score, reasons, price)

    if long_score >= min_score and long_score > short_score:
        return SignalResult(Signal.BUY, long_score, max_score, reasons, price)
    elif short_score >= min_score and short_score > long_score:
        return SignalResult(Signal.SELL, short_score, max_score, reasons, price)
    else:
        return SignalResult(Signal.NONE, max(long_score, short_score), max_score, reasons, price)
