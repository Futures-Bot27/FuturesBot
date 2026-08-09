"""
traderspost_client.py

Sends order signals to TradersPost, which routes them to your linked
Tradeify/Tradovate account. This REPLACES tradovate_client.py's order
placement -- keep tradovate_client.py in the repo for reference/future
use if you ever get direct API access on a separate standalone Tradovate
account, but it is NOT used by main.py anymore.

Webhook format confirmed against TradersPost's documented reference
(2026-08): POST to your strategy's unique webhook URL with a JSON body.
Futures tickers should use the format [ROOT][MONTH CODE][4-DIGIT YEAR],
e.g. "MGCZ2026" -- NOT the Tradovate-style "MGCZ6". Confirm the exact
expected format in your TradersPost strategy's "Submit Signal" test tool
before going live -- ticker formatting mismatches are the most common
cause of a signal silently failing to route.
"""

import os
import requests

TRADERSPOST_WEBHOOK_URL = os.environ.get("TRADERSPOST_WEBHOOK_URL", "")


def send_order(ticker: str, action: str, quantity: int) -> dict:
    """action: 'buy' or 'sell' (lowercase, per TradersPost spec)."""
    if not TRADERSPOST_WEBHOOK_URL:
        raise RuntimeError("TRADERSPOST_WEBHOOK_URL not set in environment.")
    payload = {
        "ticker": ticker,
        "action": action,
        "orderType": "market",
        "quantity": quantity,
    }
    resp = requests.post(TRADERSPOST_WEBHOOK_URL, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json() if resp.content else {"status": resp.status_code}


def send_flatten(ticker: str) -> dict:
    """TradersPost supports an explicit 'exit' / 'close' action -- confirm
    the exact keyword your strategy setup expects via the Submit Signal
    test tool. 'exit' is the documented default for closing a position."""
    if not TRADERSPOST_WEBHOOK_URL:
        raise RuntimeError("TRADERSPOST_WEBHOOK_URL not set in environment.")
    payload = {"ticker": ticker, "action": "exit"}
    resp = requests.post(TRADERSPOST_WEBHOOK_URL, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json() if resp.content else {"status": resp.status_code}
