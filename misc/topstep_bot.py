import os
import sys
import time
import json
import asyncio
import httpx
import logging
from datetime import datetime, timezone, timedelta

# ============================================================
# CONFIGURATION
# ============================================================

# Topstep API Base
API_URL = "https://api.topstepx.com/api"

# Local stream_es.py API
LOCAL_STREAM_URL = "http://localhost:8080/api/data"

# VWAP Strategy Parameters (From topstep_top4.py)
TICK_SIZE = 0.25
TICK_VALUE = 12.50
Z_THRESH = 1.5           # Enter when absolute Z-Score > 1.5
CONTRACTS = 2            # Number of ES contracts
STOP_RATIO = 0.5         # Stop distance is 50% of the target distance
MIN_STOP_TICKS = 6       # Minimum stop distance (6 ticks = 1.5 pts)
MAX_STOP_TICKS = 24      # Maximum stop distance (24 ticks = 6.0 pts)
TRAIL_ACTIVATE = 20      # Ticks of profit to activate trailing stop
TRAIL_DISTANCE = 16      # Trailing stop lags best price by this many ticks
EXIT_MIN_TICKS = 12      # Minimum ticks in profit to allow a target exit
TIME_STOP_MINS = 90      # Max time in trade (in bars/minutes)
DRY_RUN = True           # Set to False to actually place orders!

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("TopstepBot")

# ============================================================
# TOPSTEP API CLIENT
# ============================================================
class TopstepClient:
    def __init__(self, username, api_key):
        self.username = username
        self.api_key = api_key
        self.token = None
        self.headers = {}
        self.account_id = None
        self.contract_id = None
        self.client = httpx.AsyncClient(timeout=10.0)

    async def authenticate(self):
        logger.info("Authenticating with TopstepX...")
        r = await self.client.post(f"{API_URL}/Auth/loginKey", json={
            "userName": self.username,
            "apiKey": self.api_key
        })
        auth = r.json()
        self.token = auth.get("token")
        if not self.token:
            logger.error(f"Authentication failed: {auth}")
            sys.exit(1)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        logger.info("✓ Authentication successful.")

    async def setup_account_and_contract(self):
        # 1. Get Account
        r = await self.client.post(f"{API_URL}/Account/search", json={"onlyActiveAccounts": True}, headers=self.headers)
        accts = r.json().get("accounts", [])
        # Prefer the Practice Account (PRAC-V2)
        prac_accts = [a for a in accts if "PRAC" in a.get("name", "").upper() and a.get("canTrade")]
        if not prac_accts:
            # Fallback to any tradable account
            prac_accts = [a for a in accts if a.get("canTrade")]
        if not prac_accts:
            logger.error("No active tradable accounts found.")
            sys.exit(1)
        self.account_id = prac_accts[0]["id"]
        logger.info(f"✓ Using Account: {prac_accts[0]['name']} (ID: {self.account_id})")

        # 2. Get ES Contract
        r = await self.client.post(f"{API_URL}/Contract/available", json={"live": False}, headers=self.headers)
        contracts = r.json().get("contracts", [])
        # Find exactly the E-mini S&P 500 front month
        es_contracts = [c for c in contracts if c.get("symbolId") == "F.US.EP" or c.get("description", "").startswith("E-Mini S&P")]
        if not es_contracts:
            logger.error("Could not find ES contract.")
            sys.exit(1)
        self.contract_id = es_contracts[0]["id"]
        logger.info(f"✓ Using Contract: {es_contracts[0]['name']} (ID: {self.contract_id})")

    async def place_market_order(self, action: str, size: int):
        """
        Places a Market order.
        action: "BUY" or "SELL"
        """
        side = 0 if action.upper() == "BUY" else 1
        payload = {
            "accountId": self.account_id,
            "contractId": self.contract_id,
            "type": 2, # 2 = Market Order
            "side": side,
            "size": size,
            "customTag": f"BOT_{int(time.time())}"
        }
        logger.info(f"ORDER -> {action} {size} {self.contract_id} @ MKT")
        
        if DRY_RUN:
            logger.info(f"  [DRY RUN] Would have POSTed: {payload}")
            return {"success": True, "dry_run": True}
        else:
            r = await self.client.post(f"{API_URL}/Order/place", json=payload, headers=self.headers)
            res = r.json()
            if res.get("success"):
                logger.info(f"  [SUCCESS] Order placed.")
            else:
                logger.error(f"  [FAILED] {res}")
            return res

