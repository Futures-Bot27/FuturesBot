# Tradeify Lightning Funded 50K — Automated Trading Bot (v3: multi-instrument)

## What changed in v3: MGC + MNQ simultaneously

The bot now trades **both gold (MGC) and Nasdaq micros (MNQ) at once**,
sharing ONE account-level risk budget rather than getting a full budget
each. This matters: your $1,250 daily loss limit and drawdown floor are
account-wide facts, not per-instrument. `MGC_RISK_SHARE` and
`MNQ_RISK_SHARE` (default 50/50 in `.env.example`) control how the
remaining risk room splits between them each cycle — adjust if you want
one instrument to get a bigger slice.

To trade only one instrument, leave the other's `_TICKER` variable blank
in `.env` — it gets skipped entirely, no code changes needed.

Tradeify permits holding multiple instruments simultaneously and this
isn't a hedge (different product groups), so no rule conflict there.

## What changed from v1

Direct Tradovate API access requires a **standalone live Tradovate
account with API Access purchased** — your Tradeify account is a
sub-account under Tradeify's master account and doesn't expose that
option. Tradeify's officially supported automation route is
**TradersPost**, which connects to your linked Tradeify/Tradovate
account on their end. `tradovate_client.py` is left in the repo but is
**not used** by `main.py` anymore — `traderspost_client.py` replaced it.

**Important consequence:** TradersPost has no read API for account
balance/equity (webhook-in only right now). The risk engine can no
longer pull your real equity automatically. `equity_tracker.py`
estimates it instead, from trades this bot places plus the live gold
price. **You must reconcile this against your real Tradeify balance
regularly** (daily minimum, ideally after every session) via the
`/reconcile` endpoint — see Step 6. Skipping reconciliation lets the
estimate drift from reality, which undermines the entire point of the
drawdown floor.

## Files
- `risk_engine.py` — gatekeeps every trade against your account rules
- `equity_tracker.py` — estimates equity locally (see note above)
- `signal_engine.py` — confluence signals from Twelve Data
- `traderspost_client.py` — sends orders via TradersPost webhook
- `persistence.py` — SQLite state survives redeploys
- `main.py` — FastAPI service wiring it together
- `tradovate_client.py` — unused fallback, kept for reference only

## Still-unconfirmed assumptions (same as before, still matter)
1. **Trailing drawdown basis** — assumed live equity, not EOD balance.
2. **Trail amount ($1,910.30)** — inferred from your screenshots, not
   from Tradeify's actual rules document.

Both live in `risk_engine.py`'s `AccountConfig`. Check Tradeify's rules
doc when you can and correct these if they're off.

## Step-by-step activation

### 1. Create a TradersPost account
- Go to traderspost.io → register.
- Create a new **Strategy**. This is where you'll get your unique
  webhook URL.

### 2. Link your Tradeify/Tradovate account inside TradersPost
- In TradersPost, add a **Broker Connection** → select **Tradovate**.
- Log in with your Tradovate credentials (the same ones you use for the
  Tradeify account) when prompted. TradersPost handles this connection;
  you never expose an API key.
- Under your Strategy, add a **Strategy Subscription** pointing at this
  broker connection/account.

### 3. Confirm the exact ticker format — for BOTH instruments
- In your TradersPost strategy dashboard, click **••• → Submit Signal**.
- Search **MGC** — confirm the exact current front-month string (e.g.
  `MGCZ2026`) → goes in `MGC_TICKER`.
- Search **MNQ** — confirm the exact current front-month string (e.g.
  `MNQU2026`) → goes in `MNQ_TICKER`.
- Format is `[ROOT][MONTH CODE][4-DIGIT YEAR]`, differing from
  Tradovate's own `MGCZ6`-style shorthand — always confirm via this tool
  rather than assuming, since a mismatch fails silently.

### 4. Get your webhook URL
- Still in the Strategy dashboard, find the **Webhook URL** — looks like
  `https://webhooks.traderspost.io/trading/webhook/{uuid}/{password}`.
- This goes in `TRADERSPOST_WEBHOOK_URL`.

### 5. Railway setup
```bash
npm i -g @railway/cli
railway login
cd tradeify_bot
railway init
railway up
```
Set every variable from `.env.example` in Railway's **Variables** tab —
not in a local file, not pasted anywhere else. Particularly:
- `TRADERSPOST_WEBHOOK_URL`
- `MGC_TICKER` and `MNQ_TICKER` (leave either blank to disable that instrument)
- `MGC_RISK_SHARE` / `MNQ_RISK_SHARE` — confirm these sum to 1.0 or less
- `STARTING_BALANCE` — set to your **real, current** Tradeify balance
  right before you deploy
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `DRY_RUN=true` for the first deploy

### 6. First run: `DRY_RUN=true`
- Watch `/status` and Telegram for a few cycles. Confirm signals look
  reasonable and the estimated equity number is sane.
- This costs nothing and validates the signal/risk logic before any
  order is sent.

### 7. Go live
- Flip `DRY_RUN=false` in Railway, redeploy.
- **Immediately after your first few real trades**, check the actual
  balance on Tradeify's dashboard and reconcile:
  ```bash
  curl -X POST https://your-app.up.railway.app/reconcile \
    -H "Content-Type: application/json" \
    -d '{"real_balance": 50412.30}'
  ```
- Make this a standing habit — once per session minimum. The trailing
  drawdown floor is only as accurate as the last reconciliation.

### 8. Know your kill switch
```bash
curl -X POST https://your-app.up.railway.app/flatten
```
This sends a close/exit signal via TradersPost for your current ticker.
Bookmark it. After using it, reconcile equity once you've confirmed the
actual fill/exit price on Tradeify's dashboard.

### 9. Ongoing maintenance
- **Reconcile equity regularly** — this is the one habit that keeps the
  whole risk system honest given no live broker read.
- Update `TRADERSPOST_TICKER` when the MGC contract rolls (quarterly:
  H/M/U/Z cycle).
- Watch consistency room via `/status` — it's your tightest constraint
  right now given the 81.7%/20% starting point.
- Tradeify requires disclosure that you're running an automated
  strategy and requires you to be its sole owner/developer — confirm
  you've met any disclosure step on their end, separate from this build.
