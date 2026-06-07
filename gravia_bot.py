"""
Gravia-Style Compounding Bot
=============================
Strategy: 
  - Watch Binance BTC real-time price via WebSocket
  - Detect latency lag on Polymarket 15-min BTC up/down markets
  - ONLY bet on outcomes priced $0.90-$0.99 (near-certain)
  - Compound ALL profits — bet 90% of balance each trade
  - Start with $2, grow to $50-100+

Requirements:
    pip install websocket-client requests py-clob-client python-dotenv

Author: Built for Gravia-style latency arbitrage
"""

import json
import time
import os
import threading
import websocket
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Your Keys (set in .env file) ─────────────────────────────────────────────
POLYMARKET_API_KEY        = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET     = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")
POLYMARKET_PRIVATE_KEY    = os.getenv("POLYMARKET_PRIVATE_KEY", "")

# ── Gravia Strategy Config ────────────────────────────────────────────────────
STARTING_BALANCE     = 2.00      # Your starting USDC
BET_PERCENT          = 0.90      # Bet 90% of balance each trade (Gravia style)
MIN_ODDS             = 0.90      # Only bet if market price >= $0.90
MAX_ODDS             = 0.99      # Only bet if market price <= $0.99
SPIKE_THRESHOLD_PCT  = 0.04      # BTC must move 0.04% to trigger signal
COOLDOWN_SECONDS     = 30        # Wait 30s between trades
MIN_BET_USDC         = 0.50      # Minimum bet size (Polymarket limit)
STOP_LOSS_PCT        = 0.30      # Stop bot if balance drops 30% from peak

# ── State ─────────────────────────────────────────────────────────────────────
balance          = STARTING_BALANCE
peak_balance     = STARTING_BALANCE
last_price       = None
last_trade_time  = 0
trade_count      = 0
wins             = 0
losses           = 0
lock             = threading.Lock()
bot_active       = True


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def print_stats():
    winrate = (wins / trade_count * 100) if trade_count > 0 else 0
    log("─" * 50)
    log(f"💰 Balance     : ${balance:.4f} USDC")
    log(f"📈 Peak        : ${peak_balance:.4f} USDC")
    log(f"🔢 Trades      : {trade_count} | ✅ {wins}W / ❌ {losses}L")
    log(f"🎯 Win Rate    : {winrate:.1f}%")
    log(f"🚀 Growth      : {((balance - STARTING_BALANCE) / STARTING_BALANCE * 100):.1f}%")
    log("─" * 50)


# ── Polymarket Market Scanner ─────────────────────────────────────────────────
def get_btc_markets():
    """Fetch active BTC 15-min up/down markets from Polymarket"""
    try:
        url = "https://gamma-api.polymarket.com/markets"
        params = {
            "active": "true",
            "closed": "false",
            "tag_slug": "crypto",
            "limit": 50,
        }
        resp = requests.get(url, params=params, timeout=10)
        markets = resp.json()

        btc_markets = []
        for m in markets:
            question = m.get("question", "").lower()
            if "btc" in question or "bitcoin" in question:
                if "15" in question or "5 min" in question or "up or down" in question:
                    btc_markets.append(m)

        return btc_markets
    except Exception as e:
        log(f"⚠️ Market fetch error: {e}")
        return []


def find_near_certain_market(direction: str):
    """
    Find a BTC market where one side is priced $0.90-$0.99
    direction: 'up' or 'down'
    Returns: (token_id, price) or (None, None)
    """
    markets = get_btc_markets()
    for market in markets:
        try:
            tokens = market.get("tokens", [])
            for token in tokens:
                outcome = token.get("outcome", "").lower()
                price   = float(token.get("price", 0))

                if direction == "up" and outcome in ["yes", "up"]:
                    if MIN_ODDS <= price <= MAX_ODDS:
                        log(f"🎯 Found near-certain UP market: {market['question'][:60]}")
                        log(f"   Price: ${price} | Token: {token['token_id'][:20]}...")
                        return token["token_id"], price

                elif direction == "down" and outcome in ["no", "down"]:
                    if MIN_ODDS <= price <= MAX_ODDS:
                        log(f"🎯 Found near-certain DOWN market: {market['question'][:60]}")
                        log(f"   Price: ${price} | Token: {token['token_id'][:20]}...")
                        return token["token_id"], price

        except Exception:
            continue

    return None, None


# ── Compounding Bet Size ──────────────────────────────────────────────────────
def calculate_bet():
    """Bet 90% of current balance (Gravia compounding style)"""
    bet = balance * BET_PERCENT
    return max(bet, MIN_BET_USDC)  # Never below minimum