# ============================================================
# STRATEGY STATE & LOGIC
# ============================================================
class VWAPBot:
    def __init__(self, api_client: TopstepClient):
        self.api = api_client
        self.pos = None # None or dict: {"dir": "LONG"/"SHORT", "ep": price, "sl": stop, "tp": target, "bp": best_price, "bar_idx": int}
        self.trades_today = 0
        self.last_bar_idx = -1
        self.polling_client = httpx.AsyncClient(timeout=2.0)

    async def close_position(self, action, price, reason):
        logger.info(f"CLOSING {self.pos['dir']} Position at {price:.2f} (Reason: {reason})")
        # To close a LONG, we SELL. To close a SHORT, we BUY.
        close_action = "SELL" if self.pos["dir"] == "LONG" else "BUY"
        await self.api.place_market_order(close_action, CONTRACTS)
        self.pos = None

    async def open_position(self, direction, price, target, z_score, bar_idx):
        if self.trades_today >= 4:
            logger.info("Max trades (4) reached for today. Skipping entry.")
            return

        dist = abs(price - target)
        sd = max(min(dist * STOP_RATIO, MAX_STOP_TICKS * TICK_SIZE), MIN_STOP_TICKS * TICK_SIZE)
        
        stop_price = round((price - sd) / TICK_SIZE) * TICK_SIZE if direction == "LONG" else round((price + sd) / TICK_SIZE) * TICK_SIZE
        target_price = target + 4*TICK_SIZE if direction == "LONG" else target - 4*TICK_SIZE
        
        logger.info(f"ENTRY SIGNAL: {direction} @ {price:.2f} | Target: {target_price:.2f} | Stop: {stop_price:.2f} | Z={z_score:.2f}")
        
        # Place the market order
        action = "BUY" if direction == "LONG" else "SELL"
        res = await self.api.place_market_order(action, CONTRACTS)
        
        if res.get("success"):
            self.pos = {
                "dir": direction,
                "ep": price,
                "sl": stop_price,
                "tp": target_price,
                "bp": price,
                "bar_idx": bar_idx
            }
            self.trades_today += 1
            logger.info(f"Trade {self.trades_today}/4 executed successfully.")

    async def tick(self):
        try:
            r = await self.polling_client.get(LOCAL_STREAM_URL)
            if r.status_code != 200: return
            d = r.json()
        except:
            return # stream_es.py offline or parsing failed

        # Extract metrics
        price = d.get("live_last", 0) or d.get("last", 0)
        vwap = d.get("vwap", 0)
        std = d.get("vwap_std", 0)
        z = d.get("z_score", 0)
        bars = d.get("bars", [])
        bar_idx = len(bars)
        rth = d.get("rth", False)
        
        if not price or not vwap or not std or bar_idx < 15: 
            return # Data warming up
            
        if not rth:
            # We don't enter or hold trades outside RTH per the original script
            if self.pos:
                await self.close_position("CLOSE", price, "END OF RTH")
            return

        # ----------------------------------------
        # Position Management (Trailing Stops, Targets)
        # ----------------------------------------
        if self.pos:
            bars_in_trade = bar_idx - self.pos["bar_idx"]
            
            if self.pos["dir"] == "LONG":
                # Update best price
                if price > self.pos["bp"]: self.pos["bp"] = price
                ur_ticks = (self.pos["bp"] - self.pos["ep"]) / TICK_SIZE
                cur_ticks = (price - self.pos["ep"]) / TICK_SIZE
                
                # Check Stop Loss
                if price <= self.pos["sl"]:
                    await self.close_position("SELL", price, "STOP LOSS")
                    return
                # Check Target
                if price >= self.pos["tp"] and cur_ticks >= EXIT_MIN_TICKS:
                    await self.close_position("SELL", price, "PROFIT TARGET")
                    return
                # Trailing Stop Activation
                if ur_ticks >= TRAIL_ACTIVATE:
                    tl = round((self.pos["bp"] - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl > self.pos["sl"]:
                        self.pos["sl"] = tl
                        logger.info(f"Trailing Stop moved up to {tl:.2f}")
                # Breakeven Lock
                if ur_ticks >= 10 and self.pos["sl"] < self.pos["ep"]:
                    self.pos["sl"] = self.pos["ep"]
                    logger.info(f"Stop Loss locked to Break Even at {self.pos['ep']:.2f}")
                # Time Stop
                if bars_in_trade > TIME_STOP_MINS and cur_ticks > -4:
                    await self.close_position("SELL", price, "TIME STOP")
                    return

            else: # SHORT
                # Update best price
                if price < self.pos["bp"]: self.pos["bp"] = price
                ur_ticks = (self.pos["ep"] - self.pos["bp"]) / TICK_SIZE
                cur_ticks = (self.pos["ep"] - price) / TICK_SIZE
                
                # Check Stop Loss
                if price >= self.pos["sl"]:
                    await self.close_position("BUY", price, "STOP LOSS")
                    return
                # Check Target
                if price <= self.pos["tp"] and cur_ticks >= EXIT_MIN_TICKS:
                    await self.close_position("BUY", price, "PROFIT TARGET")
                    return
                # Trailing Stop Activation
                if ur_ticks >= TRAIL_ACTIVATE:
                    tl = round((self.pos["bp"] + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl < self.pos["sl"]:
                        self.pos["sl"] = tl
                        logger.info(f"Trailing Stop moved down to {tl:.2f}")
                # Breakeven Lock
                if ur_ticks >= 10 and self.pos["sl"] > self.pos["ep"]:
                    self.pos["sl"] = self.pos["ep"]
                    logger.info(f"Stop Loss locked to Break Even at {self.pos['ep']:.2f}")
                # Time Stop
                if bars_in_trade > TIME_STOP_MINS and cur_ticks > -4:
                    await self.close_position("BUY", price, "TIME STOP")
                    return

        # ----------------------------------------
        # Entry Logic
        # ----------------------------------------
        else: # No position
            # Only enter during bar idx limits (45 mins in, up to 30 mins before close)
            if bar_idx < 45 or bar_idx > 360:
                return
                
            vwap_target = round(vwap / TICK_SIZE) * TICK_SIZE
            z_abs = abs(z)
            
            # Target should be far enough, wait z is calculated by `stream_es.py` 
            # as Z=(last-vwap)/vwap_std
            if z_abs >= Z_THRESH:
                # Is price above or below VWAP?
                if z < 0: # Price is below VWAP, so we LONG back to VWAP
                    await self.open_position("LONG", price, vwap_target, z_abs, bar_idx)
                elif z > 0: # Price is above VWAP, so we SHORT back to VWAP
                    await self.open_position("SHORT", price, vwap_target, z_abs, bar_idx)


async def main():
    print("="*60)
    print("  TOPSTEP VWAP TARGET BOT - LIVE EXECUTION ENGINE")
    print(f"  DRY_RUN = {DRY_RUN}")
    print("="*60)

    username = os.environ.get("PROJECT_X_USERNAME", "")
    api_key = os.environ.get("PROJECT_X_API_KEY", "")
    if not username or not api_key:
        logger.error("Environment variables PROJECT_X_USERNAME and PROJECT_X_API_KEY must be set.")
        sys.exit(1)

    client = TopstepClient(username, api_key)
    await client.authenticate()
    await client.setup_account_and_contract()

    bot = VWAPBot(client)
    
    logger.info("Bot started. Polling stream_es.py...")
    # Loop every 0.5s to check stream_es API
    while True:
        await bot.tick()
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
