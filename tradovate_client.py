"""
tradovate_client.py

Minimal Tradovate REST client covering exactly what this bot needs:
auth, account/cash-balance polling (feeds the risk engine's equity input),
and order placement.

Confirmed against Tradovate's own API docs / example repo (2026-08):
    - Base URL demo:  https://demo.tradovateapi.com/v1
    - Base URL live:  https://live.tradovateapi.com/v1
    - Auth:           POST /auth/accessTokenRequest
    - Orders:         POST /order/placeOrder  (isAutomated MUST be set)
    - Bearer token required on every subsequent request.

NOT YET VERIFIED LIVE -- these calls are written against documented
endpoints but have not been run against a real Tradovate demo account in
this session (no network access from this environment). Before touching
the funded account:
    1. Run everything against the DEMO base URL first with a Tradovate
       demo account.
    2. Confirm order fills and account/cashBalance responses look as
       expected.
    3. Only then switch TRADOVATE_ENV=live in your .env.

Market data (price bars for signal generation) intentionally does NOT
come from Tradovate here -- it reuses your existing Twelve Data pipeline,
same as AURUM/the OANDA XAU bot. Tradovate is used only for account state
and execution. This keeps the proven signal infra and avoids building an
untested Tradovate WebSocket market-data subscription blind.
"""

import os
import time
import requests
from dataclasses import dataclass


@dataclass
class TradovateCredentials:
    name: str          # Tradovate username
    password: str
    app_id: str         # your registered API app name
    app_version: str
    cid: str             # API key / client id
    sec: str             # API secret
    device_id: str


class TradovateClient:
    def __init__(self, creds: TradovateCredentials, env: str = "demo"):
        self.creds = creds
        self.base_url = (
            "https://live.tradovateapi.com/v1"
            if env == "live"
            else "https://demo.tradovateapi.com/v1"
        )
        self._token: str | None = None
        self._token_expiry: float = 0
        self._account_id: int | None = None
        self._account_spec: str | None = None

    # -- auth ------------------------------------------------------------

    def _authenticate(self) -> None:
        resp = requests.post(
            f"{self.base_url}/auth/accessTokenRequest",
            json={
                "name": self.creds.name,
                "password": self.creds.password,
                "appId": self.creds.app_id,
                "appVersion": self.creds.app_version,
                "cid": self.creds.cid,
                "sec": self.creds.sec,
                "deviceId": self.creds.device_id,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "accessToken" not in data:
            raise RuntimeError(f"Tradovate auth failed: {data}")
        self._token = data["accessToken"]
        # Tradovate tokens are short-lived; refresh proactively at 80% of life.
        expires_in = data.get("expirationTime")
        self._token_expiry = time.time() + (23 * 3600)  # conservative default ~1 day
        if expires_in:
            self._token_expiry = time.time() + 0.8 * expires_in

    def _ensure_token(self) -> None:
        if self._token is None or time.time() >= self._token_expiry:
            self._authenticate()

    def _headers(self) -> dict:
        self._ensure_token()
        return {"Authorization": f"Bearer {self._token}"}

    # -- account -----------------------------------------------------------

    def get_account_id(self) -> int:
        """Resolve and cache the numeric account id for this login."""
        if self._account_id is not None:
            return self._account_id
        resp = requests.get(f"{self.base_url}/account/list", headers=self._headers(), timeout=15)
        resp.raise_for_status()
        accounts = resp.json()
        if not accounts:
            raise RuntimeError("No Tradovate accounts found for this login.")
        # If multiple accounts exist under this login, set TRADOVATE_ACCOUNT_SPEC
        # in .env to disambiguate. Otherwise take the first.
        target_spec = os.environ.get("TRADOVATE_ACCOUNT_SPEC")
        chosen = accounts[0]
        if target_spec:
            for a in accounts:
                if a.get("name") == target_spec:
                    chosen = a
                    break
        self._account_id = chosen["id"]
        self._account_spec = chosen.get("name")
        return self._account_id

    def get_live_equity(self) -> float:
        """Pull cash balance + open P&L. This is what should feed
        RiskEngine.update_equity() every polling cycle."""
        account_id = self.get_account_id()
        resp = requests.get(
            f"{self.base_url}/cashBalance/getCashBalanceSnapshot",
            params={"accountId": account_id},
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # netLiq / total value including open positions -- confirm exact
        # field name against a live demo response before trusting this
        # blind; falling back to cashBalance if netLiq isn't present.
        return float(data.get("netLiq", data.get("cashBalance", 0.0)))

    # -- contracts -----------------------------------------------------

    def find_contract(self, symbol: str) -> dict:
        """symbol e.g. 'MGCZ6' -- the exact front-month contract symbol.
        Tradovate does not resolve 'MGC' generically to the active month;
        you must supply the specific contract symbol. Check Tradovate's
        UI or /contract/find for the current front month periodically --
        this does NOT auto-roll contracts."""
        resp = requests.get(
            f"{self.base_url}/contract/find",
            params={"name": symbol},
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            raise RuntimeError(f"Contract '{symbol}' not found -- check the symbol/expiry.")
        return data

    # -- orders --------------------------------------------------------

    def place_market_order(self, symbol: str, action: str, qty: int) -> dict:
        """action: 'Buy' or 'Sell'. isAutomated=True is required for
        algo-placed orders per Tradovate's terms -- do not omit it."""
        contract = self.find_contract(symbol)
        account_id = self.get_account_id()
        payload = {
            "accountSpec": self._account_spec,
            "accountId": account_id,
            "contractId": contract["id"],
            "action": action,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
        }
        resp = requests.post(
            f"{self.base_url}/order/placeOrder",
            json=payload,
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def flatten_position(self, symbol: str) -> dict | None:
        """Emergency / EOD flatten. Checks open position qty and sends an
        opposing market order to close it."""
        account_id = self.get_account_id()
        resp = requests.get(
            f"{self.base_url}/position/list",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        positions = [p for p in resp.json() if p.get("accountId") == account_id]
        contract = self.find_contract(symbol)
        pos = next((p for p in positions if p.get("contractId") == contract["id"]), None)
        if not pos or pos.get("netPos", 0) == 0:
            return None
        net = pos["netPos"]
        action = "Sell" if net > 0 else "Buy"
        return self.place_market_order(symbol, action, abs(net))