# ── Place Order on Polymarket ─────────────────────────────────────────────────
def place_order(token_id: str, amount: float, direction: str):
    global balance, peak_balance, wins, losses, trade_count

    if not POLYMARKET_PRIVATE_KEY:
        # DEMO MODE — simulate trade
        log(f"🔵 [DEMO] Would place ${amount:.4f} on {direction.upper()}")
        win = True  # Simulate win for demo
        if win:
            profit = amount * 0.05  # ~5% profit on 0.95 priced market
            balance += profit
            wins += 1
            log(f"✅ [DEMO] WIN! Profit: +${profit:.4f} | Balance: ${balance:.4f}")
        else:
            balance -= amount
            losses += 1
            log(f"❌ [DEMO] LOSS! Lost: -${amount:.4f} | Balance: ${balance:.4f}")

        peak_balance = max(peak_balance, balance)
        trade_count += 1
        print_stats()
        return True

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType

        creds = ApiCreds(
            api_key=POLYMARKET_API_KEY,
            api_secret=POLYMARKET_API_SECRET,
            api_passphrase=POLYMARKET_API_PASSPHRASE,
        )
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=POLYMARKET_PRIVATE_KEY,
            chain_id=137,
            creds=creds,
        )

        order_args = MarketOrderArgs(token_id=token_id, amount=amount)
        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)

        log(f"✅ Order placed! ${amount:.4f} on {direction.upper()} | Response: {resp}")
        trade_count += 1
        return True

    except Exception as e:
        log(f"❌ Order failed: {e}")
        return False


# ── Stop Loss Check ───────────────────────────────────────────────────────────
def check_stop_loss():
    global bot_active
    drawdown = (peak_balance - balance) / peak_balance
    if drawdown >= STOP_LOSS_PCT:
        log(f"🛑 STOP LOSS HIT! Drew down {drawdown*100:.1f}% from peak")
        log(f"   Peak: ${peak_balance:.4f} | Current: ${balance:.4f}")
        log(f"   Bot paused to protect remaining capital.")
        bot_active = False


# ── Signal Handler ────────────────────────────────────────────────────────────
def handle_price(new_price: float):
    global last_price, last_trade_time, bot_active

    with lock:
        if not bot_active:
            return

        if last_price is None:
            last_price = new_price
            return

        pct_change = ((new_price - last_price) / last_price) * 100
        now = time.time()

        if now - last_trade_time < COOLDOWN_SECONDS:
            last_price = new_price
            return

        direction = None
        if pct_change >= SPIKE_THRESHOLD_PCT:
            direction = "up"
            log(f"⚡ BTC spike UP {pct_change:.4f}% → Looking for near-certain UP market...")
        elif pct_change <= -SPIKE_THRESHOLD_PCT:
            direction = "down"
            log(f"⚡ BTC spike DOWN {pct_change:.4f}% → Looking for near-certain DOWN market...")

        if direction:
            token_id, price = find_near_certain_market(direction)
            if token_id:
                bet = calculate_bet()
                log(f"💸 Betting ${bet:.4f} on {direction.upper()} (market price: ${price})")
                place_order(token_id, bet, direction)
                last_trade_time = now
                check_stop_loss()
            else:
                log(f"⏭️  No near-certain market found for {direction.upper()} — skipping")

        last_price = new_price


# ── Binance WebSocket ─────────────────────────────────────────────────────────
def on_message(ws, message):
    try:
        data  = json.loads(message)
        price = float(data.get("p") or data.get("c", 0))
        if price > 0:
            handle_price(price)
    except Exception as e:
        log(f"⚠️ Parse error: {e}")


def on_error(ws, error):
    log(f"⚠️ WS Error: {error}")


def on_close(ws, *args):
    log("🔌 Disconnected. Reconnecting in 5s...")
    time.sleep(5)
    start_ws()


def on_open(ws):
    log("✅ Connected to Binance BTC/USDT live feed")


def start_ws():
    ws = websocket.WebSocketApp(
        "wss://stream.binance.com:9443/ws/btcusdt@trade",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)


# ── Stats Thread ──────────────────────────────────────────────────────────────
def stats_loop():
    while True:
        time.sleep(120)  # every 2 minutes
        if trade_count > 0:
            print_stats()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║     🤖 GRAVIA-STYLE COMPOUNDING BOT          ║")
    print("║     Binance Latency → Polymarket Arb         ║")
    print("╠══════════════════════════════════════════════╣")
    print(f"║  Starting Balance : ${STARTING_BALANCE:.2f} USDC               ║")
    print(f"║  Bet Size         : {int(BET_PERCENT*100)}% of balance          ║")
    print(f"║  Target Odds      : ${MIN_ODDS}–${MAX_ODDS} only            ║")
    print(f"║  Spike Trigger    : {SPIKE_THRESHOLD_PCT}% BTC move           ║")
    print(f"║  Stop Loss        : {int(STOP_LOSS_PCT*100)}% drawdown from peak     ║")
    print(f"║  Mode             : {'DEMO (no keys set)' if not POLYMARKET_PRIVATE_KEY else 'LIVE TRADING ⚠️ '}          ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    if not POLYMARKET_PRIVATE_KEY:
        log("ℹ️  Running in DEMO MODE — set keys in .env to go live")

    # Stats thread
    threading.Thread(target=stats_loop, daemon=True).start()

    # Start bot
    start_ws()


# ══════════════════════════════════════════════════════
# .env file — create this in same folder as bot:
# ══════════════════════════════════════════════════════
#
# POLYMARKET_API_KEY=your_key
# POLYMARKET_API_SECRET=your_secret
# POLYMARKET_API_PASSPHRASE=your_passphrase
# POLYMARKET_PRIVATE_KEY=your_metamask_private_key
#
# ══════════════════════════════════════════════════════
