"""
ES Live Feed v7 — Historical Backfill + Live Stream
=====================================================
On startup:
  1. Pulls past 5 RTH days of 1-min bars from API
  2. Pulls today's bars up to now
  3. Streams live trades/quotes on top

  pip install httpx websockets pytz
  export PROJECT_X_USERNAME='email'
  export PROJECT_X_API_KEY='key'
  python stream_es.py
"""

import asyncio,os,json,threading,atexit,glob,random,time,uuid as _uuid
from datetime import datetime,timezone,timedelta,time as dtime
from http.server import HTTPServer,BaseHTTPRequestHandler
from collections import defaultdict
import httpx,websockets
import sys
from dotenv import load_dotenv
import math


# Load environment variables from .env file
load_dotenv()

try:
    import pytz; ET=pytz.timezone("America/New_York")
except ImportError:
    import sys
    print("  ⚠ WARNING: pytz not installed. Using fixed UTC-4 offset (EDT only).")
    print("    Install pytz for correct EST/EDT handling: pip install pytz")
    ET=timezone(timedelta(hours=-4))

# ============================================================
# BOT CONFIGURATION & CLASS
# ============================================================
BOT_ACTIVE = True
DRY_RUN = False
TARGET_MODE = "vwap"       # 'volume' uses vpoc, 'vwap' uses vwap
Z_THRESH = 1.2
CONTRACTS = 1
MAX_TRADES = 11
MAX_HOURLY_LOSS = 200      # Lockdown if we see this much pain in a rolling 60-min window
HOURLY_GUARD_TYPE = "DD"   # "PL" (absolute sum) or "DD" (peak-to-valley in hour)
STOP_RATIO = 0.4             # fallback if ATR unavailable
ATR_STOP_RATIO = 0.80          # stop = 0.80 × ATR — optimized 2026-04-16
MIN_STOP_TICKS = 4
MAX_STOP_TICKS = 12            # ES: reduced vs NQ (tick value 2.5× higher)
TRAIL_ACTIVATE = 8             # ticks of run-up before trailing activates — optimized 2026-04-16
TRAIL_DISTANCE = 4             # trail gap ticks — optimized (tight capture)
BREAKEVEN_TICKS = 6            # ticks of run-up before stop moves to breakeven — optimized 2026-04-16
EXIT_MIN_TICKS = 6
TIME_STOP_MINS = 90
TICK_SIZE = 0.25
TICK_VALUE = 12.50             # ES E-mini: $12.50/tick
GUTTER_GOAL = 1500
GUTTER_DD = 1900             # daily loss cap (gutter exit — separate from MLL)
TRAILING_MLL_DISTANCE = 2000   # MLL = peak EOD balance - this value (metric only, not an exit trigger)
ACCOUNT_START_BALANCE = 50000  # Starting account balance for MLL tracking
PROFIT_TARGET = 3000           # TopStepX combine profit target
STATS_LOOKBACK_DAYS = 0        # 0 = use all sessions
COMMISSION_RT = 2.80       # round-turn per contract (NFA + clearing)
SERVER_BRACKETS_PRIMARY = True   # use broker-side SL/TP as primary exits
ENTRY_REBASE_ENABLED = True      # rebase EP/SL/TP to broker avg fill price after entry fill

# --- SESSION & REGIME FILTERS ---
LUNCH_SKIP_START = 120         # tod_mins: skip 11:30–12:45 ET (thin tape)
LUNCH_SKIP_END   = 195
POWER_HOUR_START = 330         # tod_mins: stop entries at 3:00 PM ET
VWAP_SLOPE_THRESH = 0.3        # ES-tuned: skip entry if VWAP slope opposes direction — optimized 2026-04-16
MIN_VOL_REL = 0.8              # require ≥80% of avg volume at signal bar
MAX_SPREAD = 1.0               # max bid-ask spread to enter (1 pt = 4 ticks)
VX_CONTRACT = "CON.F.US.VX.M26"  # VIX futures on TopStepX — adjust month as needed

# --- VWAP CROSS REGIME ---
VWAP_CROSS_TRENDING  = 1      # ≤ this many crossings in 20 bars → trending, raise z-thresh
VWAP_CROSS_BALANCED  = 3      # ≥ this many crossings in 20 bars → mean-reverting, normal z-thresh

# --- PARTIAL SCALE-OUT ---
SCALE_OUT_ENABLED    = True   # exit 1 contract at VWAP touch, trail the other

# --- CUMULATIVE DELTA FILTER ---
CUM_DELTA_FILTER     = True   # skip entries strongly opposed by order flow
CUM_DELTA_FADE_MAX   = 0.12   # max |delta/volume| allowed when fading into a move

# --- SESSION-TYPE CLASSIFICATION ---
SESSION_TYPE_BARS    = 45     # classify session type after this many bars
SESSION_TREND_Z_MULT = 1.25   # z-thresh multiplier on trend days (replaces crossings heuristic, not stacked)

# --- DIRECTION COOLDOWN ---
DIR_COOLDOWN_BARS    = 8      # bars to block same-direction entries after a stop-out

# --- PREVIOUS SESSION LEVELS ---
PREV_LEVEL_TICKS     = 3      # ticks from prev VWAP/VPOC that counts as confluence
PREV_LEVEL_Z_BONUS   = 0.10   # z-thresh reduction when near a prev session level

# --- ECONOMIC CALENDAR (hardcoded 2026 high-impact dates) ---
HIGH_IMPACT_BEHAVIOR = "reduce"   # "skip" = no trades; "reduce" = raise z-thresh +0.5
# Single source of truth: maps date string → event name
HIGH_IMPACT_DATES = {
    # FOMC 2026 (decision day only)
    "2026-01-28": "FOMC", "2026-03-18": "FOMC", "2026-05-06": "FOMC", "2026-06-17": "FOMC",
    "2026-07-29": "FOMC", "2026-09-16": "FOMC", "2026-10-28": "FOMC", "2026-12-09": "FOMC",
    # CPI 2026 (BLS release dates, ~8:30 ET)
    "2026-01-14": "CPI", "2026-02-11": "CPI", "2026-03-11": "CPI", "2026-04-10": "CPI",
    "2026-05-13": "CPI", "2026-06-10": "CPI", "2026-07-14": "CPI", "2026-08-12": "CPI",
    "2026-09-11": "CPI", "2026-10-13": "CPI", "2026-11-12": "CPI", "2026-12-10": "CPI",
    # NFP 2026 (first Friday of each month)
    "2026-01-09": "NFP", "2026-02-06": "NFP", "2026-03-06": "NFP", "2026-04-03": "NFP",
    "2026-05-01": "NFP", "2026-06-05": "NFP", "2026-07-02": "NFP", "2026-08-07": "NFP",
    "2026-09-04": "NFP", "2026-10-02": "NFP", "2026-11-06": "NFP", "2026-12-04": "NFP",
}

VALID_EXIT_REASONS = {
    "STOP LOSS",
    "TRAIL STOP",
    "BREAKEVEN STOP",
    "TARGET",
    "TIME STOP",
    "END OF RTH",
    "GUTTER WIN",
    "GUTTER LOSS",
    "SERVER FILL",
    "END OF DATA",
    "UNKNOWN",
}

def normalize_exit_reason(reason):
    r = str(reason).strip().upper() if reason is not None else ""
    aliases = {
        "SL": "STOP LOSS",
        "TP": "TARGET",
        "STOP": "STOP LOSS",
        "SERVER": "SERVER FILL",
    }
    r = aliases.get(r, r)
    return r if r in VALID_EXIT_REASONS else "UNKNOWN"

def classify_stop_reason(direction, entry_price, stop_price):
    try:
        ep = float(entry_price)
        sp = float(stop_price)
    except (TypeError, ValueError):
        return "STOP LOSS"
    tol = TICK_SIZE * 0.25
    if abs(sp - ep) <= tol:
        return "BREAKEVEN STOP"
    if str(direction).upper() == "LONG":
        return "TRAIL STOP" if sp > ep else "STOP LOSS"
    if str(direction).upper() == "SHORT":
        return "TRAIL STOP" if sp < ep else "STOP LOSS"
    return "STOP LOSS"

API="https://api.topstepx.com/api"
HUB="https://rtc.topstepx.com/hubs/market"
CONTRACT="CON.F.US.EP.M26"    # ES E-mini S&P 500 June 2026
DATA_DIR="es_sessions"
DAYS_BACK=35
os.makedirs(DATA_DIR,exist_ok=True)

LOCK=threading.Lock()
BOT_STATE_LOCK=threading.Lock()  # FIX#1: protects global_bot state reads/writes across threads

def _parse_ts_to_et(ts_val):
    """Best-effort parse of API/session timestamps to ET-aware datetime."""
    if ts_val is None:
        return None
    # Handle numeric Unix timestamps (seconds or milliseconds)
    if isinstance(ts_val, (int, float)):
        epoch = float(ts_val)
        if epoch > 1e10:  # milliseconds
            epoch /= 1000.0
        return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ET)
    s = str(ts_val).strip()
    if not s:
        return None
    # Handle numeric string (e.g. "1700000000" or "1700000000000")
    if s.lstrip("-").isdigit():
        epoch = float(s)
        if epoch > 1e10:
            epoch /= 1000.0
        return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ET)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(ET)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            d = datetime.strptime(s, fmt).replace(tzinfo=ET)
            return d
        except (ValueError, TypeError):
            continue
    return None

def is_rth_bar_ts(ts_val):
    """True when timestamp falls within weekday RTH window (9:30-16:00 ET)."""
    d = _parse_ts_to_et(ts_val)
    if not d:
        return False
    t = d.time()
    return d.weekday() < 5 and dtime(9,30) <= t < dtime(16,0)

class Session:
    def __init__(self,date_str=None):
        self.date=date_str or datetime.now(ET).strftime("%Y-%m-%d")
        self.bars=[];self.cur_bar=None
        self.vol_profile=defaultdict(float)  # BUG8: float for fractional vol distribution
        self.tape=[]
        self.cum_pv=0.0;self.cum_v=0;self.cum_p2v=0.0;self.vwap=0.0;self.vwap_std=0.0
        self.open=0.0;self.high=0.0;self.low=999999.0
        self.trade_count=0;self.total_volume=0;self.tick_count=0
        self.bid=0.0;self.ask=0.0;self.last=0.0;self.quotes=[]
        self.vpoc_history=[]  # track VPOC at each bar for stability filter
        self.std_history=[]   # track std at each bar for sigma panic filter
        self.bar_ranges=[]    # track true ranges for ATR slope
        self.atr_history=[]   # developing ATR at each bar
        self.vwap_history=[]  # VWAP at each bar close (for VWAP crossing count)
        self._prev_close=None # for true range calculation
        self.bar_just_closed=False  # FIX: bar-close gate for live entries (matches backtester sampling)
        self.cum_delta=0.0    # running (buy_vol - sell_vol) since RTH open — order flow imbalance

    def add_bar(self,o,h,l,c,v,ts_str):
        """Add a completed historical bar."""
        self.bars.append({"ts":ts_str,"o":o,"h":h,"l":l,"c":c,"v":v})
        # Update session stats
        if self.open==0:self.open=o
        if h>self.high:self.high=h
        if l<self.low:self.low=l
        self.last=c;self.total_volume+=v  # BUG7: don't add volume to trade_count
        # Volume profile from bar (distribute evenly across OHLC range)
        lo_lv=round(l*4)/4;hi_lv=round(h*4)/4
        n_levels=max(1,int((hi_lv-lo_lv)/0.25)+1)
        vpl=v/n_levels  # FIX: float division, not integer — prevents volume loss
        lv=lo_lv
        while lv<=hi_lv:
            self.vol_profile[lv]+=vpl
            lv+=0.25
        # VWAP from bar (use typical price * volume)
        tp=(h+l+c)/3
        self.cum_pv+=tp*v;self.cum_v+=v;self.cum_p2v+=(tp**2)*v
        if self.cum_v>0:
            self.vwap=self.cum_pv/self.cum_v
            var=(self.cum_p2v/self.cum_v)-(self.vwap**2)
            self.vwap_std=var**0.5 if var>0 else 0
        # Track VPOC history for stability filter
        self.vpoc_history.append(self.vpoc)
        self.std_history.append(self.vwap_std)
        self.vwap_history.append(self.vwap)
        # Track ATR for slope filter
        tr = max(h-l, abs(h-self._prev_close) if self._prev_close else h-l, abs(l-self._prev_close) if self._prev_close else h-l)
        self.bar_ranges.append(tr)
        self._prev_close = c
        lookback = self.bar_ranges[-20:] if len(self.bar_ranges) >= 20 else self.bar_ranges
        self.atr_history.append(sum(lookback) / len(lookback) if lookback else 0)

    def add_trade(self,price,vol,side_str,now_utc):
        self.trade_count+=1;self.total_volume+=vol;self.last=price
        if side_str=="BUY": self.cum_delta+=vol
        elif side_str=="SELL": self.cum_delta-=vol
        if self.open==0:self.open=price
        if price>self.high:self.high=price
        if price<self.low:self.low=price
        self.vol_profile[price]+=vol
        self.cum_pv+=price*vol;self.cum_v+=vol;self.cum_p2v+=(price**2)*vol
        if self.cum_v>0:
            self.vwap=self.cum_pv/self.cum_v
            var=(self.cum_p2v/self.cum_v)-(self.vwap**2)
            self.vwap_std=var**0.5 if var>0 else 0
        bar_key=now_utc.replace(second=0,microsecond=0)
        bk_str=str(bar_key)
        if self.cur_bar is None or self.cur_bar["ts"]!=bk_str:
            if self.cur_bar:
                self.bars.append(self.cur_bar)
                self.bar_just_closed=True  # FIX: signal entry-eligible moment for live (matches backtester bar-close sampling)
                self.vpoc_history.append(self.vpoc)
                self.std_history.append(self.vwap_std)
                self.vwap_history.append(self.vwap)
                # ATR tracking on bar completion
                cb = self.cur_bar
                tr = max(cb["h"]-cb["l"], abs(cb["h"]-self._prev_close) if self._prev_close else cb["h"]-cb["l"], abs(cb["l"]-self._prev_close) if self._prev_close else cb["h"]-cb["l"])
                self.bar_ranges.append(tr)
                self._prev_close = cb["c"]
                lb = self.bar_ranges[-20:] if len(self.bar_ranges) >= 20 else self.bar_ranges
                self.atr_history.append(sum(lb)/len(lb) if lb else 0)
            self.cur_bar={"ts":bk_str,"o":price,"h":price,"l":price,"c":price,"v":vol}
        else:
            b=self.cur_bar
            if price>b["h"]:b["h"]=price
            if price<b["l"]:b["l"]=price
            b["c"]=price;b["v"]+=vol
        self.tape.append({"time":now_utc.strftime("%H:%M:%S.%f")[:12],"price":price,"vol":vol,"side":side_str})
        if len(self.tape)>500:del self.tape[:-500]

    def vpoc_migration_speed(self, lookback=20):
        """How fast is VPOC moving? Points per bar. High = trending session."""
        if len(self.vpoc_history)<lookback: return 0
        recent=self.vpoc_history[-lookback:]
        valid=[p for p in recent if p and p>0]
        if len(valid)<2: return 0
        return abs(valid[-1]-valid[0])/len(valid)

    def is_session_stable(self, max_migration=0.15, lookback=20):
        """Returns True if VPOC is stable (balanced session). False = trending."""
        return self.vpoc_migration_speed(lookback) <= max_migration

    def get_atr_slope(self, lookback=10):
        """Rate of change of developing ATR. >0.10 = range expanding = trend developing."""
        if len(self.atr_history) < lookback: return 0
        atr_now = self.atr_history[-1]
        atr_past = self.atr_history[-lookback]
        if atr_past <= 0: return 0
        return (atr_now - atr_past) / atr_past

    def add_quote(self,bid_v,ask_v,now_utc):
        self.tick_count+=1
        if bid_v and bid_v>0:self.bid=bid_v
        if ask_v and ask_v>0:self.ask=ask_v
        if self.bid>0 and self.ask>0:self.last=(self.bid+self.ask)/2
        sp=self.ask-self.bid if self.ask>0 and self.bid>0 else 0
        self.quotes.append({"time":now_utc.strftime("%H:%M:%S.%f")[:12],"bid":self.bid,"ask":self.ask,"spread":round(sp,2)})
        if len(self.quotes)>50:del self.quotes[:-50]

    @property
    def vpoc(self):
        """Volume POC — price level with most volume."""
        return max(self.vol_profile,key=self.vol_profile.get) if self.vol_profile else 0

    def to_dict(self):
        vpoc=self.vpoc
        top_vol=sorted(self.vol_profile.items(),key=lambda x:-x[1])[:30]
        # Calculate historical indicators
        prices = []; cum_pv = 0; cum_v = 0; cum_p2v = 0; vp_dict = defaultdict(float)
        for i, b in enumerate(self.bars):
            # HMA removed
            tp = (b["h"] + b["l"] + b["c"]) / 3
            cum_pv += tp * b["v"]; cum_v += b["v"]; cum_p2v += (tp**2) * b["v"]
            b["vwap"] = round(cum_pv / cum_v, 2) if cum_v > 0 else b["c"]
            if cum_v > 0:
                var = (cum_p2v / cum_v) - ((cum_pv / cum_v) ** 2)
                b["vwap_std"] = round(var**0.5 if var > 0 else 0, 4)
            else:
                b["vwap_std"] = 0
            b["z"] = round((b["c"] - b["vwap"]) / b["vwap_std"], 2) if b["vwap_std"] > 0.01 else 0
            lo_lv = round(b["l"]*4)/4; hi_lv = round(b["h"]*4)/4
            n_levels = max(1, int((hi_lv - lo_lv)/0.25) + 1)
            vpl = b["v"] / n_levels
            lv = lo_lv
            while lv <= hi_lv: vp_dict[lv] += vpl; lv += 0.25
            b["vpoc"] = max(vp_dict, key=vp_dict.get) if vp_dict else b["c"]
            
            start_idx = max(0, i - 60 + 1)
            vp60 = defaultdict(float)
            for wb in self.bars[start_idx:i+1]:
                w_lo = round(wb["l"]*4)/4; w_hi = round(wb["h"]*4)/4
                w_n = max(1, int((w_hi - w_lo)/0.25) + 1)
                w_vpl = wb["v"] / w_n
                w_lv = w_lo
                while w_lv <= w_hi: vp60[w_lv] += w_vpl; w_lv += 0.25
            b["vpoc60"] = max(vp60, key=vp60.get) if vp60 else b["c"]

        ab=[{"ts":b.get("ts"),"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"],"v":b["v"],"vwap":b.get("vwap"),"vpoc":b.get("vpoc"),"vpoc60":b.get("vpoc60"),"vwap_std":b.get("vwap_std"),"z":b.get("z")} for b in self.bars[-500:]]
        cb={"ts":self.cur_bar.get("ts"),"o":self.cur_bar["o"],"h":self.cur_bar["h"],"l":self.cur_bar["l"],"c":self.cur_bar["c"],"v":self.cur_bar["v"]} if self.cur_bar else None
        if cb:
            tp = (cb["h"] + cb["l"] + cb["c"]) / 3
            n_cum_pv = cum_pv + tp * cb["v"]; n_cum_v = cum_v + cb["v"]
            n_cum_p2v = cum_p2v + (tp**2) * cb["v"]
            cb["vwap"] = round(n_cum_pv / n_cum_v, 2) if n_cum_v > 0 else cb["c"]
            if n_cum_v > 0:
                var = (n_cum_p2v / n_cum_v) - ((n_cum_pv / n_cum_v) ** 2)
                cb["vwap_std"] = round(var**0.5 if var > 0 else 0, 4)
            else:
                cb["vwap_std"] = 0
            cb["z"] = round((cb["c"] - cb["vwap"]) / cb["vwap_std"], 2) if cb["vwap_std"] > 0.01 else 0
            lo_lv = round(cb["l"]*4)/4; hi_lv = round(cb["h"]*4)/4
            n_levels = max(1, int((hi_lv - lo_lv)/0.25) + 1)
            vpl = cb["v"] / n_levels
            lv = lo_lv
            vp_dict_cb = vp_dict.copy()
            while lv <= hi_lv: vp_dict_cb[lv] += vpl; lv += 0.25
            cb["vpoc"] = max(vp_dict_cb, key=vp_dict_cb.get) if vp_dict_cb else cb["c"]
            
            start_idx = max(0, len(self.bars) - 60 + 1)
            vp60 = defaultdict(float)
            for wb in self.bars[start_idx:] + [cb]:
                w_lo = round(wb["l"]*4)/4; w_hi = round(wb["h"]*4)/4
                w_n = max(1, int((w_hi - w_lo)/0.25) + 1)
                w_vpl = wb["v"] / w_n
                w_lv = w_lo
                while w_lv <= w_hi: vp60[w_lv] += w_vpl; w_lv += 0.25
            cb["vpoc60"] = max(vp60, key=vp60.get) if vp60 else cb["c"]

        z=round((self.last-self.vwap)/self.vwap_std,2) if self.vwap_std>0.01 else 0
        return {"date":self.date,"bid":self.bid,"ask":self.ask,"last":self.last,
            "open":self.open,"high":self.high,"low":self.low if self.low<999999 else 0,
            "volume":self.total_volume,"trade_count":self.trade_count,"tick_count":self.tick_count,
            "vwap":round(self.vwap,2),"vwap_std":round(self.vwap_std,2),"z_score":z,
            "vpoc":vpoc,"top_vol":[{"price":p,"vol":v} for p,v in top_vol],
            "profile":[{"price":p,"vol":v} for p,v in self.vol_profile.items()],
            "bars":ab,"current_bar":cb,"tape":self.tape[-200:],"quotes":self.quotes[-20:]}

    def save(self):
        path=os.path.join(DATA_DIR,f"{self.date}.json")
        d={"date":self.date,
           "bars":[{"ts":b["ts"],"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"],"v":b["v"]} for b in self.bars],
           "cur_bar":{"ts":self.cur_bar["ts"],"o":self.cur_bar["o"],"h":self.cur_bar["h"],"l":self.cur_bar["l"],"c":self.cur_bar["c"],"v":self.cur_bar["v"]} if self.cur_bar else None,
           "vol_profile":{str(k):v for k,v in self.vol_profile.items()},
           "tape":self.tape[-200:],
           "cum_pv":self.cum_pv,"cum_v":self.cum_v,"cum_p2v":self.cum_p2v,
           "cum_delta":self.cum_delta,
           "vwap":self.vwap,"vwap_std":self.vwap_std,
           "open":self.open,"high":self.high,"low":self.low,
           "trade_count":self.trade_count,"total_volume":self.total_volume,
           "tick_count":self.tick_count,"bid":self.bid,"ask":self.ask,"last":self.last}
        with open(path,"w") as f:json.dump(d,f)

    @staticmethod
    def load(date_str):
        path=os.path.join(DATA_DIR,f"{date_str}.json")
        if not os.path.exists(path):return None
        s=Session(date_str)
        with open(path) as f:d=json.load(f)
        for b in d.get("bars",[]):
            if is_rth_bar_ts(b.get("ts")):
                s.bars.append({"ts":b["ts"],"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"],"v":b["v"]})
        cb=d.get("cur_bar")
        s.cur_bar={"ts":cb["ts"],"o":cb["o"],"h":cb["h"],"l":cb["l"],"c":cb["c"],"v":cb["v"]} if cb and is_rth_bar_ts(cb.get("ts")) else None
        s.vol_profile=defaultdict(float)  # BUG8: float
        for k,v in d.get("vol_profile",{}).items():s.vol_profile[float(k)]=v
        s.tape=d.get("tape",[]);s.cum_pv=d.get("cum_pv",0);s.cum_v=d.get("cum_v",0);s.cum_p2v=d.get("cum_p2v",0);s.cum_delta=d.get("cum_delta",0.0)
        s.vwap=d.get("vwap",0);s.vwap_std=d.get("vwap_std",0)
        s.open=d.get("open",0);s.high=d.get("high",0);s.low=d.get("low",999999)
        s.trade_count=d.get("trade_count",0);s.total_volume=d.get("total_volume",0)
        s.tick_count=d.get("tick_count",0);s.bid=d.get("bid",0);s.ask=d.get("ask",0);s.last=d.get("last",0)
        # BUG3: rebuild vpoc_history and std_history from bars
        vp_rebuild=defaultdict(float)
        r_cum_pv=0.0; r_cum_v=0; r_cum_p2v=0.0
        for b in s.bars:
            lo_lv=round(b["l"]*4)/4;hi_lv=round(b["h"]*4)/4
            n_levels=max(1,int((hi_lv-lo_lv)/0.25)+1);vpl=b["v"]/n_levels;lv=lo_lv
            while lv<=hi_lv:vp_rebuild[lv]+=vpl;lv+=0.25
            s.vpoc_history.append(max(vp_rebuild,key=vp_rebuild.get) if vp_rebuild else 0)
            # Rebuild std history
            tp=(b["h"]+b["l"]+b["c"])/3
            r_cum_pv+=tp*b["v"]; r_cum_v+=b["v"]; r_cum_p2v+=(tp**2)*b["v"]
            if r_cum_v>0:
                r_vwap=r_cum_pv/r_cum_v
                r_var=(r_cum_p2v/r_cum_v)-(r_vwap**2)
                s.std_history.append(r_var**0.5 if r_var>0 else 0)
                s.vwap_history.append(r_vwap)
            else:
                s.std_history.append(0)
                s.vwap_history.append(0)
            # Rebuild ATR history
            tr = max(b["h"]-b["l"], abs(b["h"]-s._prev_close) if s._prev_close else b["h"]-b["l"], abs(b["l"]-s._prev_close) if s._prev_close else b["h"]-b["l"])
            s.bar_ranges.append(tr)
            s._prev_close = b["c"]
            lb = s.bar_ranges[-20:] if len(s.bar_ranges) >= 20 else s.bar_ranges
            s.atr_history.append(sum(lb)/len(lb) if lb else 0)
        return s

# ============================================================
# TICK RECORDER — saves every tick for accurate backtesting
# ============================================================
TICK_DIR = "es_ticks"
os.makedirs(TICK_DIR, exist_ok=True)

class TickRecorder:
    """
    Records every trade and quote tick to CSV files organized by date.
    Format: timestamp, type, price, volume, side, bid, ask
    
    Usage for backtesting: load the CSV, rebuild 1-min bars from actual
    tick data instead of using SPY proxy or API bars.
    """
    def __init__(self):
        self._file = None
        self._date = None
        self._count = 0

    def _ensure_file(self, date_str):
        if self._date != date_str:
            if self._file:
                self._file.close()
            path = os.path.join(TICK_DIR, f"{date_str}.csv")
            is_new = not os.path.exists(path)
            self._file = open(path, "a", buffering=1)  # line-buffered
            if is_new:
                self._file.write("ts,type,price,volume,side,bid,ask\n")
            self._date = date_str
            self._count = 0

    def record_trade(self, price, volume, side, now_utc):
        now_et = now_utc.astimezone(ET) if now_utc.tzinfo else datetime.now(ET)
        date_str = now_et.strftime("%Y-%m-%d")
        ts = now_et.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
        self._ensure_file(date_str)
        self._file.write(f"{ts},T,{price},{volume},{side},,\n")
        self._count += 1

    def record_quote(self, bid, ask, now_utc):
        """Record quote — throttled to only when bid/ask changes."""
        if not bid or not ask: return
        # Only record when bid or ask actually changes
        if hasattr(self, '_last_bid') and bid == self._last_bid and ask == self._last_ask:
            return
        self._last_bid = bid; self._last_ask = ask
        now_et = now_utc.astimezone(ET) if now_utc.tzinfo else datetime.now(ET)
        date_str = now_et.strftime("%Y-%m-%d")
        ts = now_et.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
        self._ensure_file(date_str)
        self._file.write(f"{ts},Q,,,,{bid},{ask}\n")
        self._count += 1

    def flush(self):
        if self._file:
            self._file.flush()

    def close(self):
        if self._file:
            self._file.close()
            self._file = None

tick_recorder = TickRecorder()
atexit.register(tick_recorder.close)

# Globals
current_session=None
connected=False;errors=[];raw_log=[]
live_bid=0.0;live_ask=0.0;live_last=0.0
_rth_session_ready=False  # True once we've reset the session for today's RTH

def get_or_create_session():
    global current_session, _rth_session_ready
    today=datetime.now(ET).strftime("%Y-%m-%d")
    if current_session is None or current_session.date!=today:
        if current_session:current_session.save()
        _rth_session_ready=False  # new day, need fresh RTH reset
        loaded=Session.load(today)
        current_session=loaded if loaded else Session(today)
    # At RTH open, ensure a clean session with no pre-market volume spillover
    if not _rth_session_ready and is_rth():
        _rth_session_ready=True
        current_session=Session(today)  # fresh: empty bars, vol_profile, VWAP accumulators
        # Update trailing MLL peak at start of new RTH day (using yesterday's EOD balance)
        with BOT_STATE_LOCK:  # FIX#1: protect bot state during daily reset
            eod_balance = global_bot.account_balance  # realized balance at EOD
            if eod_balance > global_bot.peak_eod_balance:
                old_peak = global_bot.peak_eod_balance
                global_bot.peak_eod_balance = eod_balance
                print(f"  [MLL] Peak EOD balance updated: ${old_peak:.2f} -> ${eod_balance:.2f} | New floor: ${global_bot.mll_floor:.2f}")
            # Reset bot daily counters
            global_bot.trades_today=0
            global_bot.daily_pnl=0
            if global_bot.pos:
                # FIX: Don't just null pos — that orphans the server position.
                # Set flag so the async check_logic sends a proper close order.
                print(f"\n  [WARNING] Open position carried into new day — flagging for emergency close")
                global_bot._emergency_close = True
                # Also null pos so new-day logic doesn't count it, but orphan detector will catch the server side
                global_bot.pos=None
        print(f"\n  [SESSION] Fresh RTH session started for {today} | Balance: ${global_bot.account_balance:.2f} | MLL Floor: ${global_bot.mll_floor:.2f}")
        # Reset session-type classification for the new day
        REGIME["vwap_crossings"] = 0   # reset crossings for new session
        REGIME["session_type"] = "NEUTRAL"
        REGIME["session_type_bar"] = -1
        check_calendar_event()
        # Load previous session levels for confluence detection
        load_prev_session_levels()
        # VIX fetch requires async context — schedule via event loop if available
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running() and global_bot.token:
                asyncio.ensure_future(fetch_vix_proxy(global_bot.token))
        except Exception:
            pass
    return current_session

def save_current():
    if current_session:current_session.save()

def list_sessions():
    return sorted([os.path.basename(f).replace(".json","") for f in glob.glob(os.path.join(DATA_DIR,"*.json"))],reverse=True)

def saver():
    import time
    while True:
        time.sleep(30)
        try:
            save_current()
            tick_recorder.flush()
        except Exception as e:  # FIX#9: never bare except
            print(f"  [SAVER ERROR] {e}")
threading.Thread(target=saver,daemon=True).start()
atexit.register(save_current)

# FIX#7: Background stats computation (runs every 60s, not on every HTTP poll)
_bg_stats_cache = {"data": None}

def _bg_stats_worker():
    import time as _time
    while True:
        _time.sleep(60)
        try:
            gw = getattr(global_bot, "gutter_win", True)
            gl = getattr(global_bot, "gutter_loss", True)
            combined = get_combined_exit_trades(gutter_win=gw, gutter_loss=gl, min_refresh_sec=60)
            stats = build_bot_stats(combined, max_trades=MAX_TRADES)
            _bg_stats_cache["data"] = stats
        except Exception as e:
            print(f"  [BG STATS ERROR] {e}")
threading.Thread(target=_bg_stats_worker, daemon=True).start()

# ============================================================
# HISTORICAL BACKFILL
# ============================================================
async def backfill(token):
    """Pull past N days of RTH 1-min bars + today's bars."""
    headers={"Authorization":f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60) as http:
        now_et=datetime.now(ET)

        # Figure out RTH trading days (skip weekends)
        dates=[]
        d=now_et.date()
        while len(dates)<DAYS_BACK+1:  # +1 for today
            if d.weekday()<5:  # Mon-Fri
                dates.append(d)
            d-=timedelta(days=1)
        dates.reverse()  # oldest first

        for day in dates:
            date_str=day.strftime("%Y-%m-%d")
            is_today=(day==now_et.date())

            # Check if we already have this day saved with complete RTH bars
            existing=Session.load(date_str)
            if existing and len(existing.bars)>100 and not is_today:
                # Verify the session starts near 9:30 ET (not a partial day from late bot start)
                first_ts = existing.bars[0].get("ts") if existing.bars else None
                first_et = _parse_ts_to_et(first_ts) if first_ts else None
                starts_near_open = first_et and first_et.hour == 9 and first_et.minute < 35 if first_et else False
                if starts_near_open:
                    print(f"  {date_str}: {len(existing.bars)} bars (cached)")
                    continue
                else:
                    start_t = first_et.strftime("%H:%M") if first_et else "?"
                    print(f"  {date_str}: {len(existing.bars)} bars but starts at {start_t} ET — re-fetching full RTH...")

            # RTH: 9:30 AM - 4:00 PM ET
            rth_start=ET.localize(datetime.combine(day,dtime(9,30)))
            rth_end=ET.localize(datetime.combine(day,dtime(16,0)))

            start_utc=rth_start.astimezone(timezone.utc)
            if is_today:
                end_utc=datetime.now(timezone.utc)
            else:
                end_utc=rth_end.astimezone(timezone.utc)

            if start_utc>=end_utc:
                print(f"  {date_str}: skipped (future or no data yet)")
                continue

            print(f"  {date_str}: fetching RTH bars...",end="",flush=True)

            payload={
                "contractId":CONTRACT,"live":False,
                "startTime":start_utc.isoformat(),
                "endTime":end_utc.isoformat(),
                "unit":2,"unitNumber":1,"limit":500,
                "includePartialBar":is_today,
            }

            try:
                r=await http.post(f"{API}/History/retrieveBars",json=payload,headers=headers)
                data=r.json()
                api_bars=[b for b in (data.get("bars") or []) if is_rth_bar_ts(b.get("t"))]

                if not api_bars:
                    print(f" 0 bars")
                    continue

                # API returns newest first — reverse to chronological
                api_bars.reverse()

                sess=Session(date_str)
                for b in api_bars:
                    sess.add_bar(b["o"],b["h"],b["l"],b["c"],b["v"],b["t"])

                sess.save()
                print(f" {len(api_bars)} bars, VWAP={sess.vwap:.2f}")

                # If today, set as current session
                if is_today:
                    with LOCK:
                        global current_session
                        current_session=sess

            except Exception as e:
                print(f" error: {e}")

            # Rate limit courtesy
            await asyncio.sleep(1)

    print()

def _es_contracts_for_date(d):
    """Return ordered list of ES contract IDs to try for date d.

    Always tries the current live contract (CONTRACT / ESM6) first — the API
    may serve all historical data through it. Falls back to the historically
    correct quarterly contract if the primary returns no bars.
    """
    yr2 = str(d.year)[2:]
    m = d.month

    # Historically correct contract for this date (quarterly front-month)
    if m <= 3:
        prev_yr2 = str(d.year - 1)[2:]
        historical = [f"CON.F.US.EP.H{yr2}", f"CON.F.US.EP.Z{prev_yr2}"]
    elif m <= 6:
        historical = [f"CON.F.US.EP.M{yr2}", f"CON.F.US.EP.H{yr2}"]
    elif m <= 9:
        historical = [f"CON.F.US.EP.U{yr2}", f"CON.F.US.EP.M{yr2}"]
    else:
        historical = [f"CON.F.US.EP.Z{yr2}", f"CON.F.US.EP.U{yr2}"]

    # Always try the current live contract first, then historical, deduped
    result = [CONTRACT]
    for c in historical:
        if c not in result:
            result.append(c)
    return result


async def fetch_training_history(token):
    """Legacy compatibility no-op."""
    return

# ============================================================
# REGIME STATE (DETERMINISTIC)
# ============================================================
REGIME = {
    "vix_proxy": None,
    "vwap_crossings": 0,           # price×VWAP crossings in last 20 bars (0 = trending, 3+ = balanced)
    "calendar_event": None,        # name of today's high-impact event, or None
    # Previous session reference levels
    "prev_vwap": None,
    "prev_vpoc": None,
    "prev_close": None,
    # Session-type classification (set at SESSION_TYPE_BARS)
    "session_type": "NEUTRAL",     # TRENDING / RANGING / NEUTRAL
    "session_type_bar": -1,        # bar_idx when type was last classified (prevents re-running)
}

def count_vwap_crossings(vwap_history, price_history, lookback=20, update_regime=True):
    """Count how many times price crossed VWAP in the last N bars.
    0-1 crossings = trending (raise z-threshold).
    3+ crossings = balanced/mean-reverting (normal z-threshold).
    Only updates REGIME['vwap_crossings'] when update_regime=True (live path).
    Backtest callers must pass update_regime=False to avoid corrupting live state."""
    if len(vwap_history) < 2 or len(price_history) < 2:
        return 0
    vh = vwap_history[-lookback:]
    ph = price_history[-lookback:]
    n = min(len(vh), len(ph))
    crossings = 0
    for j in range(1, n):
        prev_above = ph[j-1] > vh[j-1]
        curr_above = ph[j] > vh[j]
        if prev_above != curr_above:
            crossings += 1
    if update_regime:
        REGIME["vwap_crossings"] = crossings
    return crossings

def check_calendar_event():
    """Check if today is a high-impact economic calendar date.
    Updates REGIME['calendar_event'] — None or event name string (FOMC/CPI/NFP)."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    name = HIGH_IMPACT_DATES.get(today)  # dict lookup → event name or None
    REGIME["calendar_event"] = name
    if name:
        print(f"  [CALENDAR] High-impact event today: {name} — behavior: {HIGH_IMPACT_BEHAVIOR}")

# ============================================================
# PREVIOUS SESSION LEVELS
# ============================================================
def _get_prev_trading_date(date_str):
    """Return the most recent saved session date before date_str, or None."""
    all_sessions = sorted(list_sessions())  # ascending
    candidates = [s for s in all_sessions if s < date_str]
    return candidates[-1] if candidates else None

def load_prev_session_levels():
    """Load prior day's VWAP, VPOC, close into REGIME for confluence."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    prev = _get_prev_trading_date(today)
    if not prev:
        return
    try:
        ps = Session.load(prev)
        if ps and ps.bars:
            REGIME["prev_vwap"] = ps.vwap
            REGIME["prev_vpoc"] = ps.vpoc
            REGIME["prev_close"] = ps.bars[-1]["c"]
            print(f"  [PREV] {prev}: VWAP={ps.vwap:.2f} VPOC={ps.vpoc:.2f} Close={ps.bars[-1]['c']:.2f}")
    except Exception as e:
        print(f"  [PREV] Load failed: {e}")

# ============================================================
# SESSION-TYPE CLASSIFICATION (HEURISTIC ONLY)
# ============================================================

def _session_type_heuristic(bars, atr):
    """Heuristic fallback: classify session as TRENDING if range or VPOC migration is large."""
    if not bars or atr <= 0:
        return "NEUTRAL"
    total_range = max(b["h"] for b in bars) - min(b["l"] for b in bars)
    if total_range > 2.5 * atr:
        return "TRENDING"
    vpocs = [b.get("vpoc", bars[0]["c"]) for b in bars]
    vpoc_travel = abs(vpocs[-1] - vpocs[0]) if len(vpocs) >= 2 else 0
    if vpoc_travel > 2.0:
        return "TRENDING"
    if total_range < atr:
        return "RANGING"
    return "NEUTRAL"

def maybe_classify_session(sess, bar_idx):
    """Classify session type using deterministic heuristics."""
    if bar_idx < SESSION_TYPE_BARS:
        return
    if REGIME.get("session_type_bar") >= SESSION_TYPE_BARS:
        return  # already classified this exact bar
    bars = sess.bars[:SESSION_TYPE_BARS]
    if len(bars) < SESSION_TYPE_BARS:
        return
    idx = min(SESSION_TYPE_BARS - 1, len(sess.atr_history) - 1)
    atr = sess.atr_history[idx] if sess.atr_history else 0
    session_type = _session_type_heuristic(bars, atr) if atr > 0 else "NEUTRAL"
    REGIME["session_type"] = session_type
    REGIME["session_type_bar"] = bar_idx
    print(f"  [SESSION TYPE] bar={bar_idx}: {session_type}")

async def fetch_vix_proxy(token):
    """Fetch VIX proxy from VX futures on TopStepX. Falls back silently if unavailable."""
    try:
        now = datetime.now(ET)
        start = (now - timedelta(days=7)).astimezone(timezone.utc)
        payload = {
            "contractId": VX_CONTRACT, "live": False,
            "startTime": start.isoformat(),
            "endTime": now.astimezone(timezone.utc).isoformat(),
            "unit": 2, "unitNumber": 1440, "limit": 5,  # daily bars
        }
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.post(f"{API}/History/retrieveBars", json=payload,
                             headers={"Authorization": f"Bearer {token}"})
            bars = r.json().get("bars") or []
            if bars:
                vix_level = float(bars[-1]["c"])
                REGIME["vix_proxy"] = vix_level
                regime = "EXTREME" if vix_level > 30 else ("HIGH" if vix_level > 22 else "NORMAL")
                print(f"  [VIX] VX proxy: {vix_level:.2f} ({regime})")
                return vix_level
    except Exception as e:
        print(f"  [VIX] VX fetch skipped: {e}")
    return None

BOT_LOCK = asyncio.Lock()  # BUG1: async lock for bot state

class VWAPBot:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10.0)
        self.token = None; self.headers = {}
        self.token_ts = None  # BUG6: track when token was obtained
        self.account_id = None; self.account_name = None; self.contract_id = None
        self.pos = None; self.daily_pnl = 0.0; self.total_pnl = 0.0
        self.trade_history = []; self.pnl_history = []  
        self.equity_history = [] # list of (timestamp, equity)
        self.trades_today = 0
        self.ready = False  # FIX: init before setup
        self.peak_pnl = 0.0
        self.max_dd = 0.0
        self._open_pnl = 0  # cached for HTTP handler
        self.gutter_win = True
        self.gutter_loss = True
        self.last_exit_bar = -999
        self.lockdown_until = None
        self.cached_orders = []  # active orders cached for dashboard
        self.entry_pending = False  # Flag to prevent duplicate entries while order is being placed
        self._emergency_close = False  # FIX: flagged by RTH reset when pos is force-nulled
        self._orphan_close_attempts = 0  # FIX: orphan auto-close confirmation counter
        self._orphan_alert_ts = 0
        # Direction cooldown — bars to block same-direction entries after a stop-out
        self._cooldown_long_until = -999   # bar_idx after which LONG entries re-allowed
        self._cooldown_short_until = -999  # bar_idx after which SHORT entries re-allowed
        # Trailing MLL (Maximum Loss Limit) — mirrors TopStepX rule
        # Floor = peak_eod_balance - TRAILING_MLL_DISTANCE
        # Peak updates at EOD (end of RTH), floor only moves UP, never down
        self.peak_eod_balance = ACCOUNT_START_BALANCE  # highest end-of-day account balance

    @property
    def account_balance(self):
        """Current live account balance = starting balance + total realized P/L."""
        return ACCOUNT_START_BALANCE + self.total_pnl

    @property
    def account_balance_with_open(self):
        """Current account balance including unrealized P/L."""
        return ACCOUNT_START_BALANCE + self.total_pnl + self._open_pnl

    @property
    def mll_floor(self):
        """Trailing MLL floor — account balance must stay above this."""
        return self.peak_eod_balance - TRAILING_MLL_DISTANCE

    @property
    def mll_remaining(self):
        """How much room left before hitting MLL floor (using live balance)."""
        return self.account_balance_with_open - self.mll_floor

    async def _get_open_position_snapshot(self):
        """Return open position snapshot for current contract from server, if any.
        Returns:
            dict: position snapshot if server has an open position for this contract
            {} (empty dict): server CONFIRMED no open positions (confirmed flat)
            None: API error — could NOT determine position state (callers MUST NOT assume flat)
        """
        try:
            r = await self.client.post(f"{API}/Position/searchOpen", json={"accountId": self.account_id}, headers=self.headers)
            data = r.json()
            positions = data.get("positions", [])
            if not positions:
                return {}  # FIX: confirmed flat (empty dict, not None)
            same_contract = [p for p in positions if p.get("contractId") == self.contract_id]
            if same_contract:
                return same_contract[0]
            # Server has positions but not for this contract = flat for our purposes
            return {}  # FIX: confirmed flat for this contract
        except Exception as e:
            print(f"  [POSITION API ERROR] {e}")
            return None  # API error — state UNKNOWN

    def _extract_open_size(self, pos_snapshot):
        if pos_snapshot is None:
            return -1  # FIX: unknown (API failed) — callers must NOT treat as 0
        if not pos_snapshot:
            return 0  # confirmed flat (empty dict)
        for k in ("size", "qty", "quantity"):
            v = pos_snapshot.get(k)
            if isinstance(v, (int, float)):
                return abs(int(v))
        # FIX: We got a position dict back but couldn't find a size field.
        # This means the API has an open position with an unknown schema.
        # Returning 0 here would falsely trigger external-close → ghost position.
        print(f"  [WARN] Position snapshot has no recognized size field: {list(pos_snapshot.keys())}")
        return -1  # unknown — do NOT assume flat

    def _extract_avg_price(self, pos_snapshot):
        if not pos_snapshot:
            return None
        for k in ("averagePrice", "avgPrice", "avgFillPrice", "entryPrice", "openPrice"):
            v = pos_snapshot.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        return None

    def _has_working_brackets_for_pos(self):
        if not self.pos:
            return False
        expected_side = 1 if self.pos.get("dir") == "LONG" else 0
        for o in self.cached_orders or []:
            if (
                o.get("contractId") == self.contract_id
                and o.get("status") in [0, 1]
                and o.get("type") in [1, 4]
                and o.get("side") == expected_side
            ):
                return True
        return False

    def _rebase_position_to_fill(self, fill_price):
        if not self.pos or not fill_price or fill_price <= 0:
            return
        old_ep = self.pos.get("ep")
        if not old_ep:
            return
        shift = float(fill_price) - float(old_ep)
        self.pos["ep"] = round(float(fill_price) / TICK_SIZE) * TICK_SIZE
        if self.pos.get("sl"):
            self.pos["sl"] = round((self.pos["sl"] + shift) / TICK_SIZE) * TICK_SIZE
        if self.pos.get("tp"):
            self.pos["tp"] = round((self.pos["tp"] + shift) / TICK_SIZE) * TICK_SIZE
        self.pos["bp"] = self.pos["ep"]
        print(f"  [BOT] Entry rebased to fill: {old_ep:.2f} -> {self.pos['ep']:.2f}")

    async def setup(self, token):
        self.token = token
        self.token_ts = datetime.now(timezone.utc)  # BUG6
        self.headers = {"Authorization": f"Bearer {self.token}"}
        r = await self.client.post(f"{API}/Account/search", json={"onlyActiveAccounts": True}, headers=self.headers)
        accts = r.json().get("accounts", [])
        prac = [a for a in accts if "PRAC" in a.get("name", "").upper() and a.get("canTrade")]
        if not prac: prac = [a for a in accts if a.get("canTrade")]
        if prac:
            self.account_id = prac[0]["id"]
            self.account_name = prac[0].get("name", str(self.account_id))
        r = await self.client.post(f"{API}/Contract/available", json={"live": False}, headers=self.headers)
        target_contract = [c for c in r.json().get("contracts", []) if c.get("id") == CONTRACT or CONTRACT.replace("CON.", "") in c.get("id", "")]
        if target_contract: self.contract_id = target_contract[0]["id"]
        if self.account_id and self.contract_id:
            self.ready = True
            print(f"  [BOT] Ready. Acct: {self.account_name} ({self.account_id}) | Contract: {self.contract_id}")
            # FIX: Instrument config diagnostic — surfaces any symbol/tick-value drift at boot.
            # If CONTRACT, contract_id, or TICK_VALUE ever disagree, this is the first place it shows.
            print(f"  [DIAG] CONTRACT={CONTRACT} | server_contract_id={self.contract_id} | TICK_SIZE={TICK_SIZE} | TICK_VALUE=${TICK_VALUE} | CONTRACTS={CONTRACTS} | $/pt={TICK_VALUE/TICK_SIZE:.2f}")
            try:
                pshot = await self._get_open_position_snapshot()
                print(f"  [DIAG] Server position at boot: size={self._extract_open_size(pshot)} avg={self._extract_avg_price(pshot)}")
            except Exception as e:
                print(f"  [DIAG] Server position at boot: query failed ({e})")

    async def refresh_token_if_needed(self):
        """BUG6: Re-authenticate if token is older than 12 hours."""
        if not self.token_ts: return
        age = (datetime.now(timezone.utc) - self.token_ts).total_seconds()
        if age < 12 * 3600: return
        try:
            u=os.environ.get("PROJECT_X_USERNAME","");k=os.environ.get("PROJECT_X_API_KEY","")
            r = await self.client.post(f"{API}/Auth/loginKey", json={"userName":u,"apiKey":k})
            d = r.json()
            if d.get("success") and d.get("token"):
                self.token = d["token"]
                self.token_ts = datetime.now(timezone.utc)
                self.headers = {"Authorization": f"Bearer {self.token}"}
                print(f"\n  [BOT] Token refreshed successfully.")
        except Exception as e:
            print(f"\n  [BOT] Token refresh failed: {e}")

    async def place_order(self, action: str, size: int, sl_price: float = None, tp_price: float = None, entry_price: float = None):
        side = 0 if action.upper() == "BUY" else 1
        p = {
            "accountId": self.account_id,
            "contractId": self.contract_id,
            "type": 2, # Market
            "side": side,
            "size": size,
            "customTag": f"B_{int(datetime.now().timestamp())}"
        }

        ref_price = entry_price if entry_price and entry_price > 0 else live_last
        if ref_price and ref_price > 0:
            if sl_price and sl_price > 0:
                sl_ticks = int(round(abs(ref_price - sl_price) / TICK_SIZE))
                sl_ticks = max(4, sl_ticks)
                p["stopLossBracket"] = {"ticks": sl_ticks, "type": 4}
            if tp_price and tp_price > 0:
                tp_abs = abs(int(round((tp_price - ref_price) / TICK_SIZE)))
                if tp_abs > 0:
                    # TP ticks are always positive — API calculates direction from the order side
                    p["takeProfitBracket"] = {"ticks": tp_abs, "type": 1}
            
        print(f"\n  [BOT] ORDER -> {action} {size} @ MKT | Ref: {ref_price} | SL: {sl_price} | TP: {tp_price}")
        if DRY_RUN: print(f"  [DRY RUN] {p}"); return {"success": True, "orderId": 999}
        
        await self.refresh_token_if_needed()
        r = await self.client.post(f"{API}/Order/place", json=p, headers=self.headers)
        res = r.json()
        if res.get("success"):
            print(f"  [SUCCESS] Order placed: {res.get('orderId')}")
        else:
            print(f"  [FAILED] {res}")
            # Cooldown after rejection to prevent spam
            self._reject_cooldown = datetime.now() + timedelta(seconds=30)
        return res

    async def modify_order(self, order_id: int, stop_price: float = None, limit_price: float = None):
        """v15: Move server-side brackets (Breakeven/Trailing)"""
        p = {"accountId": self.account_id, "orderId": order_id}
        if stop_price: p["stopPrice"] = stop_price
        if limit_price: p["limitPrice"] = limit_price
        
        print(f"  [BOT] MODIFY -> Order {order_id} to SL:{stop_price} TP:{limit_price}")
        if DRY_RUN: return True
        try:
            r = await self.client.post(f"{API}/Order/modify", json=p, headers=self.headers)
            res = r.json()
            return res.get("success", False)
        except Exception as e:
            print(f"  [MODIFY ERROR] Order {order_id}: {e}")
            return False

    async def get_active_orders(self):
        """Query platform for child bracket orders."""
        try:
            r = await self.client.post(f"{API}/Order/active", json={"accountId": self.account_id}, headers=self.headers)
            if r.status_code == 401:
                # Token expired — trigger refresh, suppress repeat logging
                if not getattr(self, "_orders_auth_warned", False):
                    print(f"\n  [ACTIVE ORDERS] 401 Unauthorized — forcing token refresh...")
                    self._orders_auth_warned = True
                    self.token_ts = None  # force token refresh next tick
                return []
            self._orders_auth_warned = False
            self._orders_err_warned = False
            text = r.text.strip() if hasattr(r, "text") else ""
            if not text:
                return []   # 204 No Content or empty body — no active orders
            return r.json().get("orders", [])
        except Exception as e:
            # Suppress repeat error spam — log once until it clears
            if not getattr(self, "_orders_err_warned", False):
                print(f"\n  [ACTIVE ORDERS ERROR] {e}")
                self._orders_err_warned = True
            return []

    async def cancel_order(self, order_id: int):
        """Cancel a single order by ID."""
        if not order_id:
            return False
        try:
            p = {"accountId": self.account_id, "orderId": order_id}
            r = await self.client.post(f"{API}/Order/cancel", json=p, headers=self.headers)
            res = r.json()
            if res.get("success"):
                print(f"  [CANCEL] Order {order_id} cancelled.")
                return True
            else:
                print(f"  [CANCEL FAILED] Order {order_id}: {res}")
                return False
        except Exception as e:
            print(f"  [CANCEL ERROR] Order {order_id}: {e}")
            return False

    async def cancel_all_bracket_orders(self):
        """Cancel all outstanding bracket orders (SL + TP) for current position."""
        if not self.pos or not self.contract_id:
            return
        try:
            expected_side = 1 if self.pos.get("dir") == "LONG" else 0
            orders = await self.get_active_orders()
            for o in orders:
                if (
                    o.get("contractId") == self.contract_id
                    and o.get("status") in [0, 1]
                    and o.get("type") in [1, 4]
                    and o.get("side") == expected_side
                ):
                    await self.cancel_order(o.get("id"))
        except Exception as e:
            print(f"  [BRACKET CLEANUP ERROR] {e}")

    async def sync_from_server(self):
        """Server sync: cache orders, rebase filled entry price, detect external bracket closes safely.
        Also detects orphaned server positions (server has position, bot thinks flat)."""
        import time
        now = time.time()
        if not hasattr(self, "last_sync_ts"): self.last_sync_ts = 0
        if now - self.last_sync_ts < 5: return  # throttle to 5 sec
        self.last_sync_ts = now
        
        # Always cache active orders for dashboard
        try:
            self.cached_orders = await self.get_active_orders()
        except Exception as e:
            print(f"  [SYNC ORDERS ERROR] {e}")
            self.cached_orders = []

        # Always check server position state — even when bot thinks it's flat
        pshot = await self._get_open_position_snapshot()
        open_size = self._extract_open_size(pshot)
        avg_price = self._extract_avg_price(pshot)

        # FIX: If API failed (open_size == -1), skip ALL reconciliation this tick.
        # Drawing conclusions from missing data is how ghost positions happen.
        if open_size < 0:
            return

        if self.pos:
            # Bot thinks it has a position — check if server agrees
            pos_age = (datetime.now() - self.pos.get("created_at", datetime.now())).total_seconds()

            if ENTRY_REBASE_ENABLED and not self.pos.get("fill_synced") and open_size >= CONTRACTS and avg_price:
                old_ep = self.pos.get("ep")
                self._rebase_position_to_fill(avg_price)
                self.pos["fill_synced"] = True
                print(f"  [FILL SYNC] size={open_size} avg={avg_price:.2f} ep {old_ep:.2f}->{self.pos.get('ep', 0):.2f}")

            # External close detection: after settle window (fill_synced or 8+ sec age) and server shows flat
            can_detect_close = self.pos.get("fill_synced") or pos_age >= 8
            if can_detect_close and pos_age >= 2 and open_size == 0:
                fill_price = live_last if live_last and live_last > 0 else self.pos.get("tp") or self.pos.get("sl") or self.pos.get("ep", 0)
                print(f"  [EXTERNAL CLOSE] Server shows flat after {pos_age:.0f}s, fill_synced={self.pos.get('fill_synced')}")
                await self.finalize_external_close(fill_price, "SERVER FILL", 0)
        else:
            # Bot thinks it's flat — check if server has an orphaned position
            if open_size > 0 and pshot:
                # Suppress during entry_pending (order is being placed)
                if not self.entry_pending:
                    if not hasattr(self, '_orphan_alert_ts'):
                        self._orphan_alert_ts = 0
                    if not hasattr(self, '_orphan_close_attempts'):
                        self._orphan_close_attempts = 0
                    
                    # FIX: Emergency close flag (from RTH reset) — skip cooldown, close immediately
                    is_emergency = getattr(self, '_emergency_close', False)
                    confirmations_needed = 1 if is_emergency else 3
                    
                    if self._orphan_close_attempts == 0:
                        # First detection — start the clock
                        if is_emergency or now - self._orphan_alert_ts > 30:
                            self._orphan_alert_ts = now
                            self._orphan_close_attempts = 1
                            tag = "EMERGENCY" if is_emergency else "ORPHAN"
                            print(f"\n  [⚠ {tag} DETECTED] Server has position (size={open_size}, avg={avg_price}) but bot is flat!")
                            if not is_emergency:
                                print(f"    Will auto-close in ~15 seconds if still orphaned...")
                    elif self._orphan_close_attempts < confirmations_needed:
                        self._orphan_close_attempts += 1
                        print(f"  [ORPHAN] Still open. Confirmation {self._orphan_close_attempts}/{confirmations_needed}")
                    else:
                        # Confirmed orphan — auto-close
                        tag = "EMERGENCY" if is_emergency else "ORPHAN"
                        print(f"\n  [{tag} AUTO-CLOSE] Sending market order to close orphaned position (size={open_size})")
                        try:
                            # Determine close side from server position data
                            # TopStepX may use "side", "direction", "positionSide" etc.
                            server_side = None
                            for key in ("side", "direction", "positionSide", "type"):
                                val = pshot.get(key)
                                if val is not None:
                                    server_side = val
                                    break
                            
                            if server_side is None:
                                print(f"  [{tag} CLOSE ABORTED] Cannot determine position side from server data: {list(pshot.keys())}")
                                print(f"    Raw: {pshot}")
                                print(f"    ACTION REQUIRED: Close manually on platform")
                                self._orphan_close_attempts = 0  # reset, will re-detect
                            else:
                                if isinstance(server_side, int):
                                    act = "SELL" if server_side == 0 else "BUY"  # 0=Long->Sell, 1=Short->Buy
                                else:
                                    s = str(server_side).upper()
                                    if s in ("LONG", "BUY", "0", "B"):
                                        act = "SELL"
                                    elif s in ("SHORT", "SELL", "1", "S"):
                                        act = "BUY"
                                    else:
                                        print(f"  [{tag} CLOSE ABORTED] Unrecognized side value: {server_side}")
                                        self._orphan_close_attempts = 0
                                        raise ValueError(f"Unknown side: {server_side}")
                                
                                res = await self.place_order(act, open_size)
                                if res.get("success"):
                                    print(f"  [{tag} CLOSED] Market order sent successfully")
                                else:
                                    print(f"  [{tag} CLOSE FAILED] {res}")
                        except Exception as e:
                            print(f"  [{tag} CLOSE ERROR] {e}")
                        self._orphan_close_attempts = 0
                        self._orphan_alert_ts = now
                        self._emergency_close = False  # clear the flag
                else:
                    # Reset orphan counter during entry_pending
                    self._orphan_close_attempts = 0
            else:
                # Server confirmed flat, reset orphan state
                if hasattr(self, '_orphan_close_attempts'):
                    self._orphan_close_attempts = 0
                if getattr(self, '_emergency_close', False):
                    self._emergency_close = False

    async def finalize_external_close(self, price, reason, bar_idx):
        """Book a position closed externally (server bracket/manual close) without sending a market order."""
        if not self.pos:
            return

        reason = normalize_exit_reason(reason)
        # FIX#2: capture UUID before any async work
        pos_uuid = self.pos.get("uuid")
        pos_snapshot = self.pos  # cache before any async calls

        # Cancel any remaining bracket orders (the one that didn't fill)
        try:
            await self.cancel_all_bracket_orders()
        except Exception as e:
            print(f"  [WARN] Bracket cancel error during finalize: {e}")

        # FIX#2: If another path already closed this position, bail out
        if not self.pos or self.pos.get("uuid") != pos_uuid:
            print(f"  [BOT] Position already closed by another path (uuid mismatch)")
            return

        if not price or price <= 0:
            price = pos_snapshot.get("ep", 0)

        size = pos_snapshot.get("contracts_remaining", CONTRACTS)
        ticks = (price - pos_snapshot["ep"]) / TICK_SIZE if pos_snapshot["dir"] == "LONG" else (pos_snapshot["ep"] - price) / TICK_SIZE
        gross_pnl = ticks * TICK_VALUE * size
        net_pnl = gross_pnl - COMMISSION_RT * size
        print(f"\n  [BOT] CLOSED {pos_snapshot['dir']} @ {price:.2f} ({reason}) | Net PnL: ${net_pnl:.2f}")

        # Direction cooldown — block same-direction entries after a stop-out
        normalized_reason = normalize_exit_reason(reason)
        if normalized_reason in ("STOP LOSS", "TRAIL STOP"):
            if pos_snapshot["dir"] == "LONG":
                self._cooldown_long_until = bar_idx + DIR_COOLDOWN_BARS
            else:
                self._cooldown_short_until = bar_idx + DIR_COOLDOWN_BARS

        # FIX#1: Acquire state lock for all P&L and pos mutations
        with BOT_STATE_LOCK:
            # FIX#2: Final UUID re-check under lock — HTTP thread may have nulled pos via RTH reset
            if not self.pos or self.pos.get("uuid") != pos_uuid:
                print(f"  [BOT] Position cleared by another thread during finalize (uuid mismatch)")
                return

            self.total_pnl += gross_pnl
            self.daily_pnl += gross_pnl
            self.trade_history.append({"action": "EXIT", "dir": pos_snapshot["dir"], "price": price, "pnl": net_pnl, "bar_idx": bar_idx, "reason": reason, "size": size})
            self.pnl_history.append((datetime.now(), net_pnl))
            self.equity_history.append((datetime.now(), self.total_pnl))

            # FIX#8: Prune history older than 2 hours
            cutoff = datetime.now() - timedelta(hours=2)
            self.pnl_history = [(t, p) for t, p in self.pnl_history if t > cutoff]
            self.equity_history = [(t, e) for t, e in self.equity_history if t > cutoff]

            now = datetime.now()
            hourly_pnl = sum([p for t, p in self.pnl_history if t > now - timedelta(minutes=60)])
            h_eq = [e for t, e in self.equity_history if t > now - timedelta(minutes=60)]
            h_dd = 0
            if h_eq:
                h_peak = max(h_eq)
                h_dd = h_peak - self.total_pnl

            trigger_val = hourly_pnl if HOURLY_GUARD_TYPE == "PL" else -h_dd
            if trigger_val <= -MAX_HOURLY_LOSS:
                self.lockdown_until = now + timedelta(minutes=60)
                guard_reason = "Hourly P/L" if HOURLY_GUARD_TYPE == "PL" else "Hourly Drawdown"
                print(f"\n  [LOCKDOWN] {guard_reason} hit limit. Trading disabled for 60 mins until {self.lockdown_until.strftime('%H:%M:%S')}")

            self.last_exit_bar = bar_idx
            self.pos = None
        print(f"  [BOT] Total: ${self.total_pnl:.2f} | Hourly PnL: ${hourly_pnl:.2f} | Hourly DD: ${h_dd:.2f}")

    async def update_stop_on_server(self, new_stop: float):
        """Find the child stop order and modify it."""
        try:
            orders = await self.get_active_orders()
            # SL is a STOP order (type=4) on the closing side:
            # LONG position → SL is a SELL stop (side=1)
            # SHORT position → SL is a BUY stop (side=0)
            expected_side = 1 if self.pos and self.pos.get("dir") == "LONG" else 0
            sl_order = None
            for o in orders:
                if (o.get("contractId") == self.contract_id
                        and o.get("type") == 4
                        and o.get("side") == expected_side):
                    sl_order = o
                    break
            
            if sl_order:
                oid = sl_order["id"]
                success = await self.modify_order(oid, stop_price=new_stop)
                if success:
                    print(f"  [SUCCESS] Server-side Stop moved to {new_stop}")
                    return True
        except Exception as e:
            print(f"  [STOP UPDATE ERROR] {e}")
        return False

    async def update_tp_on_server(self, new_tp: float):
        """Find the child limit (TP) order and modify it to new_tp."""
        try:
            orders = await self.get_active_orders()
            # TP is a LIMIT order (type=1) on the closing side:
            # LONG position → TP is a SELL limit (side=1)
            # SHORT position → TP is a BUY limit (side=0)
            expected_side = 1 if self.pos and self.pos.get("dir") == "LONG" else 0
            tp_order = None
            for o in orders:
                if (o.get("contractId") == self.contract_id
                        and o.get("type") == 1
                        and o.get("side") == expected_side):
                    tp_order = o
                    break
            if tp_order:
                oid = tp_order["id"]
                success = await self.modify_order(oid, limit_price=new_tp)
                if success:
                    print(f"  [TP UPDATE] Server-side TP moved to {new_tp:.2f}")
                    return True
        except Exception as e:
            print(f"  [TP UPDATE ERROR] {e}")
        return False

    async def close_position(self, price, reason, bar_idx):
        if not self.pos: return

        reason = normalize_exit_reason(reason)
        # FIX#2: capture UUID before any async work
        pos_uuid = self.pos.get("uuid")
        pos_snapshot = self.pos  # cache in case of concurrent modification

        # ---- CRITICAL: Check server state BEFORE sending exit order ----
        # If the server already closed the position (bracket fill), don't send
        # another market order — that would OPEN a new position in the opposite direction!
        try:
            pshot = await self._get_open_position_snapshot()
            server_size = self._extract_open_size(pshot)
            if server_size == 0:
                print(f"\n  [BOT] Server already flat — booking as external close (no market order sent)")
                await self.finalize_external_close(price, reason, bar_idx)
                return
            if server_size < 0:
                # FIX: API failed — we don't know server state. Do NOT send a market order.
                # Server brackets are still protecting the position. Retry next tick.
                print(f"  [WARN] Cannot verify server state — skipping close, will retry next tick")
                return
        except Exception as e:
            # FIX: Network error — same logic: do NOT proceed blindly.
            print(f"  [WARN] Server check failed: {e} — skipping close, will retry next tick")
            return

        # Cancel any outstanding bracket orders before closing
        try:
            await self.cancel_all_bracket_orders()
        except Exception as e:
            print(f"  [WARN] Bracket cancel error during close: {e}")

        # FIX#2: Guard on UUID — if another path already closed this exact position, bail
        if not self.pos or self.pos.get("uuid") != pos_uuid:
            print(f"  [BOT] Position already closed during bracket cleanup (uuid mismatch)")
            return

        # FIX: RE-VERIFY server state after bracket cancel — a bracket may have filled
        # during the cancel_all_bracket_orders await, closing the position.
        # Sending a market order now would OPEN a reverse position.
        try:
            pshot2 = await self._get_open_position_snapshot()
            size2 = self._extract_open_size(pshot2)
            if size2 == 0:
                print(f"\n  [BOT] Position closed by bracket fill during cleanup — booking as external close")
                await self.finalize_external_close(price, reason, bar_idx)
                return
            if size2 < 0:
                print(f"  [WARN] Cannot re-verify server state — aborting close, will retry next tick")
                return
        except Exception as e:
            print(f"  [WARN] Re-verify failed: {e} — aborting close, will retry next tick")
            return

        size = pos_snapshot.get("contracts_remaining", CONTRACTS)
        ticks = (price - pos_snapshot["ep"]) / TICK_SIZE if pos_snapshot["dir"] == "LONG" else (pos_snapshot["ep"] - price) / TICK_SIZE
        gross_pnl = ticks * TICK_VALUE * size
        net_pnl = gross_pnl - COMMISSION_RT * size
        print(f"\n  [BOT] CLOSING {pos_snapshot['dir']} @ {price:.2f} ({reason}) | Net PnL: ${net_pnl:.2f}")

        # Direction cooldown — block same-direction entries after a stop-out
        normalized_reason = normalize_exit_reason(reason)
        if normalized_reason in ("STOP LOSS", "TRAIL STOP"):
            if pos_snapshot["dir"] == "LONG":
                self._cooldown_long_until = bar_idx + DIR_COOLDOWN_BARS
            else:
                self._cooldown_short_until = bar_idx + DIR_COOLDOWN_BARS

        # Market out
        act = "SELL" if pos_snapshot["dir"] == "LONG" else "BUY"
        try:
            res = await self.place_order(act, size)
        except Exception as e:
            print(f"  [BOT] ⚠ ORDER EXCEPTION: {e} — position still open, will retry next tick")
            return
        success = res.get("success", False)
        
        if success or DRY_RUN:
            # ---- POST-CLOSE VERIFICATION ----
            # Wait briefly then check if server actually shows flat
            if not DRY_RUN:
                await asyncio.sleep(0.5)  # give server time to process
                try:
                    pshot_after = await self._get_open_position_snapshot()
                    size_after = self._extract_open_size(pshot_after)
                    if size_after > 0:
                        print(f"  [⚠ CLOSE VERIFY] Server still shows position (size={size_after}) after exit order!")
                        print(f"    Keeping pos state — will retry next tick")
                        return  # DON'T clear pos — server still has it
                    elif size_after < 0:
                        # FIX: API failed — can't confirm. Keep pos alive, sync_from_server will reconcile.
                        print(f"  [WARN] Post-close verification API failed — keeping pos state, will reconcile via sync")
                        return  # DON'T clear pos — state unknown
                    else:
                        print(f"  [✓ CLOSE VERIFY] Server confirmed flat")
                except Exception as e:
                    # FIX: Don't assume success — keep pos alive for next tick retry
                    print(f"  [WARN] Post-close verification exception: {e} — keeping pos state, will retry")
                    return
            
            # FIX#1: Acquire state lock for all P&L and pos mutations
            with BOT_STATE_LOCK:
                # FIX#2: Final UUID check after all async work
                if not self.pos or self.pos.get("uuid") != pos_uuid:
                    print(f"  [BOT] Position closed by another path during exit order (uuid mismatch)")
                    return

                self.total_pnl += gross_pnl
                self.daily_pnl += gross_pnl
                self.trade_history.append({"action": "EXIT", "dir": pos_snapshot["dir"], "price": price, "pnl": net_pnl, "bar_idx": bar_idx, "reason": reason, "size": size})
                self.pnl_history.append((datetime.now(), net_pnl))
                self.equity_history.append((datetime.now(), self.total_pnl))

                # FIX#8: Prune history older than 2 hours
                cutoff = datetime.now() - timedelta(hours=2)
                self.pnl_history = [(t, p) for t, p in self.pnl_history if t > cutoff]
                self.equity_history = [(t, e) for t, e in self.equity_history if t > cutoff]

                # --- v9 HOURLY GUARD ---
                now = datetime.now()
                hourly_pnl = sum([p for t, p in self.pnl_history if t > now - timedelta(minutes=60)])
                h_eq = [e for t, e in self.equity_history if t > now - timedelta(minutes=60)]
                h_dd = 0
                if h_eq:
                    h_peak = max(h_eq)
                    h_dd = h_peak - self.total_pnl
                
                trigger_val = hourly_pnl if HOURLY_GUARD_TYPE == "PL" else -h_dd
                if trigger_val <= -MAX_HOURLY_LOSS:
                    self.lockdown_until = now + timedelta(minutes=60)
                    guard_reason = "Hourly P/L" if HOURLY_GUARD_TYPE == "PL" else "Hourly Drawdown"
                    print(f"\n  [LOCKDOWN] {guard_reason} hit limit. Trading disabled for 60 mins until {self.lockdown_until.strftime('%H:%M:%S')}")

                self.last_exit_bar = bar_idx
                self.pos = None
            print(f"  [BOT] Total: ${self.total_pnl:.2f} | Hourly PnL: ${hourly_pnl:.2f} | Hourly DD: ${h_dd:.2f}")
        else:
            print(f"  [BOT] ⚠ ORDER FAILED — position still open, will retry next tick")

    async def check_logic(self, price, vwap, vpoc, std, bar_idx, rth, stable=True,
                          vpoc60=0, vol=0, bar_range=0.0, tod_mins=0,
                          vwap_dist=0.0, vpoc_dist=0.0, vwap_slope=0.0,
                          vwap_vpoc_dist=0.0, mom_5=0.0, velocity_10=0.0, vol_rel=1.0, rng_rel=1.0,
                          cum_vol=0, vol_60=0, poc_mig=0.0, sess_pct=0.5,
                          body_size=0.0, wick_low=0.0, wick_high=0.0,
                          sigma_exp=1.0, prev_h=0.0, prev_l=999999.0,
                          atr_slope=0.0, atr=0.0, spread=0.0, bar_close_event=False,
                          cum_delta_pct=0.0):
        if not self.ready or not vwap or not std or bar_idx < 15: return

        # Track equity and drawdown on every tick
        epnl = 0
        if self.pos:
            ep = self.pos["ep"]
            size_now = self.pos.get("contracts_remaining", CONTRACTS)
            ticks = (price - ep) / TICK_SIZE if self.pos["dir"] == "LONG" else (ep - price) / TICK_SIZE
            epnl = ticks * TICK_VALUE * size_now
        cur_eq = self.total_pnl + epnl
        if cur_eq > self.peak_pnl: self.peak_pnl = cur_eq
        dd = self.peak_pnl - cur_eq
        if dd > self.max_dd: self.max_dd = dd
        self._open_pnl = epnl  # cache for HTTP handler
        
        if not rth:
            if self.pos: await self.close_position(price, "END OF RTH", bar_idx)
            return
        
        target = vpoc if TARGET_MODE == "volume" else round(vwap / TICK_SIZE) * TICK_SIZE
        if not target: return
        target_z = (price - target) / std if std >= TICK_SIZE else 0

        # FIX: MLL-aware daily loss cap — prevents blowing combine when MLL floor is close
        # Uses realized balance (not open P&L) so the cap is stable during a position
        effective_dd = GUTTER_DD
        if self.gutter_loss:
            mll_room = self.account_balance - self.mll_floor
            if mll_room < GUTTER_DD:
                effective_dd = max(0, mll_room - 100)  # $100 safety buffer above MLL
                if effective_dd < GUTTER_DD:
                    print(f"\r  [MLL GUARD] Daily loss cap reduced: ${GUTTER_DD} -> ${effective_dd:.0f} (MLL room: ${mll_room:.0f})  ", end="")

        if self.pos:
            bt = bar_idx - self.pos["bar_idx"]
            
            if self.pos["dir"] == "LONG":
                _sz = self.pos.get("contracts_remaining", CONTRACTS)
                if self.gutter_win and self.daily_pnl < GUTTER_GOAL:
                    gut_tp = self.pos["ep"] + ((GUTTER_GOAL - self.daily_pnl) / (_sz*TICK_VALUE)) * TICK_SIZE
                    if price >= gut_tp: await self.close_position(gut_tp, "GUTTER WIN", bar_idx); return
                if self.gutter_loss and self.daily_pnl > -effective_dd:
                    remaining = (self.daily_pnl + effective_dd) / (_sz*TICK_VALUE)
                    if remaining > 0:
                        gut_sl = self.pos["ep"] - remaining * TICK_SIZE
                        if price <= gut_sl: await self.close_position(gut_sl, "GUTTER LOSS", bar_idx); return

                if price > self.pos["bp"]: self.pos["bp"] = price
                ur = (self.pos["bp"] - self.pos["ep"]) / TICK_SIZE
                cur = (price - self.pos["ep"]) / TICK_SIZE
                use_local_bracket_exit = (not SERVER_BRACKETS_PRIMARY) or DRY_RUN or (not self._has_working_brackets_for_pos())

                # Client-side stop (matches validated backtest)
                if use_local_bracket_exit and price <= self.pos["sl"]:
                    sr = classify_stop_reason("LONG", self.pos.get("ep"), self.pos.get("sl"))
                    await self.close_position(price, sr, bar_idx); return
                if use_local_bracket_exit and target and price >= target and cur >= EXIT_MIN_TICKS: await self.close_position(price, "TARGET", bar_idx); return

                # BACKUP: Even when server brackets are primary, if price blows past SL/TP by 2+ ticks, force close client-side
                # This catches cases where server brackets fail to fire
                if SERVER_BRACKETS_PRIMARY and not use_local_bracket_exit:
                    sl_breach = (self.pos["sl"] - price) / TICK_SIZE  # positive = price below SL
                    if sl_breach >= 2:
                        sr = classify_stop_reason("LONG", self.pos.get("ep"), self.pos.get("sl"))
                        print(f"  [BACKUP EXIT] SL breached by {sl_breach:.0f} ticks but server bracket didn't fire")
                        await self.close_position(price, sr, bar_idx); return
                    # FIX: Compare against actual bracket level (pos["tp"]), not VWAP target
                    tp_level = self.pos.get("tp", target)
                    if tp_level and price >= tp_level + 2 * TICK_SIZE and cur >= EXIT_MIN_TICKS:
                        print(f"  [BACKUP EXIT] TP breached by {((price - tp_level) / TICK_SIZE):.0f} ticks but server bracket didn't fire")
                        await self.close_position(price, "TARGET", bar_idx); return

                # --- Partial scale-out: exit 1 contract at VWAP, keep 1 trailing ---
                if (SCALE_OUT_ENABLED and _sz > 1 and not self.pos.get("scale1_done")
                        and target and price >= target and cur >= EXIT_MIN_TICKS):
                    # Cancel TP bracket only (leave SL intact), then sell 1 contract MKT
                    try:
                        orders = await self.get_active_orders()
                        for o in orders:
                            if (o.get("contractId") == self.contract_id
                                    and o.get("type") == 1  # LIMIT = TP
                                    and o.get("side") == 1):  # SELL side for LONG pos
                                await self.cancel_order(o["id"])
                                break
                    except Exception as _e:
                        print(f"  [SCALE-OUT] TP cancel error: {_e}")
                    try:
                        _res = await self.place_order("SELL", 1)
                    except Exception as _e:
                        print(f"  [SCALE-OUT] Order error: {_e}"); _res = {}
                    if _res.get("success"):
                        so_ticks = cur; so_pnl = so_ticks * TICK_VALUE * 1
                        so_net = so_pnl - COMMISSION_RT * 1
                        with BOT_STATE_LOCK:
                            self.total_pnl += so_pnl; self.daily_pnl += so_pnl
                            self.pos["contracts_remaining"] = _sz - 1
                            self.pos["scale1_done"] = True
                            # Move SL to breakeven now that we're partially out
                            if self.pos["sl"] < self.pos["ep"]:
                                self.pos["sl"] = self.pos["ep"]
                            self.trade_history.append({"action": "EXIT", "dir": "LONG", "price": price, "pnl": so_net, "bar_idx": bar_idx, "reason": "SCALE OUT", "size": 1})
                        print(f"  [SCALE-OUT] Sold 1 LONG @ {price:.2f} | Net: ${so_net:.2f} | Remaining: {self.pos.get('contracts_remaining',1)}")

                # Client-side trailing stop
                if ur >= TRAIL_ACTIVATE:
                    tl = round((self.pos["bp"] - TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl > self.pos["sl"]:
                        if await self.update_stop_on_server(tl):
                            self.pos["sl"] = tl

                # Breakeven
                if ur >= BREAKEVEN_TICKS and self.pos["sl"] < self.pos["ep"]:
                    if await self.update_stop_on_server(self.pos["ep"]):
                        self.pos["sl"] = self.pos["ep"]

                # Live TP drift update — as VWAP drifts, recalc and push TP to server
                if self.pos.get("entry_vwap") and self.pos.get("tp_buffer_ticks") and vwap > 0:
                    ev = self.pos["entry_vwap"]
                    drift_ticks = abs(vwap - ev) / TICK_SIZE
                    if drift_ticks >= 3:
                        new_tp = round((vwap + self.pos["tp_buffer_ticks"] * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                        if abs(new_tp - self.pos["tp"]) >= TICK_SIZE:
                            if await self.update_tp_on_server(new_tp):
                                self.pos["tp"] = new_tp
                                self.pos["entry_vwap"] = vwap  # reset drift anchor

                # Dynamic time stop: exit early if losing at 30 bars, normal at TIME_STOP_MINS, hard cap at 2×
                if bt > TIME_STOP_MINS * 2:
                    print(f"  [SAFETY VALVE] Position open {bt} bars, force closing")
                    await self.close_position(price, "TIME STOP", bar_idx); return
                if bt > TIME_STOP_MINS:
                    await self.close_position(price, "TIME STOP", bar_idx); return
                early_stop = int(TIME_STOP_MINS * 0.5)  # e.g. 45 bars = half time
                if bt > early_stop and cur < -4:
                    await self.close_position(price, "TIME STOP", bar_idx); return
            else:
                # SHORT
                _sz = self.pos.get("contracts_remaining", CONTRACTS)
                if self.gutter_win and self.daily_pnl < GUTTER_GOAL:
                    gut_tp = self.pos["ep"] - ((GUTTER_GOAL - self.daily_pnl) / (_sz*TICK_VALUE)) * TICK_SIZE
                    if price <= gut_tp: await self.close_position(gut_tp, "GUTTER WIN", bar_idx); return
                if self.gutter_loss and self.daily_pnl > -effective_dd:
                    remaining = (self.daily_pnl + effective_dd) / (_sz*TICK_VALUE)
                    if remaining > 0:
                        gut_sl = self.pos["ep"] + remaining * TICK_SIZE
                        if price >= gut_sl: await self.close_position(gut_sl, "GUTTER LOSS", bar_idx); return

                if price < self.pos["bp"]: self.pos["bp"] = price
                ur = (self.pos["ep"] - self.pos["bp"]) / TICK_SIZE
                cur = (self.pos["ep"] - price) / TICK_SIZE
                use_local_bracket_exit = (not SERVER_BRACKETS_PRIMARY) or DRY_RUN or (not self._has_working_brackets_for_pos())
                if use_local_bracket_exit and price >= self.pos["sl"]:
                    sr = classify_stop_reason("SHORT", self.pos.get("ep"), self.pos.get("sl"))
                    await self.close_position(price, sr, bar_idx); return
                if use_local_bracket_exit and target and price <= target and cur >= EXIT_MIN_TICKS: await self.close_position(price, "TARGET", bar_idx); return

                # BACKUP: Even when server brackets are primary, if price blows past SL/TP by 2+ ticks, force close
                if SERVER_BRACKETS_PRIMARY and not use_local_bracket_exit:
                    sl_breach = (price - self.pos["sl"]) / TICK_SIZE  # positive = price above SL (short)
                    if sl_breach >= 2:
                        sr = classify_stop_reason("SHORT", self.pos.get("ep"), self.pos.get("sl"))
                        print(f"  [BACKUP EXIT] SL breached by {sl_breach:.0f} ticks but server bracket didn't fire")
                        await self.close_position(price, sr, bar_idx); return
                    # FIX: Compare against actual bracket level (pos["tp"]), not VWAP target
                    tp_level = self.pos.get("tp", target)
                    if tp_level and price <= tp_level - 2 * TICK_SIZE and cur >= EXIT_MIN_TICKS:
                        print(f"  [BACKUP EXIT] TP breached by {((tp_level - price) / TICK_SIZE):.0f} ticks but server bracket didn't fire")
                        await self.close_position(price, "TARGET", bar_idx); return

                # --- Partial scale-out: exit 1 contract at VWAP, keep 1 trailing ---
                if (SCALE_OUT_ENABLED and _sz > 1 and not self.pos.get("scale1_done")
                        and target and price <= target and cur >= EXIT_MIN_TICKS):
                    try:
                        orders = await self.get_active_orders()
                        for o in orders:
                            if (o.get("contractId") == self.contract_id
                                    and o.get("type") == 1  # LIMIT = TP
                                    and o.get("side") == 0):  # BUY side for SHORT pos
                                await self.cancel_order(o["id"])
                                break
                    except Exception as _e:
                        print(f"  [SCALE-OUT] TP cancel error: {_e}")
                    try:
                        _res = await self.place_order("BUY", 1)
                    except Exception as _e:
                        print(f"  [SCALE-OUT] Order error: {_e}"); _res = {}
                    if _res.get("success"):
                        so_ticks = cur; so_pnl = so_ticks * TICK_VALUE * 1
                        so_net = so_pnl - COMMISSION_RT * 1
                        with BOT_STATE_LOCK:
                            self.total_pnl += so_pnl; self.daily_pnl += so_pnl
                            self.pos["contracts_remaining"] = _sz - 1
                            self.pos["scale1_done"] = True
                            if self.pos["sl"] > self.pos["ep"]:
                                self.pos["sl"] = self.pos["ep"]
                            self.trade_history.append({"action": "EXIT", "dir": "SHORT", "price": price, "pnl": so_net, "bar_idx": bar_idx, "reason": "SCALE OUT", "size": 1})
                        print(f"  [SCALE-OUT] Bought 1 SHORT @ {price:.2f} | Net: ${so_net:.2f} | Remaining: {self.pos.get('contracts_remaining',1)}")

                # --- v15 Server-Side Trailing ---
                if ur >= TRAIL_ACTIVATE:
                    tl = round((self.pos["bp"] + TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl < self.pos.get("sl", 999999):
                        if await self.update_stop_on_server(tl):
                            self.pos["sl"] = tl

                # Breakeven
                if ur >= BREAKEVEN_TICKS and self.pos.get("sl", 999999) > self.pos["ep"]:
                    if await self.update_stop_on_server(self.pos["ep"]):
                        self.pos["sl"] = self.pos["ep"]

                # Live TP drift update — as VWAP drifts, recalc and push TP to server
                if self.pos.get("entry_vwap") and self.pos.get("tp_buffer_ticks") and vwap > 0:
                    ev = self.pos["entry_vwap"]
                    drift_ticks = abs(vwap - ev) / TICK_SIZE
                    if drift_ticks >= 3:
                        new_tp = round((vwap - self.pos["tp_buffer_ticks"] * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                        if abs(new_tp - self.pos["tp"]) >= TICK_SIZE:
                            if await self.update_tp_on_server(new_tp):
                                self.pos["tp"] = new_tp
                                self.pos["entry_vwap"] = vwap  # reset drift anchor

                # Dynamic time stop: exit early if losing at half-time, normal at full, hard cap at 2×
                if bt > TIME_STOP_MINS * 2:
                    print(f"  [SAFETY VALVE] Position open {bt} bars, force closing")
                    await self.close_position(price, "TIME STOP", bar_idx); return
                if bt > TIME_STOP_MINS:
                    await self.close_position(price, "TIME STOP", bar_idx); return
                early_stop = int(TIME_STOP_MINS * 0.5)
                if bt > early_stop and cur < -4:
                    await self.close_position(price, "TIME STOP", bar_idx); return
        else:
            # FIX: Bar-close gate. Entries fire only on the tick that finalizes a bar,
            # mirroring the backtester's per-bar sampling. Without this, live evaluates
            # entry on every tick (100-500x more samples per bar than backtest), making
            # the backtested edge non-transferable. Exits/stops/trails above still run
            # on every tick for fast risk reactions.
            if not bar_close_event:
                return

            # --- GUARD: Only 1 trade at a time (including pending entries) ---
            # Reset entry_pending if stuck for >10 seconds (safety valve for hung API calls)
            if self.entry_pending:
                if not hasattr(self, '_entry_pending_ts'):
                    self._entry_pending_ts = datetime.now()
                elif (datetime.now() - self._entry_pending_ts).total_seconds() > 10:
                    print(f"  [SAFETY] Resetting stuck entry_pending flag")
                    self.entry_pending = False
                    self._entry_pending_ts = None
            
            if self.pos or self.entry_pending: return
            
            await self.refresh_token_if_needed()  
            # --- HARD LIMITS (Anti-Implosion) ---
            if not BOT_ACTIVE or bar_idx < 45 or self.trades_today >= MAX_TRADES:
                return
            # Time-of-day filters: skip lunch and power hour
            if tod_mins >= POWER_HOUR_START:
                return
            if LUNCH_SKIP_START <= tod_mins <= LUNCH_SKIP_END:
                return
            if self.gutter_win and round(self.daily_pnl, 2) >= GUTTER_GOAL: return
            if self.gutter_loss and round(self.daily_pnl, 2) <= -effective_dd: return

            if self.lockdown_until and datetime.now() < self.lockdown_until:
                return

            if not stable: return

            # --- 1. TIME COOLDOWN ---
            # Wait 5 bars before allowing another trade
            if bar_idx - self.last_exit_bar < 5:
                return

            # VIX proxy — skip extreme vol, raise threshold on elevated vol
            vix = REGIME.get("vix_proxy")
            if vix and vix > 30:
                print(f"\r  [VIX SKIP] VIX={vix:.1f} extreme — no entries           ", end="")
                return
            # Economic calendar — skip or reduce on high-impact days
            cal_event = REGIME.get("calendar_event")
            if cal_event and HIGH_IMPACT_BEHAVIOR == "skip":
                print(f"\r  [CAL SKIP] {cal_event} today — no entries                ", end="")
                return

            target_z = (price - target) / std if std >= TICK_SIZE else 0
            z_abs = abs(target_z)
            # Z-threshold: use deterministic session-type + crossings heuristics.
            crossings = REGIME.get("vwap_crossings", VWAP_CROSS_BALANCED)
            if REGIME.get("session_type") == "TRENDING":
                effective_z_thresh = Z_THRESH * SESSION_TREND_Z_MULT
            elif crossings <= VWAP_CROSS_TRENDING:
                effective_z_thresh = Z_THRESH * 1.3
            else:
                effective_z_thresh = Z_THRESH
            if vix and vix > 22:
                effective_z_thresh = max(effective_z_thresh, Z_THRESH + 0.3)
            # Calendar reduce — raise threshold further on high-impact days
            if cal_event and HIGH_IMPACT_BEHAVIOR == "reduce":
                effective_z_thresh = max(effective_z_thresh, Z_THRESH + 0.5)
            # Previous session level confluence — lower threshold when near prev VWAP/VPOC
            prev_vwap = REGIME.get("prev_vwap"); prev_vpoc = REGIME.get("prev_vpoc")
            near_prev_level = False
            if prev_vwap and abs(price - prev_vwap) <= PREV_LEVEL_TICKS * TICK_SIZE:
                near_prev_level = True
            if prev_vpoc and abs(price - prev_vpoc) <= PREV_LEVEL_TICKS * TICK_SIZE:
                near_prev_level = True
            if near_prev_level:
                effective_z_thresh = max(Z_THRESH * 0.8, effective_z_thresh - PREV_LEVEL_Z_BONUS)

            if z_abs >= effective_z_thresh:
                # --- Direction cooldown after stop-out ---
                if target_z < 0 and bar_idx < self._cooldown_long_until:
                    print(f"\r  [DIR COOLDOWN] LONG blocked until bar {self._cooldown_long_until}  ", end="")
                    return
                if target_z > 0 and bar_idx < self._cooldown_short_until:
                    print(f"\r  [DIR COOLDOWN] SHORT blocked until bar {self._cooldown_short_until}  ", end="")
                    return

                dist = abs(price - target)
                # ATR-based stop sizing — adapts to current session volatility
                if atr > 0:
                    atr_ticks = max(1, int(atr / TICK_SIZE))
                    sd = max(MIN_STOP_TICKS, min(MAX_STOP_TICKS, int(atr_ticks * ATR_STOP_RATIO))) * TICK_SIZE
                else:
                    sd = max(min(dist * STOP_RATIO, MAX_STOP_TICKS * TICK_SIZE), MIN_STOP_TICKS * TICK_SIZE)

                # --- MICRO-STRUCTURE ---
                # Sigma Panic Filter — A/B tested, +$3.18/trade
                if sigma_exp > 1.25:
                    print(f"\r  [v7 SKIP] Sigma Panic: {sigma_exp-1:.1%} expansion             ", end="")
                    return
                # ATR Slope Filter — A/B tested, +$8.44/trade
                if atr_slope > 0.10:
                    print(f"\r  [ATR SKIP] Range expanding: {atr_slope:.1%} slope             ", end="")
                    return
                # VWAP slope filter — don't fade direction of strong VWAP drift
                if target_z < 0 and vwap_slope < -VWAP_SLOPE_THRESH:
                    print(f"\r  [SLOPE SKIP] VWAP declining ({vwap_slope:.2f}) — no long      ", end="")
                    return
                if target_z > 0 and vwap_slope > VWAP_SLOPE_THRESH:
                    print(f"\r  [SLOPE SKIP] VWAP rising ({vwap_slope:.2f}) — no short        ", end="")
                    return
                # Volume confirmation — skip thin-tape deviations
                if vol_rel < MIN_VOL_REL:
                    print(f"\r  [VOL SKIP] Thin bar: {vol_rel:.2f}× avg                ", end="")
                    return
                # Spread filter — skip wide bid-ask
                if spread > MAX_SPREAD:
                    print(f"\r  [SPREAD SKIP] Wide spread: {spread:.2f}                 ", end="")
                    return
                # Cumulative delta filter — skip if order flow strongly opposes the fade
                if CUM_DELTA_FILTER:
                    if target_z < 0 and cum_delta_pct < -CUM_DELTA_FADE_MAX:
                        # Strong sell flow while fading down to VWAP — sellers in control
                        print(f"\r  [DELTA SKIP] Sell flow {cum_delta_pct:.2f} opposes LONG fade  ", end="")
                        return
                    if target_z > 0 and cum_delta_pct > CUM_DELTA_FADE_MAX:
                        # Strong buy flow while fading up to VWAP — buyers in control
                        print(f"\r  [DELTA SKIP] Buy flow {cum_delta_pct:.2f} opposes SHORT fade  ", end="")
                        return
                # Dynamic TP: buffer scales with z-score depth
                tp_buffer_ticks = max(3, int(z_abs * 2))

                if target_z < 0:
                    sl_price = round((price - sd) / TICK_SIZE) * TICK_SIZE
                    tp_price = round((target + tp_buffer_ticks * TICK_SIZE) / TICK_SIZE) * TICK_SIZE

                    print(f"\n  [BOT] ENTRY LONG @ {price:.2f} | SL: {sl_price:.2f} | TP: {tp_price:.2f} | z={abs(target_z):.1f} | sess={REGIME.get('session_type','?')} | delta={cum_delta_pct:.2f}")

                    # Check rejection cooldown
                    if hasattr(self, '_reject_cooldown') and datetime.now() < self._reject_cooldown:
                        print(f"  [COOLDOWN] Waiting after rejected order..."); return

                    self.entry_pending = True
                    self._entry_pending_ts = datetime.now()
                    try:
                        res = await self.place_order("BUY", CONTRACTS, sl_price=sl_price, tp_price=tp_price, entry_price=price)
                    finally:
                        self.entry_pending = False
                        self._entry_pending_ts = None
                    if res.get("success"):
                        comm = COMMISSION_RT * CONTRACTS
                        with BOT_STATE_LOCK:  # FIX#1: atomic entry state mutation
                            self.total_pnl -= comm
                            self.daily_pnl -= comm
                            self.pos = {"dir": "LONG", "ep": price, "sl": sl_price, "tp": tp_price, "bp": price, "bar_idx": bar_idx, "order_id": res.get("orderId"), "fill_synced": False, "created_at": datetime.now(), "uuid": _uuid.uuid4().hex, "entry_vwap": vwap, "tp_buffer_ticks": tp_buffer_ticks, "contracts_remaining": CONTRACTS, "scale1_done": False}
                            self.trades_today += 1
                            self.trade_history.append({"action": "ENTER", "dir": "LONG", "price": price, "bar_idx": bar_idx, "order_id": res.get("orderId"), "sl": sl_price, "tp": tp_price, "max_bars": TIME_STOP_MINS, "commission": comm, "z_abs": z_abs, "atr_slope": atr_slope, "sigma_exp": sigma_exp, "vol_rel": vol_rel, "vwap_slope": vwap_slope, "tod_mins": tod_mins, "atr": atr})
                        self.last_sync_ts = 0  # FIX: force fresh server sync next tick (close stale-state window)
                        print(f"  [COMM] ${comm:.2f} deducted | Daily: ${self.daily_pnl:.2f} | Total: ${self.total_pnl:.2f}")
                        return  # One trade at a time
                elif target_z > 0:
                    sl_price = round((price + sd) / TICK_SIZE) * TICK_SIZE
                    tp_price = round((target - tp_buffer_ticks * TICK_SIZE) / TICK_SIZE) * TICK_SIZE

                    print(f"\n  [BOT] ENTRY SHORT @ {price:.2f} | SL: {sl_price:.2f} | TP: {tp_price:.2f} | z={abs(target_z):.1f} | sess={REGIME.get('session_type','?')} | delta={cum_delta_pct:.2f}")

                    # Check rejection cooldown
                    if hasattr(self, '_reject_cooldown') and datetime.now() < self._reject_cooldown:
                        print(f"  [COOLDOWN] Waiting after rejected order..."); return

                    self.entry_pending = True
                    self._entry_pending_ts = datetime.now()
                    try:
                        res = await self.place_order("SELL", CONTRACTS, sl_price=sl_price, tp_price=tp_price, entry_price=price)
                    finally:
                        self.entry_pending = False
                        self._entry_pending_ts = None
                    if res.get("success"):
                        comm = COMMISSION_RT * CONTRACTS
                        with BOT_STATE_LOCK:  # FIX#1: atomic entry state mutation
                            self.total_pnl -= comm
                            self.daily_pnl -= comm
                            self.pos = {"dir": "SHORT", "ep": price, "sl": sl_price, "tp": tp_price, "bp": price, "bar_idx": bar_idx, "order_id": res.get("orderId"), "fill_synced": False, "created_at": datetime.now(), "uuid": _uuid.uuid4().hex, "entry_vwap": vwap, "tp_buffer_ticks": tp_buffer_ticks, "contracts_remaining": CONTRACTS, "scale1_done": False}
                            self.trades_today += 1
                            self.trade_history.append({"action": "ENTER", "dir": "SHORT", "price": price, "bar_idx": bar_idx, "order_id": res.get("orderId"), "sl": sl_price, "tp": tp_price, "max_bars": TIME_STOP_MINS, "commission": comm, "z_abs": z_abs, "atr_slope": atr_slope, "sigma_exp": sigma_exp, "vol_rel": vol_rel, "vwap_slope": vwap_slope, "tod_mins": tod_mins, "atr": atr})
                        self.last_sync_ts = 0  # FIX: force fresh server sync next tick (close stale-state window)
                        print(f"  [COMM] ${comm:.2f} deducted | Daily: ${self.daily_pnl:.2f} | Total: ${self.total_pnl:.2f}")
                        return  # One trade at a time

global_bot = VWAPBot()

from collections import defaultdict
def run_backtest(bars, gutter_win=True, gutter_loss=True):
    total_pnl = 0; daily_pnl = 0; trades = []; last_exit_bar = -999; trades_today = 0
    pos = None; peak_pnl = 0; max_dd = 0
    bt_peak_eod_balance = ACCOUNT_START_BALANCE  # trailing MLL peak for backtest
    cum_pv = 0; cum_v = 0; cum_p2v = 0; vp_dict = defaultdict(float)
    vp60 = defaultdict(float)
    vpoc_hist = []
    bt_bar_ranges = []; bt_atr_history = []; bt_prev_close = None
    bt_cooldown_long_until = -999   # bar_idx after which LONG entries re-allowed
    bt_cooldown_short_until = -999  # bar_idx after which SHORT entries re-allowed
    bt_session_type = "NEUTRAL"     # classified at SESSION_TYPE_BARS

    # VWAP crossings regime — updated as bars accumulate
    bt_vwap_history = []  # VWAP at each bar for crossings count

    for i in range(1, len(bars)):
        b = bars[i]; prev_b = bars[i-1]; c = b["c"]; h = b["h"]; l = b["l"]; o = b["o"]
        v = b["v"]
        
        # Calculate indicators for backtest
        tp_v = (h+l+c)/3
        cum_pv += tp_v*v; cum_v += v; cum_p2v += (tp_v**2)*v
        vwap = cum_pv/cum_v if cum_v > 0 else 0
        var = (cum_p2v/cum_v) - (vwap**2) if cum_v > 0 else 0
        std = var**0.5 if var > 0 else 0
        b["vwap"] = vwap; b["std"] = std
        bt_vwap_history.append(vwap)
        
        prev_std = bars[i-5].get("std", std) if i >= 5 else std
        sigma_exp = std / prev_std if prev_std > 0 else 1.0
        b["sigma_exp"] = sigma_exp

        # ATR tracking
        tr = max(h-l, abs(h-bt_prev_close) if bt_prev_close else h-l, abs(l-bt_prev_close) if bt_prev_close else h-l)
        bt_bar_ranges.append(tr)
        bt_prev_close = c
        lb = bt_bar_ranges[-20:] if len(bt_bar_ranges) >= 20 else bt_bar_ranges
        bt_atr_history.append(sum(lb)/len(lb) if lb else 0)
        # ATR slope
        atr_slope = 0
        if len(bt_atr_history) >= 10 and bt_atr_history[-10] > 0:
            atr_slope = (bt_atr_history[-1] - bt_atr_history[-10]) / bt_atr_history[-10]

        lo_lv = round(l*4)/4; hi_lv = round(h*4)/4
        n_levels = max(1, int((hi_lv - lo_lv)/0.25) + 1)
        vpl = v / n_levels
        lv = lo_lv
        while lv <= hi_lv:
            vp_dict[lv] += vpl
            vp60[lv] += vpl
            lv += 0.25
        vpoc = max(vp_dict, key=vp_dict.get) if vp_dict else 0
        b["vpoc"] = vpoc
        vpoc_hist.append(vpoc)

        start_idx = max(0, i - 60 + 1)
        # Drop the oldest bar from the sliding 60-bar profile
        if i >= 60:
            db = bars[i-60]
            d_lo = round(db["l"]*4)/4; d_hi = round(db["h"]*4)/4
            d_n = max(1, int((d_hi - d_lo)/0.25) + 1)
            d_vpl = db["v"] / d_n
            d_lv = d_lo
            while d_lv <= d_hi:
                vp60[d_lv] -= d_vpl
                if vp60[d_lv] < 0: vp60[d_lv] = 0  # FIX#5: clamp float imprecision
                d_lv += 0.25
                
        b["vpoc60"] = max(vp60, key=vp60.get) if vp60 else c
        vpoc60 = b["vpoc60"]

        stable = True
        if len(vpoc_hist) >= 20:
            recent_p = vpoc_hist[-20:]
            valid = [p for p in recent_p if p and p > 0]
            if len(valid) >= 2:
                migration = abs(valid[-1] - valid[0]) / len(valid)
                stable = migration <= 0.15
        
        # --- v11 ALL FEATURES FOR BACKTEST ---
        vwap_dist = c - vwap; vpoc_dist = c - vpoc
        vwap_slope = vwap - prev_b.get("vwap", vwap)
        vwap_vpoc_dist = abs(vwap - vpoc)
        mom_5 = c - bars[i-5]["c"] if i >= 5 else 0
        velocity_10 = c - bars[i-10]["c"] if i >= 10 else 0
        recent_bars = bars[i-5:i]
        avg_vol_5 = max(1.0, sum(rb["v"] for rb in recent_bars)/5)
        avg_rng_5 = max(0.25, sum(rb["h"]-rb["l"] for rb in recent_bars)/5)
        vol_rel = v/avg_vol_5; rng_rel = (h-l)/avg_rng_5
        cum_vol = sum(rb["v"] for rb in bars[:i+1])
        vol_60 = sum(rb["v"] for rb in bars[start_idx:i+1])
        s_high = max(rb["h"] for rb in bars[:i+1]); s_low = min(rb["l"] for rb in bars[:i+1])
        sess_pct = (c-s_low)/(s_high-s_low) if s_high>s_low else 0.5
        p_hist_tmp = [rb.get("vpoc", rb["c"]) for rb in bars[max(0,i-20):i+1]]
        poc_mig = abs(p_hist_tmp[-1]-p_hist_tmp[0])/20 if len(p_hist_tmp)>1 else 0
        body_size = c-o; wick_low = min(c,o)-l; wick_high = h-max(c,o)
        try:
            ts_str = b.get("ts", "2026-03-26 09:30:00")
            time_part = ts_str.split(" ")[1] if " " in ts_str else ts_str
            hh, mm, _ = map(int, time_part[:8].split(":"))
            tod_mins = (hh*60+mm) - (9*60+30)
        except (ValueError, IndexError, TypeError): tod_mins = 0
        
        target = b.get("vpoc") if TARGET_MODE == "volume" else round(b.get("vwap", 0) / TICK_SIZE) * TICK_SIZE if b.get("vwap") else 0
        target_z = (c - target)/std if std > 0.01 and target else 0
       
        if i < 15: continue
        # The following lines are replaced by the new target and target_z calculation above
        # target = vpoc if TARGET_MODE == "volume" else round(vwap/TICK_SIZE)*TICK_SIZE
        # if not target: continue
        # target_z = (c - target)/std if std >= TICK_SIZE else 0

        # Session-type heuristic at SESSION_TYPE_BARS
        if i == SESSION_TYPE_BARS and bt_atr_history:
            bt_session_type = _session_type_heuristic(bars[:i], bt_atr_history[-1])

        if pos:
            _sz = pos.get("contracts_remaining", CONTRACTS)
            epnl = 0
            ticks = (c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE
            epnl = ticks * TICK_VALUE * _sz
            cur_eq = total_pnl + epnl
            cur_dd = peak_pnl - cur_eq if cur_eq < peak_pnl else 0

            bt = i - pos["bar_idx"]
            if pos["dir"] == "LONG":
                if gutter_win:
                    gut_tp = pos["ep"] + ((GUTTER_GOAL - daily_pnl)/(_sz*TICK_VALUE))*TICK_SIZE
                    if h >= gut_tp:
                        ticks = (gut_tp - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                        trades.append({"action":"EXIT","dir":"LONG","price":gut_tp,"pnl":pnl,"bar_idx":i,"reason":"GUTTER WIN","size":_sz})
                        last_exit_bar = i; pos = None; continue
                if gutter_loss and daily_pnl > -GUTTER_DD:
                    remaining_l = (daily_pnl + GUTTER_DD)/(_sz*TICK_VALUE)
                    if remaining_l > 0:
                        gut_sl = pos["ep"] - remaining_l*TICK_SIZE
                        if l <= gut_sl:
                            ticks = (gut_sl - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                            trades.append({"action":"EXIT","dir":"LONG","price":gut_sl,"pnl":pnl,"bar_idx":i,"reason":"GUTTER LOSS","size":_sz})
                            last_exit_bar = i; pos = None; continue
                if l <= pos["sl"]:
                    sl_fill = pos["sl"] - TICK_SIZE  # FIX#4: 1-tick slippage on stop fills
                    ticks = (sl_fill - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                    sr = classify_stop_reason("LONG", pos.get("ep"), pos.get("sl"))
                    trades.append({"action":"EXIT","dir":"LONG","price":sl_fill,"pnl":pnl,"bar_idx":i,"reason":sr,"size":_sz})
                    if sr in ("STOP LOSS", "TRAIL STOP"): bt_cooldown_long_until = i + DIR_COOLDOWN_BARS
                    last_exit_bar = i; pos = None; continue
                if h > pos["bp"]: pos["bp"] = h
                ur = (pos["bp"] - pos["ep"])/TICK_SIZE; cur = (c - pos["ep"])/TICK_SIZE

                # --- Partial scale-out at VWAP target ---
                if (SCALE_OUT_ENABLED and _sz > 1 and not pos.get("scale1_done")
                        and pos.get("tp") and h >= pos["tp"] and cur >= EXIT_MIN_TICKS):
                    so_pnl = cur * TICK_VALUE * 1; total_pnl += so_pnl; daily_pnl += so_pnl
                    trades.append({"action":"EXIT","dir":"LONG","price":c,"pnl":so_pnl,"bar_idx":i,"reason":"SCALE OUT","size":1})
                    pos["contracts_remaining"] = _sz - 1
                    pos["scale1_done"] = True
                    if pos["sl"] < pos["ep"]: pos["sl"] = pos["ep"]
                    _sz = pos["contracts_remaining"]

                # --- v10 True Mean Exit (full position) ---
                if pos.get("tp") and c >= pos["tp"] and cur >= EXIT_MIN_TICKS:
                    ticks = (c - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"LONG","price":c,"pnl":pnl,"bar_idx":i,"reason":"TARGET","size":_sz})
                    last_exit_bar = i; pos = None; continue
                if ur >= TRAIL_ACTIVATE:
                    tl = round((pos["bp"] - TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl > pos["sl"]: pos["sl"] = tl
                if ur >= BREAKEVEN_TICKS and pos["sl"] < pos["ep"]: pos["sl"] = pos["ep"]
                # Dynamic time stop: early exit at half-time if losing, normal at full
                early_stop = int(TIME_STOP_MINS * 0.5)
                if bt > TIME_STOP_MINS or (bt > early_stop and cur < -4):
                    ticks = (c - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"LONG","price":c,"pnl":pnl,"bar_idx":i,"reason":"TIME STOP","size":_sz})
                    last_exit_bar = i; pos = None; continue
            else:
                if gutter_win:
                    gut_tp = pos["ep"] - ((GUTTER_GOAL - daily_pnl)/(_sz*TICK_VALUE))*TICK_SIZE
                    if l <= gut_tp:
                        ticks = (pos["ep"] - gut_tp)/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                        trades.append({"action":"EXIT","dir":"SHORT","price":gut_tp,"pnl":pnl,"bar_idx":i,"reason":"GUTTER WIN","size":_sz})
                        last_exit_bar = i; pos = None; continue
                if gutter_loss and daily_pnl > -GUTTER_DD:
                    remaining_l = (daily_pnl + GUTTER_DD)/(_sz*TICK_VALUE)
                    if remaining_l > 0:
                        gut_sl = pos["ep"] + remaining_l*TICK_SIZE
                        if h >= gut_sl:
                            ticks = (pos["ep"] - gut_sl)/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                            trades.append({"action":"EXIT","dir":"SHORT","price":gut_sl,"pnl":pnl,"bar_idx":i,"reason":"GUTTER LOSS","size":_sz})
                            last_exit_bar = i; pos = None; continue
                if h >= pos["sl"]:
                    sl_fill = pos["sl"] + TICK_SIZE  # FIX#4: 1-tick slippage on stop fills
                    ticks = (pos["ep"] - sl_fill)/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                    sr = classify_stop_reason("SHORT", pos.get("ep"), pos.get("sl"))
                    trades.append({"action":"EXIT","dir":"SHORT","price":sl_fill,"pnl":pnl,"bar_idx":i,"reason":sr,"size":_sz})
                    if sr in ("STOP LOSS", "TRAIL STOP"): bt_cooldown_short_until = i + DIR_COOLDOWN_BARS
                    last_exit_bar = i; pos = None; continue
                if l < pos["bp"]: pos["bp"] = l
                ur = (pos["ep"] - pos["bp"])/TICK_SIZE; cur = (pos["ep"] - c)/TICK_SIZE

                # --- Partial scale-out at VWAP target ---
                if (SCALE_OUT_ENABLED and _sz > 1 and not pos.get("scale1_done")
                        and pos.get("tp") and l <= pos["tp"] and cur >= EXIT_MIN_TICKS):
                    so_pnl = cur * TICK_VALUE * 1; total_pnl += so_pnl; daily_pnl += so_pnl
                    trades.append({"action":"EXIT","dir":"SHORT","price":c,"pnl":so_pnl,"bar_idx":i,"reason":"SCALE OUT","size":1})
                    pos["contracts_remaining"] = _sz - 1
                    pos["scale1_done"] = True
                    if pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                    _sz = pos["contracts_remaining"]

                # --- v10 True Mean Exit (full position) ---
                if pos.get("tp") and c <= pos["tp"] and cur >= EXIT_MIN_TICKS:
                    ticks = (pos["ep"] - c)/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"SHORT","price":c,"pnl":pnl,"bar_idx":i,"reason":"TARGET","size":_sz})
                    last_exit_bar = i; pos = None; continue
                if ur >= TRAIL_ACTIVATE:
                    tl = round((pos["bp"] + TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl < pos["sl"]: pos["sl"] = tl
                if ur >= BREAKEVEN_TICKS and pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                # Dynamic time stop
                early_stop = int(TIME_STOP_MINS * 0.5)
                if bt > TIME_STOP_MINS or (bt > early_stop and cur < -4):
                    ticks = (pos["ep"] - c)/TICK_SIZE; pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"SHORT","price":c,"pnl":pnl,"bar_idx":i,"reason":"TIME STOP","size":_sz})
                    last_exit_bar = i; pos = None; continue
        else:
            if not BOT_ACTIVE or i < 45 or trades_today >= MAX_TRADES: continue
            # Time-of-day filters
            if tod_mins >= POWER_HOUR_START: continue
            if gutter_win and round(daily_pnl, 2) >= GUTTER_GOAL: continue
            if gutter_loss and round(daily_pnl, 2) <= -GUTTER_DD: continue
            if not stable: continue

            # Z-threshold: crossings-based trending regime only (matches optimizer)
            bt_crossings = count_vwap_crossings(bt_vwap_history[-20:], [bars[j]["c"] for j in range(max(0,i-20), i+1)], update_regime=False)
            bt_effective_z = Z_THRESH * 1.3 if bt_crossings <= VWAP_CROSS_TRENDING else Z_THRESH
            if abs(target_z) >= bt_effective_z:
                # Direction cooldown check
                if target_z < 0 and i < bt_cooldown_long_until: continue
                if target_z > 0 and i < bt_cooldown_short_until: continue

                dist = abs(c - target)
                # ATR-based stop sizing
                if bt_atr_history:
                    atr_ticks = max(1, int(bt_atr_history[-1] / TICK_SIZE))
                    sd = max(MIN_STOP_TICKS, min(MAX_STOP_TICKS, int(atr_ticks * ATR_STOP_RATIO))) * TICK_SIZE
                else:
                    sd = max(min(dist * STOP_RATIO, MAX_STOP_TICKS * TICK_SIZE), MIN_STOP_TICKS * TICK_SIZE)

                # --- MICRO-STRUCTURE ---
                if sigma_exp > 1.25: continue  # Sigma panic — A/B validated
                if atr_slope > 0.10: continue  # ATR slope — A/B validated, +$8.44/trade
                # VWAP slope filter
                if target_z < 0 and vwap_slope < -VWAP_SLOPE_THRESH: continue
                if target_z > 0 and vwap_slope > VWAP_SLOPE_THRESH: continue
                # Volume confirmation
                if vol_rel < MIN_VOL_REL: continue
                # (Spread filter skipped in backtest — no bid/ask in bar data)

                # Dynamic TP buffer scales with z-score depth
                tp_buffer_ticks = max(3, int(abs(target_z) * 2))
                z_abs = abs(target_z)

                if target_z < 0:
                    sl = round((c - sd)/TICK_SIZE)*TICK_SIZE
                    tp = target + tp_buffer_ticks * TICK_SIZE
                    pos = {"dir": "LONG", "ep": c, "sl": sl, "tp": tp, "bp": c, "bar_idx": i, "contracts_remaining": CONTRACTS, "scale1_done": False}
                    trades.append({"action": "ENTER", "dir": "LONG", "price": c, "bar_idx": i, "sl": sl, "tp": tp, "max_bars": TIME_STOP_MINS, "z_abs": z_abs, "atr_slope": atr_slope, "sigma_exp": sigma_exp, "vol_rel": vol_rel, "vwap_slope": vwap_slope, "tod_mins": tod_mins, "atr": bt_atr_history[-1] if bt_atr_history else 0})
                    trades_today += 1
                    comm = COMMISSION_RT * CONTRACTS; total_pnl -= comm; daily_pnl -= comm
                elif target_z > 0:
                    sl = round((c + sd)/TICK_SIZE)*TICK_SIZE
                    tp = target - tp_buffer_ticks * TICK_SIZE
                    pos = {"dir": "SHORT", "ep": c, "sl": sl, "tp": tp, "bp": c, "bar_idx": i, "contracts_remaining": CONTRACTS, "scale1_done": False}
                    trades.append({"action": "ENTER", "dir": "SHORT", "price": c, "bar_idx": i, "sl": sl, "tp": tp, "max_bars": TIME_STOP_MINS, "z_abs": z_abs, "atr_slope": atr_slope, "sigma_exp": sigma_exp, "vol_rel": vol_rel, "vwap_slope": vwap_slope, "tod_mins": tod_mins, "atr": bt_atr_history[-1] if bt_atr_history else 0})
                    trades_today += 1
                    comm = COMMISSION_RT * CONTRACTS; total_pnl -= comm; daily_pnl -= comm

        cur_eq = total_pnl
        if pos:
            _sz_eq = pos.get("contracts_remaining", CONTRACTS)
            cur_ticks = (c - pos["ep"])/TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c)/TICK_SIZE
            cur_eq += cur_ticks * TICK_VALUE * _sz_eq
        if cur_eq > peak_pnl: peak_pnl = cur_eq
        if peak_pnl - cur_eq > max_dd: max_dd = peak_pnl - cur_eq

    if pos:
        c = bars[-1]["c"]
        _sz = pos.get("contracts_remaining", CONTRACTS)
        ticks = (c - pos["ep"])/TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c)/TICK_SIZE
        pnl = ticks*TICK_VALUE*_sz; total_pnl += pnl
        trades.append({"action":"EXIT","dir":pos["dir"],"price":c,"pnl":pnl,"bar_idx":len(bars)-1,"reason":"END OF DATA","size":_sz})

    for t in trades:
        if t["action"] == "EXIT":
            t["pnl"] -= COMMISSION_RT * t.get("size", CONTRACTS)

    return {"total_pnl": total_pnl, "daily_pnl": total_pnl, "trades": trades_today, "max_trades": MAX_TRADES, "pos": None, "open_pnl": 0, "max_dd": max_dd, "gutter_win": gutter_win, "gutter_loss": gutter_loss}, trades

_STATS_CACHE = {"key": None, "data": None}
_COMBINED_EXITS_CACHE = {"key": None, "trades": [], "ts": 0.0}

def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    p = max(0.0, min(1.0, float(p)))
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    idx = (n - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_vals[lo])
    w = idx - lo
    return float(sorted_vals[lo] * (1.0 - w) + sorted_vals[hi] * w)

def build_bot_stats(bot_trades, max_trades=MAX_TRADES, n_sims=1000, max_days=45):
    # Group trades accurately into Daily historical boundaries using indexing
    days_dict = {}
    trade_pnls = []
    for t in bot_trades or []:
        if t.get("action") != "EXIT": continue
        p = t.get("pnl")
        if isinstance(p, (int, float)):
            trade_pnls.append(float(p))
            day_group = t.get("date", "0")
            if day_group not in days_dict: days_dict[day_group] = 0.0
            days_dict[day_group] += float(p)
            
    exits = list(days_dict.values())

    if len(exits) < 5:
        return {"ready": False, "trades": len(trade_pnls)}

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    flats = [p for p in trade_pnls if p == 0]
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    rr = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        rr = avg_win / abs(avg_loss)
    expectancy = sum(trade_pnls) / len(trade_pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0: profit_factor = gross_win / gross_loss
    elif gross_win > 0: profit_factor = None
    else: profit_factor = 0.0

    max_steps = int(max_days)
    key = (
        len(trade_pnls),
        round(sum(exits), 2),
        round(sum(abs(p) for p in exits), 2),
        round(sum(p * p for p in exits), 2),
        len(wins),
        len(losses),
        int(n_sims),
        int(max_days),
    )
    if _STATS_CACHE["key"] == key and _STATS_CACHE["data"] is not None:
        return _STATS_CACHE["data"]

    rng = random.Random(42 + len(exits))
    outcomes = {"PASSED": 0, "BLOWN": 0, "TIMEOUT": 0}
    pass_steps = []
    blow_steps = []
    stride = max(1, max_steps // 180)
    x_axis = list(range(0, max_steps + 1, stride))
    if not x_axis or x_axis[-1] != max_steps:
        x_axis.append(max_steps)
    cols = [[] for _ in range(len(x_axis))]
    preview_paths = []
    preview_limit = 60

    for _ in range(int(n_sims)):
        pnl = 0.0
        peak = 0.0
        path = [ACCOUNT_START_BALANCE]
        outcome = "TIMEOUT"
        end_step = max_steps
        journey_pnls = []

        for step in range(1, max_steps + 1):
            day_pnl = rng.choice(exits)
            
            # Simulated history daily rules caps
            if day_pnl < -GUTTER_DD: day_pnl = -GUTTER_DD
            elif day_pnl > GUTTER_GOAL: day_pnl = GUTTER_GOAL
                
            pnl += day_pnl
            journey_pnls.append(day_pnl)
            
            peak = max(peak, pnl)
            dd_floor = peak - TRAILING_MLL_DISTANCE
            bal = ACCOUNT_START_BALANCE + pnl
            path.append(bal)

            if pnl <= dd_floor:
                outcome = "BLOWN"
                end_step = step
                blow_steps.append(step)
                break
                
            if pnl >= PROFIT_TARGET:
                best_day = max(journey_pnls)
                if best_day < pnl * 0.5:
                    outcome = "PASSED"
                    end_step = step
                    pass_steps.append(step)
                    break

        outcomes[outcome] += 1
        if len(path) < max_steps + 1:
            path.extend([path[-1]] * (max_steps + 1 - len(path)))
        sampled = [path[i] for i in x_axis]
        if len(preview_paths) < preview_limit:
            preview_paths.append(sampled)
        for i, v in enumerate(sampled):
            cols[i].append(v)

    p5 = []
    p50 = []
    p95 = []
    for c in cols:
        c.sort()
        p5.append(_pct(c, 0.05))
        p50.append(_pct(c, 0.50))
        p95.append(_pct(c, 0.95))

    n = float(n_sims)
    stats = {
        "ready": True,
        "trades": len(trade_pnls),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": (len(wins) / (len(wins) + len(losses))) if (len(wins) + len(losses)) else 0.0,
        "win_rate_nonflat": (len(wins) / (len(wins) + len(losses))) if (len(wins) + len(losses)) else 0.0,
        "win_rate_all": (len(wins) / len(trade_pnls)) if trade_pnls else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_loss_abs": abs(avg_loss) if avg_loss is not None else None,
        "rr": rr,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "pass_rate": outcomes["PASSED"] / n,
        "blow_rate": outcomes["BLOWN"] / n,
        "timeout_rate": outcomes["TIMEOUT"] / n,
        "avg_trades_to_pass": 0.0,
        "avg_trades_to_blow": 0.0,
        "avg_days_to_pass": (sum(pass_steps) / len(pass_steps)) if pass_steps else 0.0,
        "avg_days_to_blow": (sum(blow_steps) / len(blow_steps)) if blow_steps else 0.0,
        "simulations": int(n_sims),
        "sample_days": len(exits),
        "lookback_days": STATS_LOOKBACK_DAYS,
        "max_days": int(max_days),
        "max_trades_day": max_trades,
        "start_balance": ACCOUNT_START_BALANCE,
        "pass_balance": ACCOUNT_START_BALANCE + PROFIT_TARGET,
        "trailing_mll": TRAILING_MLL_DISTANCE,
        "profit_target": PROFIT_TARGET,
        "mll_floor": getattr(global_bot, 'mll_floor', ACCOUNT_START_BALANCE - TRAILING_MLL_DISTANCE),
        "account_balance": getattr(global_bot, 'account_balance_with_open', ACCOUNT_START_BALANCE),
        "mll_remaining": getattr(global_bot, 'mll_remaining', TRAILING_MLL_DISTANCE),
        "peak_eod_balance": getattr(global_bot, 'peak_eod_balance', ACCOUNT_START_BALANCE),
        "x_axis": x_axis,
        "paths_preview": preview_paths,
        "curve": {"p5": p5, "p50": p50, "p95": p95},
    }
    _STATS_CACHE["key"] = key
    _STATS_CACHE["data"] = stats
    return stats

def get_combined_exit_trades(gutter_win=True, gutter_loss=True, min_refresh_sec=60, lookback_days=STATS_LOOKBACK_DAYS):
    now_ts = time.time()
    if _COMBINED_EXITS_CACHE.get("trades") and (now_ts - _COMBINED_EXITS_CACHE.get("ts", 0.0) < float(min_refresh_sec)):
        return list(_COMBINED_EXITS_CACHE["trades"])

    sessions = list_sessions()
    # Filter to most recent N days so MC reflects current params, not stale history
    if lookback_days and lookback_days > 0:
        sessions = sessions[:lookback_days]

    sig = []
    for ds in sessions:
        p = os.path.join(DATA_DIR, f"{ds}.json")
        try:
            sig.append((ds, int(os.path.getmtime(p))))
        except OSError:
            sig.append((ds, 0))

    key = (tuple(sig), bool(gutter_win), bool(gutter_loss), TARGET_MODE)
    if _COMBINED_EXITS_CACHE["key"] == key and _COMBINED_EXITS_CACHE.get("trades"):
        _COMBINED_EXITS_CACHE["ts"] = now_ts
        return list(_COMBINED_EXITS_CACHE["trades"])

    combined = []
    for ds in sessions:
        s = Session.load(ds)
        if not s or len(s.bars) <= 15:
            continue
        try:
            _, day_trades = run_backtest(s.bars, gutter_win=gutter_win, gutter_loss=gutter_loss)
            for t in day_trades:
                if t.get("action") == "EXIT" and isinstance(t.get("pnl"), (int, float)):
                    combined.append({"action": "EXIT", "pnl": float(t["pnl"]), "date": ds})
        except Exception as e:
            print(f"  [BT ERROR] {ds}: {e}")
            continue

    _COMBINED_EXITS_CACHE["key"] = key
    _COMBINED_EXITS_CACHE["trades"] = combined
    _COMBINED_EXITS_CACHE["ts"] = now_ts
    return list(combined)

# ============================================================
# AUTH + STREAM + SYNC FETCH
# ============================================================
import urllib.request
def fetch_history_sync(token, d_str):
    url = f"{API}/History/retrieveBars"
    try:
        day = datetime.strptime(d_str, "%Y-%m-%d").date()
        rth_start = ET.localize(datetime.combine(day, dtime(9,30)))
        rth_end = ET.localize(datetime.combine(day, dtime(16,0)))
        start_utc = rth_start.astimezone(timezone.utc)
        end_utc = rth_end.astimezone(timezone.utc)
        
        def _get(cid):
            payload = json.dumps({"contractId": cid, "live": False, "startTime": start_utc.isoformat(), "endTime": end_utc.isoformat(), "unit": 2, "unitNumber": 1, "limit": 500, "includePartialBar": False}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode("utf-8")).get("bars") or []

        bars = _get(CONTRACT)
        if not bars:
            # Fallback to previous ES contract month for historical data
            bars = _get("CON.F.US.EP.H26")

        bars = [b for b in bars if is_rth_bar_ts(b.get("t"))]

        if not bars: return None
        bars.reverse()
        s = Session(d_str)
        for b in bars:
            s.add_bar(b["o"], b["h"], b["l"], b["c"], b["v"], b["t"])
        return s
    except Exception as e:
        print(f"\n  Sync fetch error for {d_str}: {e}")
        return None

def auth_sync():
    """Synchronous auth for backtest scripts."""
    u = os.environ.get("PROJECT_X_USERNAME", ""); k = os.environ.get("PROJECT_X_API_KEY", "")
    if not u or not k: print("  Set env vars"); return None
    payload = json.dumps({"userName": u, "apiKey": k}).encode("utf-8")
    req = urllib.request.Request(f"{API}/Auth/loginKey", data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        d = json.loads(response.read().decode("utf-8"))
        if d.get("success") and d.get("token"): return d["token"]
        print(f"  Auth failed: {d}"); return None

async def auth():
    u=os.environ.get("PROJECT_X_USERNAME","");k=os.environ.get("PROJECT_X_API_KEY","")
    if not u or not k:
        print("  [ERROR] PROJECT_X_USERNAME or PROJECT_X_API_KEY NOT FOUND.")
        print("          Check your .env file or export them manually.")
        return None
    async with httpx.AsyncClient(timeout=30) as h:
        r=await h.post(f"{API}/Auth/loginKey",json={"userName":u,"apiKey":k})
        d=r.json()
        if d.get("success") and d.get("token"):return d["token"]
        print(f"  Auth failed: {d}")
        if d.get("errorCode") == 3:
            print("  TIP: ErrorCode 3 usually means invalid credentials.")
            if "@" not in u:
                print(f"  WARNING: Username '{u}' does not look like an email. TopStepX usually requires an email.")
        return None

async def stream(token):
    global connected
    _current_token = token
    while True:
        try:
            if getattr(global_bot, "token", None):
                await global_bot.refresh_token_if_needed()
                _current_token = global_bot.token

            async with httpx.AsyncClient(timeout=30) as h:
                r=await h.post(f"{HUB}/negotiate?negotiateVersion=1&access_token={_current_token}")
                if r.status_code != 200:
                    print(f"\n  [NET] Negotiate failed ({r.status_code}). Retrying in 10s...")
                    await asyncio.sleep(10)
                    continue
                ct=r.json().get("connectionToken",r.json().get("connectionId",""))
            
            ws_url=HUB.replace("https://","wss://")+f"?id={ct}&access_token={_current_token}"
            async for ws in websockets.connect(ws_url,additional_headers={"Authorization":f"Bearer {_current_token}"},ping_interval=15,ping_timeout=30):
                try:
                    await ws.send(json.dumps({"protocol":"json","version":1})+"\x1e")
                    await asyncio.wait_for(ws.recv(),timeout=10)
                    with LOCK:connected=True
                    
                    print("  Connected! Fetching missed bars...")
                    await backfill(_current_token)

                    await ws.send(json.dumps({"type":1,"target":"SubscribeContractQuotes","arguments":[CONTRACT],"invocationId":"1"})+"\x1e")
                    await ws.send(json.dumps({"type":1,"target":"SubscribeContractTrades","arguments":[CONTRACT],"invocationId":"2"})+"\x1e")
                    print("  ✓ Live stream active\n")
                    
                    async for raw in ws:
                        for chunk in raw.split("\x1e"):
                            chunk=chunk.strip()
                            if not chunk:continue
                            try: await on_msg(json.loads(chunk))
                            except json.JSONDecodeError:pass
                            except Exception as e:
                                with LOCK:errors.append(str(e));errors[:]=errors[-10:]
                except websockets.ConnectionClosed:
                    print("\n  [WS] Connection closed. Reconnecting...");connected=False
                    if getattr(global_bot, "token", None):
                        await global_bot.refresh_token_if_needed()
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"\n  [WS] Inner Error: {e}");connected=False
                    await asyncio.sleep(3)
                    break  # Break async for to trigger full re-negotiate
        except Exception as e:
            connected = False
            print(f"\n  [NET] Connection lost or failed: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

def is_rth():
    """True if current time is within NYSE RTH: 9:30 AM - 4:00 PM ET."""
    now_et=datetime.now(ET)
    t=now_et.time()
    return dtime(9,30)<=t<dtime(16,0) and now_et.weekday()<5

_otc=0  # outside-RTH tick counter
async def on_msg(msg):
    global _otc
    if msg.get("type")==6:return
    t=msg.get("target","");a=msg.get("arguments",[])
    if not a:return
    rth=is_rth()
    if not rth:
        _otc+=1
        if _otc%500==1:
            now_et=datetime.now(ET)
            print(f"\r  Outside RTH ({now_et.strftime('%H:%M ET')}) — waiting for 9:30 AM ET  ",end="",flush=True)
    if t=="GatewayTrade": await on_trade(a,rth)
    elif t=="GatewayQuote": await on_quote(a,rth)

async def on_trade(args,rth=True):
    global live_last
    with LOCK:raw_log.append(args);raw_log[:]=raw_log[-3:]
    dicts=[]
    for item in args:
        if isinstance(item,dict):dicts.append(item)
        elif isinstance(item,list):
            for sub in item:
                if isinstance(sub,dict):dicts.append(sub)
    now=datetime.now(timezone.utc)
    if not rth:
        with LOCK:  # BUG2: update live_last inside lock
            for td in dicts:
                p=td.get("price")
                if p: live_last=p
        return
    with LOCK:
        for td in dicts:
            p=td.get("price")
            if p: live_last=p  # BUG2: inside lock
        sess=get_or_create_session()
        for td in dicts:
            price=td.get("price",0);vol=td.get("volume",0);sr=td.get("side","")
            if not price or price<=0:continue
            if vol<=0:vol=1
            if isinstance(sr,int):s="BUY" if sr==1 else("SELL" if sr==2 else str(sr))
            elif isinstance(sr,str):s=sr.upper();s="BUY" if s in("B","BUY")else("SELL" if s in("S","SELL","A")else s)
            else:s=str(sr)
            sess.add_trade(price,vol,s,now)
            tick_recorder.record_trade(price, vol, s, now)  # record every tick
        cz = round((live_last - sess.vwap)/sess.vwap_std, 2) if sess.vwap_std > 0 else 0
        stable = sess.is_session_stable()
        bar_idx = len(sess.bars) + (1 if sess.cur_bar else 0)  # BUG5: count forming bar
        # Indicators updated in Session.add_trade
        snap_price = live_last; snap_vwap = sess.vwap; snap_vpoc = sess.vpoc
        snap_std = sess.vwap_std
        
        # Raw Distances (Anti Z-Score Paradox)
        vwap_dist = snap_price - snap_vwap
        vpoc_dist = snap_price - snap_vpoc
        
        # Slopes
        vwap_slope = snap_vwap - sess.bars[-1].get("vwap", snap_vwap) if sess.bars else 0.0
        
        # Feature inputs for entry checks:
        now_et = datetime.now(ET)
        tod_mins = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
        
        c_b = sess.cur_bar if sess.cur_bar else (sess.bars[-1] if sess.bars else None)
        vol = float(c_b["v"]) if c_b else 0.0
        bar_range = float(c_b["h"] - c_b["l"]) if c_b else 0.0
        
        # Momentum (5-bar delta) and Velocity (10-bar delta)
        mom_5 = snap_price - sess.bars[-5]["c"] if len(sess.bars) >= 5 else 0.0
        velocity_10 = snap_price - sess.bars[-10]["c"] if len(sess.bars) >= 10 else 0.0
        
        # Relative Volatility (last 5 bars)
        recent = sess.bars[-5:]
        av_v = sum(rb["v"] for rb in recent)/5 if recent else 1.0
        av_r = sum(rb["h"]-rb["l"] for rb in recent)/5 if recent else 1.0
        vol_rel = vol / av_v if av_v > 0 else 1.0
        rng_rel = bar_range / av_r if av_r > 0 else 1.0
        
        # Cumulative and Hourly Vol
        cum_vol = sess.total_volume
        vol_60 = sum(rb["v"] for rb in sess.bars[-60:]) if sess.bars else vol
        
        # Anti-Blowout: Session Percentile and POC Migration
        s_high = max([rb["h"] for rb in sess.bars] + [snap_price]) if sess.bars else snap_price
        s_low = min([rb["l"] for rb in sess.bars] + [snap_price]) if sess.bars else snap_price
        sess_pct = (snap_price - s_low) / (s_high - s_low) if s_high > s_low else 0.5
        
        p_hist = [rb.get("vpoc", snap_price) for rb in sess.bars[-20:]] + [snap_vpoc]
        poc_mig = abs(p_hist[-1] - p_hist[0]) / 20 if len(p_hist) > 1 else 0.0

        # Convergence
        vwap_vpoc_dist = abs(snap_vwap - snap_vpoc)
        
        # Candle Shape (v6)
        body_size = live_last - (sess.bars[-1]["o"] if sess.bars else live_last)
        wick_low = min(live_last, (sess.bars[-1]["o"] if sess.bars else live_last)) - (sess.bars[-1]["l"] if sess.bars else live_last)
        wick_high = (sess.bars[-1]["h"] if sess.bars else live_last) - max(live_last, (sess.bars[-1]["o"] if sess.bars else live_last))

        # Z-Scoresulate Rolling 1-Hour VPOC
        start_idx = max(0, len(sess.bars) - 60 + 1)
        vp60_dict = defaultdict(float)
        lookback_bars = sess.bars[start_idx:]
        if sess.cur_bar: lookback_bars.append(sess.cur_bar)
        
        for wb in lookback_bars:
            lo_lv = round(wb["l"]*4)/4; hi_lv = round(wb["h"]*4)/4
            n_levels = max(1, int((hi_lv - lo_lv)/0.25) + 1)
            vpl = wb["v"] / n_levels
            lv = lo_lv
            while lv <= hi_lv: vp60_dict[lv] += vpl; lv += 0.25
        snap_vpoc60 = max(vp60_dict, key=vp60_dict.get) if vp60_dict else snap_price

        # Sigma expansion (v7 panic filter)
        if len(sess.std_history) >= 5 and sess.std_history[-5] > 0:
            sigma_exp = snap_std / sess.std_history[-5]
        else:
            sigma_exp = 1.0
        
        # Previous bar H/L for HH/LL confirmation
        prev_h = sess.bars[-1]["h"] if sess.bars else snap_price
        prev_l = sess.bars[-1]["l"] if sess.bars else snap_price

        # ATR slope — A/B tested, +$8.44/trade
        atr_slope = sess.get_atr_slope(lookback=10)
        # Current ATR value (for adaptive stop sizing)
        snap_atr = sess.atr_history[-1] if sess.atr_history else 0.0
        # Live bid-ask spread
        snap_spread = (sess.ask - sess.bid) if sess.ask > 0 and sess.bid > 0 else 0.0

        # FIX: Read-and-clear bar-close event inside the lock so each bar fires
        # exactly one entry evaluation, matching the backtester's per-bar sampling.
        bar_close_event = sess.bar_just_closed
        sess.bar_just_closed = False

        # Update VWAP crossings regime every bar (last 20 bars — matches backtest lookback)
        if bar_close_event and len(sess.bars) >= 2:
            count_vwap_crossings(sess.vwap_history[-20:], [b["c"] for b in sess.bars[-20:]])
            # Classify session type after SESSION_TYPE_BARS
            maybe_classify_session(sess, bar_idx)

        # Cumulative delta fraction (order flow imbalance)
        snap_cum_delta_pct = sess.cum_delta / max(sess.total_volume, 1)

    async with BOT_LOCK:
        await global_bot.sync_from_server()
        await global_bot.check_logic(snap_price, snap_vwap, snap_vpoc, snap_std, bar_idx, rth, stable, snap_vpoc60, vol, bar_range, tod_mins, vwap_dist, vpoc_dist, vwap_slope, vwap_vpoc_dist, mom_5, velocity_10, vol_rel, rng_rel, cum_vol, vol_60, poc_mig, sess_pct, body_size, wick_low, wick_high, sigma_exp, prev_h, prev_l, atr_slope, snap_atr, snap_spread, bar_close_event, cum_delta_pct=snap_cum_delta_pct)

async def on_quote(args,rth=True):
    global live_bid,live_ask,live_last
    now=datetime.now(timezone.utc)
    with LOCK:
        for item in args:
            if isinstance(item,dict):
                b=item.get("bestBid");a=item.get("bestAsk")
                if b:live_bid=b
                if a:live_ask=a
                if live_bid and live_ask:live_last=(live_bid+live_ask)/2
        if not rth:return
        sess=get_or_create_session()
        for item in args:
            if isinstance(item,dict):
                sess.add_quote(item.get("bestBid"),item.get("bestAsk"),now)
                # Record quotes throttled (1/sec) — trades are full fidelity
                if live_bid and live_ask:
                    tick_recorder.record_quote(live_bid, live_ask, now)
        print(f"\r  {sess.bid:>8.2f}/{sess.ask:>8.2f} Last:{sess.last:>8.2f} Bars:{len(sess.bars)} Tr:{sess.trade_count} V:{sess.total_volume}  ",end="",flush=True)

# ============================================================
# HTML — same static layout as v5/v6
# ============================================================
HTML=r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>ES</title>
<style>
:root{--bg:#eef2f6;--panel:#ffffff;--panel2:#fbfcfd;--line:#d6e0ea;--text:#1f2d3d;--muted:#5f7085;--accent:#2f6bff;--good:#00a66a;--bad:#d64545;--warn:#b07a00;}
body{background:var(--bg);color:var(--text);font:12px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif;padding:12px;margin:0;letter-spacing:.1px;}
table{border-collapse:collapse;width:100%;margin:6px 0;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden;}
td,th{padding:6px 8px;text-align:left;border-bottom:1px solid #e8edf3;}
th{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.7px;font-weight:600;background:#f6f8fb;} 
.g{color:var(--good);}.r{color:var(--bad);}.b{color:var(--accent);}.d{color:var(--muted);}.y{color:var(--warn);}
h3{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.8px;margin:14px 0 4px;border-bottom:1px solid var(--line);padding-bottom:4px;}
pre{background:var(--panel);padding:8px;border:1px solid var(--line);font-size:10px;margin:6px 0;border-radius:8px;white-space:pre-wrap;color:#5f7085;}
.cbox{background:var(--panel);border:1px solid var(--line);margin:6px 0;border-radius:8px;overflow:hidden;}.cbox canvas{display:block;}
.tw{display:flex;margin:6px 0;}.tw .cbox{flex:1;min-width:0;}.tw .pbox{width:160px;background:var(--panel);border:1px solid var(--line);border-left:none;border-radius:0 8px 8px 0;overflow:hidden;}.tw .pbox canvas{display:block;}
.vb{display:inline-block;height:10px;background:var(--accent);margin-left:6px;vertical-align:middle;border-radius:3px;}
.nav-day{display:flex;align-items:center;gap:8px;margin:8px 0;padding:8px;background:var(--panel);border:1px solid var(--line);border-radius:8px;flex-wrap:wrap;}
.nav-day button{background:#f7f9fc;color:var(--text);border:1px solid #d3dde8;padding:5px 12px;cursor:pointer;font:600 12px/1.1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif;border-radius:6px;}
.nav-day button:hover{background:#eef3f9;}.nav-day button.on{background:var(--accent);border-color:var(--accent);color:#fff;}
.nav-day .dt{font-size:14px;font-weight:700;min-width:120px;text-align:center;letter-spacing:.4px;}
.nav-day .days{display:flex;gap:4px;margin-left:auto;}
.nav-day .dbtn{padding:3px 8px;font-size:10px;background:#f7f9fc;border:1px solid #d3dde8;color:var(--muted);cursor:pointer;border-radius:4px;}
.nav-day .dbtn:hover,.nav-day .dbtn.on{color:#fff;border-color:var(--accent);}
#status,#live247,#bot-pl,#orders-section{background:var(--panel);border:1px solid var(--line);border-radius:8px;}
#status{padding:6px 8px;margin-bottom:6px;}
#live247,#bot-pl{margin-bottom:6px;padding:6px 8px;font-size:12px;display:flex;gap:10px;flex-wrap:wrap;}
#orders-section{margin-bottom:6px;padding:6px;}
#v-last{font-size:16px;font-weight:700;letter-spacing:.1px;}
#cal{background:#f7f9fc;color:var(--text);border:1px solid #d3dde8;padding:4px 6px;border-radius:6px;font-family:inherit;font-size:11px;}
.page{display:flex;flex-direction:column;gap:8px;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px;}
.card h3{margin:0 0 6px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.65px;border-bottom:1px solid var(--line);padding-bottom:5px;}
.chart-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px;}
.chart-head h3{margin:0;border-bottom:none;padding-bottom:0;}
.chart-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.chart-tools button{background:#f7f9fc;color:var(--text);border:1px solid #d3dde8;padding:3px 8px;cursor:pointer;font:600 11px/1.1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif;border-radius:5px;}
.chart-tools button:hover{background:#eef3f9;}
.legend{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.legend .item{font-size:10px;color:var(--muted);display:flex;align-items:center;gap:4px;}
.legend .dot{width:10px;height:2px;border-radius:2px;display:inline-block;}
.summary-line{font-weight:600;color:var(--text);padding:2px 0 0 0;}
.tabs{display:flex;gap:6px;margin-top:8px;}
.tabs button{background:#f7f9fc;color:var(--text);border:1px solid #d3dde8;padding:5px 12px;cursor:pointer;font:600 11px/1.1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif;border-radius:6px;}
.tabs button:hover{background:#eef3f9;}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff;}
.kpi{display:grid;grid-template-columns:repeat(6,minmax(80px,1fr));gap:6px;margin-top:6px;}
.kpi .it{background:#f8fbff;border:1px solid #dfe7f1;border-radius:6px;padding:5px;}
.kpi .lb{font-size:10px;text-transform:uppercase;color:var(--muted);letter-spacing:.6px;}
.kpi .vl{font-size:13px;font-weight:700;margin-top:1px;}
#live-card{padding:6px;}
#live-card h3{margin:0 0 4px;padding-bottom:4px;}
#live-card #live247{margin-bottom:4px;padding:4px 6px;font-size:11px;gap:8px;}
#live-card .kpi{grid-template-columns:repeat(4,minmax(88px,1fr));gap:4px;margin-top:4px;}
#live-card .kpi .it{padding:4px;}
#live-card .kpi .lb{font-size:9px;letter-spacing:.45px;}
#live-card .kpi .vl{font-size:12px;}
#live-card #v-last{font-size:14px;}
#live-card .optional,#live-card .optional-group{display:none;}
.main-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(320px,380px);gap:8px;align-items:start;}
.left-col,.right-col{display:flex;flex-direction:column;gap:8px;min-width:0;}
.chart-card .cbox{min-height:360px;}
.pnl-card .cbox{min-height:120px;}
.pm-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px;}
.pm-item{background:#f8fbff;border:1px solid #dfe7f1;border-radius:6px;padding:6px;}
.pm-item .lb{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;}
.pm-item .vl{font-size:13px;font-weight:700;margin-top:2px;}
.extras{display:grid;grid-template-columns:1.15fr 1fr 1fr;gap:8px;}
.tight table{margin:0;}
.stats-grid{display:grid;grid-template-columns:1fr;gap:8px;}
.mc-legend{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:10px;color:var(--muted);}
.mc-legend .dot{width:12px;height:2px;border-radius:2px;display:inline-block;}
@media (max-width:1300px){
  .main-grid{grid-template-columns:1fr;}
  .extras{grid-template-columns:1fr;}
  .kpi{grid-template-columns:repeat(3,minmax(90px,1fr));}
}
</style></head><body>
<div class="page">
    <div class="card">
        <h3>Date and info</h3>
        <div id="status">Loading...</div>
        <div class="nav-day">
          <button onclick="prev()">◄</button>
          <div class="dt" id="day-label">—</div>
          <button onclick="next()">►</button>
          <button onclick="goLive()" id="btn-live" class="on">Live</button>
            <input type="date" id="cal" onchange="goD(this.value)">
          <div class="days" id="days"></div>
        </div>
        <div id="hl" class="summary-line">ES —</div>
        <div class="tabs">
            <button id="tab-trading-btn" class="on" onclick="showTab('trading')">Trading</button>
            <button id="tab-stats-btn" onclick="showTab('stats')">Stats</button>
        </div>
    </div>

    <div id="tab-trading">
    <div class="card" id="live-card">
        <h3>Live price & all the numbers</h3>
        <div id="live247">
            <span class="d">Global Live (24/7):</span>
            <span>Last: <span id="glb-last" style="color:#1f2d3d;font-weight:bold">—</span></span>
          <span>Bid: <span id="glb-bid" class="g">—</span></span>
          <span>Ask: <span id="glb-ask" class="r">—</span></span>
        </div>
        <div class="kpi">
            <div class="it"><div class="lb">Last</div><div class="vl" id="v-last">—</div></div>
            <div class="it"><div class="lb">Bid / Ask</div><div class="vl"><span id="v-bid" class="g">—</span> / <span id="v-ask" class="r">—</span></div></div>
            <div class="it optional"><div class="lb">Spread</div><div class="vl" id="v-sprd">—</div></div>
            <div class="it"><div class="lb">VWAP</div><div class="vl b" id="v-vwap">—</div></div>
            <div class="it optional"><div class="lb">Sigma</div><div class="vl" id="v-std">—</div></div>
            <div class="it"><div class="lb">Z-Score</div><div class="vl" id="v-z">—</div></div>
        </div>
        <div class="kpi optional-group" style="grid-template-columns:repeat(5,minmax(90px,1fr));">
            <div class="it"><div class="lb">Open</div><div class="vl" id="v-open">—</div></div>
            <div class="it"><div class="lb">High</div><div class="vl g" id="v-high">—</div></div>
            <div class="it"><div class="lb">Low</div><div class="vl r" id="v-low">—</div></div>
            <div class="it"><div class="lb">VPOC</div><div class="vl y" id="v-vpoc">—</div></div>
            <div class="it"><div class="lb">Target Mode</div><div class="vl" id="sig-target">VWAP</div></div>
        </div>
    </div>

    <div class="main-grid">
        <div class="left-col">
            <div class="card">
                <h3>P/L data</h3>
                <div id="bot-pl">
                    <span class="d">VWAP Bot P&L:</span>
                  <span>Acct: <span id="b-acct" class="b">—</span></span>
                  <span>Daily: <span id="b-day" class="d">$0.00</span></span>
                  <span>Open: <span id="b-opn" class="d">$0.00</span></span>
                  <span>Max DD: <span id="b-dd" class="r">$0.00</span></span>
                  <span>Total: <span id="b-tot" class="d">$0.00</span></span>
                  <span>Trades: <span id="b-trd">0</span> / <span id="b-mtrd">4</span></span>
                  <span>Pos: <span id="b-pos" class="d">FLAT</span></span>
                </div>
            </div>

            <div class="card" id="regime-card">
                <h3>Regime</h3>
                <div class="kpi" style="grid-template-columns:repeat(4,minmax(90px,1fr));gap:6px;">
                    <div class="it"><div class="lb">VIX Proxy</div><div class="vl" id="r-vix">—</div></div>
                    <div class="it"><div class="lb">VWAP X (20 bars)</div><div class="vl" id="r-xings">—</div></div>
                    <div class="it"><div class="lb">Calendar</div><div class="vl" id="r-cal">—</div></div>
                    <div class="it"><div class="lb">Session Type</div><div class="vl" id="r-sesstype">—</div></div>
                    <div class="it"><div class="lb">Prev Levels</div><div class="vl" id="r-prevlvl" style="font-size:10px;">—</div></div>
                </div>
            </div>

            <div class="card chart-card">
                <div class="chart-head">
                    <h3 id="lc">Candles chart</h3>
                    <div class="chart-tools">
                        <button onclick="resetChart()">Reset Zoom</button>
                        <div class="legend">
                            <span class="item"><span class="dot" style="background:#2f6bff"></span>VWAP</span>
                            <span class="item"><span class="dot" style="background:#b07a00"></span>VPOC</span>
                            <span class="item"><span class="dot" style="background:#cf8a1a"></span>VPOC60</span>
                            <span class="item"><span class="dot" style="background:#1f2d3d"></span>P/L</span>
                            <span class="item"><span class="dot" style="background:#8fa4bf"></span>VOL</span>
                            <span class="item"><span class="dot" style="background:#7b61ff"></span>Z</span>
                            <span class="item"><span class="dot" style="background:#cf8a1a"></span>VOLAT</span>
                        </div>
                    </div>
                </div>
                <div class="cbox"><canvas id="c1"></canvas></div>
            </div>
        </div>

        <div class="right-col">
            <div class="card">
                <h3>Position manager</h3>
                <div class="pm-grid">
                    <div class="pm-item"><div class="lb">Direction</div><div class="vl" id="pm-dir">FLAT</div></div>
                    <div class="pm-item"><div class="lb">Quantity</div><div class="vl" id="pm-qty">0</div></div>
                    <div class="pm-item"><div class="lb">Entry</div><div class="vl" id="pm-ep">—</div></div>
                    <div class="pm-item"><div class="lb">Stop</div><div class="vl" id="pm-sl">—</div></div>
                    <div class="pm-item"><div class="lb">Target</div><div class="vl" id="pm-tp">—</div></div>
                    <div class="pm-item"><div class="lb">Open P&L</div><div class="vl" id="pm-opnl">$0.00</div></div>
                    <div class="pm-item"><div class="lb">Risk (ticks)</div><div class="vl" id="pm-risk">—</div></div>
                    <div class="pm-item"><div class="lb">R Multiple</div><div class="vl" id="pm-r">—</div></div>
                </div>
            </div>

            <div class="card tight">
                <h3>Order history</h3>
                <div id="orders-section" style="display:none;">
                    <table><tr><th>ID</th><th>Side</th><th>Type</th><th>Price</th><th>Size</th><th>Status</th><th>Role</th></tr><tbody id="ob"></tbody></table>
                </div>
                <table><tr><th>Action</th><th>Dir</th><th>Price</th><th>PnL</th><th>Reason</th><th>Bar</th></tr><tbody id="ohb"></tbody></table>
            </div>
        </div>
    </div>

    <div class="extras">
        <div class="card">
            <h3>Micro tape</h3>
            <div class="tw"><div class="cbox"><canvas id="tc"></canvas></div><div class="pbox"><canvas id="vp"></canvas></div></div>
        </div>
        <div class="card tight">
            <h3 id="lb">Recent bars</h3>
            <table><tr><th>O</th><th>H</th><th>L</th><th>C</th><th>Vol</th><th>Rng</th></tr><tbody id="bb"></tbody></table>
        </div>
        <div class="card tight">
            <h3 style="margin-top:0">Volume profile</h3>
            <table><tr><th>Price</th><th>Vol</th><th></th></tr><tbody id="vb"></tbody></table>
            <h3 id="lt" style="margin-top:8px">Tape</h3>
            <table><tr><th>Time</th><th>Price</th><th>Vol</th><th>Side</th></tr><tbody id="tb"></tbody></table>
        </div>
    </div>
    </div>

    <div id="tab-stats" style="display:none;">
        <div class="stats-grid">
            <div class="card">
                <h3>Combine stats</h3>
                <div class="kpi" style="grid-template-columns:repeat(5,minmax(90px,1fr));">
                    <div class="it"><div class="lb">Trades</div><div class="vl" id="s-trades">—</div></div>
                    <div class="it"><div class="lb">Win Rate (NF / All)</div><div class="vl" id="s-winrate">—</div></div>
                    <div class="it"><div class="lb">R:R</div><div class="vl" id="s-rr">—</div></div>
                    <div class="it"><div class="lb">Avg Win</div><div class="vl g" id="s-avgwin">—</div></div>
                    <div class="it"><div class="lb">Avg Loss</div><div class="vl r" id="s-avgloss">—</div></div>
                </div>
                <div class="kpi" style="grid-template-columns:repeat(5,minmax(90px,1fr));">
                    <div class="it"><div class="lb">Pass Rate</div><div class="vl g" id="s-pass">—</div></div>
                    <div class="it"><div class="lb">Blow Rate</div><div class="vl r" id="s-blow">—</div></div>
                    <div class="it"><div class="lb">Timeout</div><div class="vl d" id="s-timeout">—</div></div>
                    <div class="it"><div class="lb">Expectancy</div><div class="vl" id="s-exp">—</div></div>
                    <div class="it"><div class="lb">Profit Factor</div><div class="vl" id="s-pf">—</div></div>
                </div>
                <div class="kpi" style="grid-template-columns:repeat(4,minmax(90px,1fr));">
                    <div class="it"><div class="lb">Avg Days to Pass</div><div class="vl" id="s-dpass">—</div></div>
                    <div class="it"><div class="lb">Avg Days to Blow</div><div class="vl" id="s-dblow">—</div></div>
                    <div class="it"><div class="lb">Sims</div><div class="vl" id="s-sims">—</div></div>
                    <div class="it"><div class="lb">Max Days</div><div class="vl" id="s-maxdays">—</div></div>
                    <div class="it"><div class="lb">Sample Days</div><div class="vl" id="s-sampledays">—</div></div>
                    <div class="it"><div class="lb">Lookback Window</div><div class="vl" id="s-lookback">—</div></div>
                </div>
                <div class="kpi" style="grid-template-columns:repeat(5,minmax(90px,1fr));margin-top:6px;">
                    <div class="it"><div class="lb">Account Balance</div><div class="vl" id="s-acctbal">—</div></div>
                    <div class="it"><div class="lb">Peak EOD</div><div class="vl" id="s-peakeod">—</div></div>
                    <div class="it"><div class="lb">MLL Floor</div><div class="vl r" id="s-mllfloor">—</div></div>
                    <div class="it"><div class="lb">MLL Remaining</div><div class="vl" id="s-mllrem">—</div></div>
                    <div class="it"><div class="lb">Trailing DD</div><div class="vl" id="s-traildd">—</div></div>
                </div>
            </div>
            <div class="card chart-card">
                <div class="chart-head">
                    <h3>Monte Carlo (Topstep Combine)</h3>
                    <div class="mc-legend">
                        <span><span class="dot" style="background:#7e89ff"></span>Paths</span>
                        <span><span class="dot" style="background:#1f2d3d"></span>Start</span>
                        <span><span class="dot" style="background:#00a66a"></span>95th</span>
                        <span><span class="dot" style="background:#b07a00"></span>Median</span>
                        <span><span class="dot" style="background:#d64545"></span>5th</span>
                    </div>
                </div>
                <div class="cbox"><canvas id="c-mc" style="height:300px"></canvas></div>
            </div>
        </div>
    </div>
</div>
<pre id="dbg"></pre>
<script>
const F=(n,d=2)=>(n!=null&&isFinite(n)&&n!==0)?Number(n).toFixed(d):'—';
const FN=(n,d=2)=>(n!=null&&isFinite(n))?Number(n).toFixed(d):'—';
const FP=n=>(n!=null&&isFinite(n))?`${(Number(n)*100).toFixed(1)}%`:'—';
const $=id=>document.getElementById(id);
let vd=null,sessions=[];
let activeTab='trading';
function prev(){const i=sessions.indexOf(vd||sessions[0]);if(i<sessions.length-1){vd=sessions[i+1];go();}}
function next(){const i=sessions.indexOf(vd||sessions[0]);if(i>0){vd=sessions[i-1];go();}else{vd=null;go();}}
function goLive(){vd=null;go();}
function goD(d){vd=d;go();}
function resetChart(){chartState.zoom=180;chartState.offset=0;chartState.hoverX=null;chartState.hoverY=null;go();}
function showTab(tab){
    activeTab=tab;
    $('tab-trading').style.display=tab==='trading'?'block':'none';
    $('tab-stats').style.display=tab==='stats'?'block':'none';
    $('tab-trading-btn').className=tab==='trading'?'on':'';
    $('tab-stats-btn').className=tab==='stats'?'on':'';
    if(tab==='stats')go();
}

const chartState={zoom:180,offset:0,hoverX:null,hoverY:null,drag:false,dragStartX:0,dragOffset:0};
const clamp=(v,lo,hi)=>Math.max(lo,Math.min(hi,v));

function candles(id,bars,H,bot_trades,profile,botPos,liveVals,maxTradeBars){
    const c=$(id);if(!c)return;
    c._chartArgs={id,bars,H,bot_trades,profile,botPos,liveVals,maxTradeBars};
    const rerender=()=>{const a=c._chartArgs;if(a)candles(a.id,a.bars,a.H,a.bot_trades,a.profile,a.botPos,a.liveVals,a.maxTradeBars);};

    if(!c._interactiveBound){
        c.addEventListener('wheel',e=>{
            const a=c._chartArgs;
            if(!a||!a.bars||!a.bars.length)return;
            e.preventDefault();
            const rect=c.getBoundingClientRect();
            const xPos=e.clientX-rect.left;
            const padL=52,padR=86;
            const cW=Math.max(1,c.clientWidth-padL-padR);
            const oldZoom=clamp(Math.round(chartState.zoom||a.bars.length),40,a.bars.length);
            const maxOldOffset=Math.max(0,a.bars.length-oldZoom);
            const oldOffset=clamp(Math.round(chartState.offset||0),0,maxOldOffset);
            const oldStart=Math.max(0,a.bars.length-oldZoom-oldOffset);
            const ratio=clamp((xPos-padL)/cW,0,1);
            const anchor=oldStart+Math.round(ratio*Math.max(0,oldZoom-1));
            let newZoom=oldZoom+(e.deltaY>0?12:-12);
            newZoom=clamp(newZoom,40,a.bars.length);
            const maxStart=Math.max(0,a.bars.length-newZoom);
            let newStart=Math.round(anchor-ratio*Math.max(0,newZoom-1));
            newStart=clamp(newStart,0,maxStart);
            chartState.zoom=newZoom;
            chartState.offset=a.bars.length-newZoom-newStart;
            rerender();
        },{passive:false});

        c.addEventListener('mousedown',e=>{
            chartState.drag=true;
            chartState.dragStartX=e.clientX;
            chartState.dragOffset=chartState.offset||0;
            c.style.cursor='grabbing';
        });

        window.addEventListener('mouseup',()=>{
            chartState.drag=false;
            c.style.cursor='crosshair';
        });

        c.addEventListener('mousemove',e=>{
            const rect=c.getBoundingClientRect();
            chartState.hoverX=e.clientX-rect.left;
            chartState.hoverY=e.clientY-rect.top;
            if(chartState.drag){
                const a=c._chartArgs;
                if(a&&a.bars&&a.bars.length){
                    const padL=52,padR=86;
                    const cW=Math.max(1,c.clientWidth-padL-padR);
                    const visCount=clamp(Math.round(chartState.zoom||a.bars.length),40,a.bars.length);
                    const barPx=Math.max(1,cW/Math.max(1,visCount));
                    const dx=e.clientX-chartState.dragStartX;
                    const deltaBars=Math.round(dx/barPx);
                    const maxOffset=Math.max(0,a.bars.length-visCount);
                    chartState.offset=clamp(chartState.dragOffset+deltaBars,0,maxOffset);
                }
            }
            rerender();
        });

        c.addEventListener('mouseleave',()=>{
            chartState.hoverX=null;chartState.hoverY=null;
            rerender();
        });

        c._interactiveBound=true;
    }

    const dp=devicePixelRatio||1,W=c.parentElement.offsetWidth;
  c.width=W*dp;c.height=H*dp;c.style.width=W+'px';c.style.height=H+'px';
  const x=c.getContext('2d');x.scale(dp,dp);x.clearRect(0,0,W,H);
    c.style.cursor=chartState.drag?'grabbing':'crosshair';
        if(!bars||!bars.length){x.fillStyle='#7a8798';x.font='11px monospace';x.textAlign='center';x.fillText('No data',W/2,H/2);return;}
                const hasPnl=Array.isArray(bot_trades)&&bot_trades.some(t=>t&&t.action==='EXIT'&&isFinite(Number(t.pnl))&&isFinite(Number(t.bar_idx)));
                const pad={t:10,b:14,l:52,r:86},cW=W-pad.l-pad.r;
            const indH=86, pnlGap=8;
    chartState.zoom=clamp(Math.round(chartState.zoom||Math.min(220,bars.length)),40,bars.length);
    const visCount=clamp(chartState.zoom,40,bars.length);
    const maxOffset=Math.max(0,bars.length-visCount);
    chartState.offset=clamp(Math.round(chartState.offset||0),0,maxOffset);
    const start=Math.max(0,bars.length-visCount-chartState.offset);
    const vis=bars.slice(start,start+visCount);
    const slot=cW/Math.max(1,vis.length),bw=Math.max(2,slot*0.65);
    const priceBottom=H-pad.b-indH-pnlGap;
    const mn=Math.min(...vis.map(b=>b.l))-0.25,mx=Math.max(...vis.map(b=>b.h))+0.25,rng=mx-mn||1,cH=Math.max(80,priceBottom-pad.t);
  const tY=v=>pad.t+(1-(v-mn)/rng)*cH;

    // In-chart volume profile overlay (right side)
    const inR=(profile||[]).filter(v=>v.price>=mn&&v.price<=mx);
    const profileW=Math.max(70,Math.floor(cW*0.2));
    const profileX=W-pad.r-profileW;
    if(inR.length){
        const mV=Math.max(...inR.map(v=>v.vol),1);
        x.fillStyle='rgba(242,246,251,0.92)';
        x.fillRect(profileX,pad.t,profileW,cH);
        inR.forEach(v=>{
            const y=tY(v.price),w=(v.vol/mV)*(profileW-8),bh=Math.max(1,(cH/rng)*0.25+0.5);
            x.fillStyle='rgba(47,107,255,0.28)';
            x.fillRect(profileX+profileW-w-2,y-bh/2,w,bh);
        });
    }

    x.fillStyle='#5f7085';x.font='9px monospace';x.textAlign='right';
    for(let i=0;i<=5;i++){const v=mn+(rng/5)*i,y=tY(v);x.fillText(v.toFixed(2),pad.l-3,y+3);x.strokeStyle='#e6ecf3';x.lineWidth=.5;x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();}
    vis.forEach((b,i)=>{const cx=pad.l+i*slot+slot/2,up=b.c>=b.o,col=up?'#00a66a':'#d64545';
    x.strokeStyle=col;x.lineWidth=1;x.beginPath();x.moveTo(cx,tY(b.h));x.lineTo(cx,tY(b.l));x.stroke();
    const bt=tY(Math.max(b.o,b.c)),bb2=tY(Math.min(b.o,b.c));
    x.fillStyle=col;x.fillRect(cx-bw/2,bt,bw,Math.max(1,bb2-bt));});

    const drawLine = (prop, col, label) => {
    x.beginPath(); let f=true; let lastVal=null;
    vis.forEach((b,i)=>{
      if(b[prop]){
        const cx=pad.l+i*slot+slot/2, cy=tY(b[prop]);
        if(f){x.moveTo(cx,cy);f=false;}else{x.lineTo(cx,cy);}
        lastVal=b[prop];
      }
    });
    x.strokeStyle=col;x.lineWidth=1.5;x.stroke();
        if(lastVal) {
      const y=tY(lastVal);
            x.fillStyle=col;x.fillRect(W-pad.r, y-6, pad.r, 12);
                        x.fillStyle='#102033';x.font='8px monospace';x.textAlign='left';x.fillText(`${label} ${lastVal.toFixed(1)}`, W-pad.r+2, y+3);
    }
  };
        drawLine('vpoc', '#b07a00','VPOC');
        drawLine('vpoc60', '#cf8a1a','VPOC60');
        drawLine('vwap', '#2f6bff','VWAP');
        drawLine('hma', '#7b61ff','HMA');

    // Historical trade overlays (persistent TP/SL + TradingView-style box)
    const tradeWindows=[];
    if(Array.isArray(bot_trades)&&bot_trades.length){
        let openTrade=null;
        bot_trades.forEach(t=>{
            if(!t||!isFinite(Number(t.bar_idx)))return;
            const bi=Number(t.bar_idx);
            if(t.action==='ENTER'){
                const mb=Math.max(1,Number(t.max_bars||maxTradeBars||90));
                openTrade={
                    entry_idx:bi,
                    dir:t.dir||'LONG',
                    ep:Number(t.price),
                    sl:Number(t.sl),
                    tp:Number(t.tp),
                    max_idx:bi+mb,
                    exit_idx:null
                };
                tradeWindows.push(openTrade);
            }else if(t.action==='EXIT'&&openTrade&&openTrade.exit_idx===null){
                openTrade.exit_idx=bi;
                openTrade=null;
            }
        });
    }

    tradeWindows.forEach(tr=>{
        if(!isFinite(tr.ep)||!isFinite(tr.sl)||!isFinite(tr.tp))return;
        const chartEnd=start+vis.length-1;
        const plannedLeft=Math.max(start,Number(tr.entry_idx));
        const plannedRight=Math.min(chartEnd,Number(tr.max_idx));
        if(plannedRight<start||plannedLeft>chartEnd||plannedRight<plannedLeft)return;

        const x1=pad.l+(plannedLeft-start)*slot+slot/2;
        const x2=pad.l+(plannedRight-start)*slot+slot/2;
        const yEP=tY(tr.ep), ySL=tY(tr.sl), yTP=tY(tr.tp);

        const topReward=Math.min(yEP,yTP), hReward=Math.max(1,Math.abs(yEP-yTP));
        const topRisk=Math.min(yEP,ySL), hRisk=Math.max(1,Math.abs(yEP-ySL));
        x.fillStyle='rgba(0,166,106,0.12)';
        x.fillRect(x1,topReward,Math.max(1,x2-x1),hReward);
        x.fillStyle='rgba(214,69,69,0.12)';
        x.fillRect(x1,topRisk,Math.max(1,x2-x1),hRisk);

        x.setLineDash([4,3]);
        x.strokeStyle='#d64545';x.lineWidth=1;
        x.beginPath();x.moveTo(x1,ySL);x.lineTo(x2,ySL);x.stroke();
        x.strokeStyle='#00a66a';
        x.beginPath();x.moveTo(x1,yTP);x.lineTo(x2,yTP);x.stroke();
        x.setLineDash([]);

        if(isFinite(Number(tr.max_idx))&&tr.max_idx>=start&&tr.max_idx<=chartEnd){
            const xm=pad.l+(Number(tr.max_idx)-start)*slot+slot/2;
            x.setLineDash([2,3]);
            x.strokeStyle='#b07a00';x.lineWidth=1;
            x.beginPath();x.moveTo(xm,pad.t);x.lineTo(xm,priceBottom);x.stroke();
            x.setLineDash([]);
            x.fillStyle='#b07a00';x.fillRect(xm-16,pad.t+2,32,10);
            x.fillStyle='#fff';x.textAlign='center';x.font='8px monospace';x.fillText('MAX',xm,pad.t+10);
        }

        if(tr.exit_idx!==null && isFinite(Number(tr.exit_idx)) && tr.exit_idx>=start && tr.exit_idx<=chartEnd){
            const xe=pad.l+(Number(tr.exit_idx)-start)*slot+slot/2;
            x.setLineDash([1,0]);
            x.strokeStyle='#1f2d3d';x.lineWidth=1.2;
            x.beginPath();x.moveTo(xe,pad.t);x.lineTo(xe,priceBottom);x.stroke();
            x.fillStyle='#1f2d3d';x.fillRect(xe-16,pad.t+14,32,10);
            x.fillStyle='#fff';x.textAlign='center';x.font='8px monospace';x.fillText('EXIT',xe,pad.t+22);
        }
    });

    // Active position overlays (entry / stop / target)
    if(botPos && botPos.ep){
        const levels=[
            {p:botPos.ep,c:'#17324f',k:'EP'},
            {p:botPos.sl,c:'#d64545',k:'SL'},
            {p:botPos.tp,c:'#00a66a',k:'TP'}
        ].filter(v=>v.p&&isFinite(v.p));
        levels.forEach(lv=>{
            const y=tY(lv.p);
            x.setLineDash([4,3]);
            x.strokeStyle=lv.c;x.lineWidth=1;
            x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();
            x.setLineDash([]);
            x.fillStyle=lv.c;x.fillRect(pad.l+2,y-5,34,10);
            x.fillStyle='#fff';x.textAlign='center';x.fillText(lv.k,pad.l+19,y+3);
        });
    }
  
  const curPrice = vis[vis.length-1]?.c;
  if(curPrice) {
    const y=tY(curPrice);
        x.fillStyle=vis[vis.length-1].c >= vis[vis.length-1].o ? '#00a66a' : '#d64545';
    x.fillRect(W-pad.r, y-5, pad.r, 11);
        x.fillStyle='#102033';x.textAlign='left';x.fillText(curPrice.toFixed(1), W-pad.r+2, y+3);
  }

    // Plot key live/session values as guide lines on 1-min chart
    const lv=liveVals||{};
    const guides=[
        {k:'open', lb:'OPEN', c:'#5f7085'},
        {k:'high', lb:'HIGH', c:'#00a66a'},
        {k:'low', lb:'LOW', c:'#d64545'},
        {k:'bid', lb:'BID', c:'#00a66a'},
        {k:'ask', lb:'ASK', c:'#d64545'},
        {k:'last', lb:'LAST', c:'#1f2d3d'}
    ];
    guides.forEach(g=>{
        const v=Number(lv[g.k]);
        if(!isFinite(v)||v<=0||v<mn||v>mx)return;
        const y=tY(v);
        x.setLineDash([3,3]);
        x.strokeStyle=g.c;x.lineWidth=.9;
        x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();
        x.setLineDash([]);
        x.fillStyle=g.c;x.fillRect(pad.l+2,y-5,42,10);
        x.fillStyle='#fff';x.textAlign='center';x.font='8px monospace';
        x.fillText(g.lb,pad.l+23,y+3);
    });

    if(bot_trades&&bot_trades.length){
    bot_trades.forEach(t=>{
            const localIdx=t.bar_idx-start;
            if(localIdx<0||localIdx>=vis.length)return;
            const cx=pad.l+localIdx*slot+slot/2, cy=tY(t.price);
      x.beginPath();x.arc(cx,cy,4,0,Math.PI*2);
            x.fillStyle=t.action==='ENTER'?(t.dir==='LONG'?'#00b578':'#e25555'):'#d19b20';
            x.fill();x.strokeStyle='#1f2d3d';x.lineWidth=1;x.stroke();
            if(t.action==='EXIT' && t.reason){
            const rs=(String(t.reason).length>12)?`${String(t.reason).slice(0,12)}…`:String(t.reason);
            x.fillStyle='rgba(247,250,253,.96)';
            x.fillRect(cx+6,cy-10,Math.max(34,rs.length*6)+6,11);
            x.strokeStyle='#d2deea';x.lineWidth=.8;
            x.strokeRect(cx+6,cy-10,Math.max(34,rs.length*6)+6,11);
            x.fillStyle='#27435e';x.textAlign='left';x.font='8px monospace';
            x.fillText(rs,cx+9,cy-2);
            }
    });
  }

    if(hasPnl||vis.length){
        const panelTop=priceBottom+pnlGap;
        const panelBottom=H-pad.b;
        const panelH=Math.max(1,panelBottom-panelTop);
        x.fillStyle='rgba(246,249,252,0.95)';
        x.fillRect(pad.l,panelTop,cW,panelH);
        x.strokeStyle='#e1e9f2';x.lineWidth=1;
        x.strokeRect(pad.l,panelTop,cW,panelH);

        const volMax=Math.max(1,...vis.map(b=>Number(b.v)||0));
        vis.forEach((b,i)=>{
            const v=Math.max(0,Number(b.v)||0);
            const h=(v/volMax)*panelH;
            const bx=pad.l+i*slot+Math.max(1,slot*0.15);
            const bwv=Math.max(1,slot*0.7);
            x.fillStyle='rgba(143,164,191,0.28)';
            x.fillRect(bx,panelBottom-h,bwv,h);
        });

        const zVals=vis.map(b=>{
            if(isFinite(Number(b.z)))return Number(b.z);
            const vwap=Number(b.vwap), sd=Number(b.vwap_std), c=Number(b.c);
            return (isFinite(vwap)&&isFinite(sd)&&sd>0.01&&isFinite(c))?((c-vwap)/sd):0;
        });
        const volatVals=vis.map(b=>Math.max(0,Number(b.vwap_std)||0));
        const volatMax=Math.max(0.01,...volatVals);
        const vtY=v=>panelBottom-(Math.max(0,v)/volatMax)*panelH;
        x.strokeStyle='#cf8a1a';x.lineWidth=1.2;x.beginPath();
        volatVals.forEach((v,i)=>{
            const px=pad.l+i*slot+slot/2, py=vtY(v);
            if(i===0)x.moveTo(px,py);else x.lineTo(px,py);
        });
        x.stroke();

        const zCap=Math.max(2,Math.min(6,Math.max(...zVals.map(v=>Math.abs(v||0)),2)));
        const zY=v=>panelTop+(1-((Math.max(-zCap,Math.min(zCap,v))+zCap)/(2*zCap)))*panelH;
        const z0=zY(0);
        x.setLineDash([3,3]);
        x.strokeStyle='rgba(123,97,255,0.35)';x.lineWidth=1;
        x.beginPath();x.moveTo(pad.l,z0);x.lineTo(W-pad.r,z0);x.stroke();
        x.setLineDash([]);
        x.strokeStyle='#7b61ff';x.lineWidth=1.4;x.beginPath();
        zVals.forEach((v,i)=>{
            const px=pad.l+i*slot+slot/2, py=zY(v);
            if(i===0)x.moveTo(px,py);else x.lineTo(px,py);
        });
        x.stroke();

        if(hasPnl){
            const exits=(bot_trades||[])
                .filter(t=>t&&t.action==='EXIT'&&isFinite(Number(t.pnl))&&isFinite(Number(t.bar_idx)))
                .map(t=>({bar_idx:Number(t.bar_idx),pnl:Number(t.pnl)}))
                .sort((a,b)=>a.bar_idx-b.bar_idx);

            let cp=0, ei=0;
            while(ei<exits.length && exits[ei].bar_idx<start){cp+=exits[ei].pnl;ei++;}
            const pnlVals=[];
            for(let i=0;i<vis.length;i++){
                const gi=start+i;
                while(ei<exits.length && exits[ei].bar_idx<=gi){cp+=exits[ei].pnl;ei++;}
                pnlVals.push(cp);
            }

            if(pnlVals.length){
                const pMin=Math.min(0,...pnlVals);
                const pMax=Math.max(0,...pnlVals);
                const pR=Math.max(1,pMax-pMin);
                const pY=v=>panelTop+(1-(v-pMin)/pR)*panelH;
                const p0=pY(0);

                x.setLineDash([4,3]);
                x.strokeStyle='#b8c6d8';x.lineWidth=1;
                x.beginPath();x.moveTo(pad.l,p0);x.lineTo(W-pad.r,p0);x.stroke();
                x.setLineDash([]);

                x.strokeStyle='#1f2d3d';x.lineWidth=1.7;x.beginPath();
                pnlVals.forEach((v,i)=>{
                    const px=pad.l+i*slot+slot/2, py=pY(v);
                    if(i===0)x.moveTo(px,py);else x.lineTo(px,py);
                });
                x.stroke();

                x.fillStyle='#5f7085';x.font='8px monospace';x.textAlign='right';
                x.fillText(FN(pMax,0),pad.l-3,panelTop+8);
                x.fillText('0',pad.l-3,p0+3);
                x.fillText(FN(pMin,0),pad.l-3,panelBottom-2);
            }
        }

        const lastBar=vis[vis.length-1]||{};
        const lastVol=Math.max(0,Number(lastBar.v)||0);
        const lastZ=zVals.length?Number(zVals[zVals.length-1]):0;
        const lastVolat=volatVals.length?Number(volatVals[volatVals.length-1]):0;
        x.fillStyle='#5f7085';x.font='8px monospace';x.textAlign='left';
        x.fillText('VOL',W-pad.r+2,panelTop+8);
        x.fillText('Z',W-pad.r+2,panelTop+18);
        x.fillText('VOLAT',W-pad.r+2,panelTop+28);
        if(hasPnl)x.fillText('P/L',W-pad.r+2,panelTop+38);
        x.fillStyle='#31485f';x.textAlign='right';
        x.fillText(lastVol>=1000?`${(lastVol/1000).toFixed(1)}k`:`${Math.round(lastVol)}`,W-2,panelTop+8);
        x.fillText(FN(lastZ,2),W-2,panelTop+18);
        x.fillText(FN(lastVolat,2),W-2,panelTop+28);
    }

    x.fillStyle='#70839a';x.font='9px monospace';x.textAlign='center';
  const lC=Math.max(2,Math.floor(cW/60)), step=Math.max(1,Math.floor(vis.length/lC));
  for(let i=0;i<vis.length;i+=step){
    if(!vis[i].ts)continue;
    let d=new Date(vis[i].ts.replace(' ','T'));
        if(!isNaN(d))x.fillText(d.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false}),pad.l+i*slot+slot/2,H-3);
    }

    if(chartState.hoverX!=null&&chartState.hoverY!=null&&vis.length){
        const idx=clamp(Math.round((chartState.hoverX-pad.l)/Math.max(slot,1)),0,vis.length-1);
        const hb=vis[idx];
        const cx=pad.l+idx*slot+slot/2;
        const cy=tY(hb.c);
        x.setLineDash([4,3]);
        x.strokeStyle='rgba(42,68,100,.55)';x.lineWidth=1;
        x.beginPath();x.moveTo(cx,pad.t);x.lineTo(cx,H-pad.b);x.stroke();
        x.beginPath();x.moveTo(pad.l,cy);x.lineTo(W-pad.r,cy);x.stroke();
        x.setLineDash([]);

        const t=(hb.ts||'').toString();
        const tLabel=t.includes(' ')?t.split(' ')[1].slice(0,5):`#${start+idx}`;
        const info1=`${tLabel}  O:${F(hb.o)} H:${F(hb.h)} L:${F(hb.l)} C:${F(hb.c)} V:${hb.v||0}`;
        const info2=`VWAP:${F(hb.vwap,1)}  VPOC:${F(hb.vpoc,1)}  VPOC60:${F(hb.vpoc60,1)}`;
        x.font='10px monospace';
        const tw=Math.max(x.measureText(info1).width,x.measureText(info2).width)+12;
        const tx=clamp(cx+10,pad.l+4,W-pad.r-tw-4),ty=pad.t+6;
        x.fillStyle='rgba(247,250,253,.96)';x.fillRect(tx,ty,tw,30);
        x.strokeStyle='#d2deea';x.strokeRect(tx,ty,tw,30);
        x.fillStyle='#27435e';x.textAlign='left';
        x.fillText(info1,tx+6,ty+11);
        x.fillText(info2,tx+6,ty+23);
    }}

function drawMonteCarlo(id,stats,H){
    const c=$(id);if(!c)return;const dp=devicePixelRatio||1,W=c.parentElement.offsetWidth;
    c.width=W*dp;c.height=H*dp;c.style.width=W+'px';c.style.height=H+'px';
    const x=c.getContext('2d');x.scale(dp,dp);x.clearRect(0,0,W,H);
    if(!stats||!stats.ready||!stats.curve||!stats.curve.p50||!stats.curve.p50.length){
        x.fillStyle='#7a8798';x.font='11px monospace';x.textAlign='center';x.fillText('Need at least 5 closed trades to run stats',W/2,H/2);
        return;
    }
    const p5=stats.curve.p5||[], p50=stats.curve.p50||[], p95=stats.curve.p95||[];
    const xp=stats.x_axis||p50.map((_,i)=>i);
    const paths=stats.paths_preview||[];
    const startBal=(stats.start_balance!=null&&isFinite(stats.start_balance))?Number(stats.start_balance):50000;
    const passBal=(stats.pass_balance!=null&&isFinite(stats.pass_balance))?Number(stats.pass_balance):53000;
    const n=Math.max(p50.length,1);
    const pad={t:12,b:20,l:58,r:12},cW=W-pad.l-pad.r,cH=H-pad.t-pad.b;
    const all=[startBal,passBal,...p5,...p50,...p95,...paths.flat()];
    const mn=Math.min(...all)-80,mx=Math.max(...all)+80,rng=Math.max(1,mx-mn);
    const tY=v=>pad.t+(1-(v-mn)/rng)*cH;
    const maxX=Math.max(1,xp.length?xp[xp.length-1]:n-1);
    const tX=i=>pad.l+((xp[i]||0)/maxX)*cW;
    for(let i=0;i<=5;i++){
        const v=mn+(rng/5)*i,y=tY(v);
        x.strokeStyle='#e6ecf3';x.lineWidth=.7;x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();
        x.fillStyle='#5f7085';x.font='9px monospace';x.textAlign='right';x.fillText(v.toFixed(0),pad.l-4,y+3);
    }

    if(paths.length){
        x.strokeStyle='rgba(126,137,255,0.12)';x.lineWidth=1;
        paths.forEach(arr=>{
            if(!arr||!arr.length)return;
            x.beginPath();
            arr.forEach((v,i)=>{const px=tX(i),py=tY(v);if(i===0)x.moveTo(px,py);else x.lineTo(px,py);});
            x.stroke();
        });
    }

    const drawLine=(arr,col,w=2)=>{
        x.beginPath();
        arr.forEach((v,i)=>{const px=tX(i),py=tY(v);if(i===0)x.moveTo(px,py);else x.lineTo(px,py);});
        x.strokeStyle=col;x.lineWidth=w;x.stroke();
    };
    drawLine(new Array(n).fill(startBal),'#1f2d3d',1.4);
    x.setLineDash([5,3]);x.strokeStyle='#1f2d3d';x.lineWidth=1.2;
    x.beginPath();x.moveTo(pad.l,tY(startBal));x.lineTo(W-pad.r,tY(startBal));x.stroke();
    x.strokeStyle='rgba(0,166,106,.55)';
    x.beginPath();x.moveTo(pad.l,tY(passBal));x.lineTo(W-pad.r,tY(passBal));x.stroke();
    x.setLineDash([]);
    drawLine(p95,'#00a66a',2.2);
    drawLine(p50,'#b07a00',2.2);
    drawLine(p5,'#d64545',2.2);
    x.fillStyle='#70839a';x.font='9px monospace';x.textAlign='center';
    x.fillText('Trade #',pad.l+cW/2,H-3);
}

function tickC(tid,pid,tape,prof,H,bars){
  const c=$(tid);if(!c)return;const dp=devicePixelRatio||1,W=c.parentElement.offsetWidth;
  c.width=W*dp;c.height=H*dp;c.style.width=W+'px';c.style.height=H+'px';
  const x=c.getContext('2d');x.scale(dp,dp);x.clearRect(0,0,W,H);
  const pad={t:10,b:14,l:52,r:4},cW=W-pad.l-pad.r,cH=H-pad.t-pad.b;
  let ap=[];
  if(tape&&tape.length)ap.push(...tape.map(t=>t.price));
  if(bars&&bars.length){const lb=bars.slice(-30);ap.push(...lb.map(b=>b.h),...lb.map(b=>b.l));}
  if(ap.length<2&&prof&&prof.length)ap.push(...prof.map(p=>p.price));
    if(ap.length<2){x.fillStyle='#7a8798';x.font='11px monospace';x.textAlign='center';x.fillText('Waiting...',W/2,H/2);return;}
  const mn=Math.min(...ap)-0.25,mx=Math.max(...ap)+0.25,rng=mx-mn||1;
  const tY=v=>pad.t+(1-(v-mn)/rng)*cH;
    x.fillStyle='#5f7085';x.font='9px monospace';x.textAlign='right';
    for(let i=0;i<=5;i++){const v=mn+(rng/5)*i,y=tY(v);x.fillText(v.toFixed(2),pad.l-3,y+3);x.strokeStyle='#e6ecf3';x.lineWidth=.5;x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();}
  if(tape&&tape.length>=2){const sx=cW/Math.max(tape.length-1,1);
        x.beginPath();tape.forEach((t,i)=>{const px=pad.l+i*sx,py=tY(t.price);if(!i)x.moveTo(px,py);else x.lineTo(px,py);});x.strokeStyle='#8fa4bf';x.lineWidth=1;x.stroke();
        tape.forEach((t,i)=>{const px=pad.l+i*sx,py=tY(t.price);x.beginPath();x.arc(px,py,1.5,0,Math.PI*2);x.fillStyle=t.side==='BUY'?'#00a66a':(t.side==='SELL'?'#d64545':'#7f8fa3');x.fill();});}
    else{x.fillStyle='#70839a';x.font='10px monospace';x.textAlign='center';x.fillText('No trades in tape (Live off/After hours)',pad.l+cW/2,H/2);}
  const pc=$(pid);if(!pc)return;const pW=pc.parentElement.offsetWidth;
  pc.width=pW*dp;pc.height=H*dp;pc.style.width=pW+'px';pc.style.height=H+'px';
  const p=pc.getContext('2d');p.scale(dp,dp);p.clearRect(0,0,pW,H);
  if(!prof||!prof.length)return;const inR=prof.filter(v=>v.price>=mn&&v.price<=mx);if(!inR.length)return;
  const mV=Math.max(...inR.map(v=>v.vol)),bH=Math.max(1,(cH/rng)*0.25+0.5);
    inR.forEach(v=>{const y=tY(v.price),w=(v.vol/mV)*(pW-6);p.fillStyle=v.vol===mV?'#b07a00':'rgba(47,107,255,0.35)';p.fillRect(2,y-bH/2,w,bH);
        if(inR.length<=30){p.fillStyle='#70839a';p.font='7px monospace';p.fillText(Math.round(v.vol)+'',w+4,y+3);}});
}

async function go(){try{
  const sl=await(await fetch('/api/sessions')).json();sessions=sl.dates||[];
  $('days').innerHTML=sessions.slice(0,10).map(d=>`<div class="dbtn${(vd||sessions[0])===d?' on':''}" onclick="goD('${d}')">${d.slice(5)}</div>`).join('');
  const url=vd?`/api/day/${vd}`:'/api/data';
  const d=await(await fetch(url)).json();
  const live=!vd,sp=d.ask&&d.bid?d.ask-d.bid:0;
  const bars=[...(d.bars||[])];if(d.current_bar)bars.push(d.current_bar);const tp=d.tape||[];
  $('day-label').textContent=d.date||'—';$('btn-live').className=live?'on':'';
  const rth=d.rth?'<span class="g">RTH</span>':'<span class="y">PRE/POST</span>';
  $('status').innerHTML=live?(d.connected?`<span class="g">● LIVE</span> ${rth} Bars:${bars.length} Trades:${d.trade_count} Vol:${(d.volume||0).toLocaleString()}`:`<span class="r">● OFF</span>`):`<span class="d">● ${d.date}</span> Bars:${bars.length} Trades:${d.trade_count} Vol:${(d.volume||0).toLocaleString()}`;
  $('hl').innerHTML=`ES ${F(d.last)} <span class="g">H${F(d.high)}</span> <span class="r">L${F(d.low)}</span> <span class="b">VWAP${F(d.vwap)}</span> Z:<span class="${Math.abs(d.z_score||0)>=1.5?'y':''}">${F(d.z_score)}</span>`;
  if(typeof d.live_last!=='undefined'){$('glb-last').textContent=F(d.live_last);$('glb-bid').textContent=F(d.live_bid);$('glb-ask').textContent=F(d.live_ask);}
  $('v-last').textContent=F(d.last);$('v-bid').textContent=F(d.bid);$('v-ask').textContent=F(d.ask);$('v-sprd').textContent=F(sp);
  $('v-vwap').textContent=F(d.vwap);$('v-std').textContent=F(d.vwap_std);
  $('v-z').textContent=F(d.z_score);$('v-z').className=Math.abs(d.z_score||0)>=1.5?'y':'';
  $('v-open').textContent=F(d.open);$('v-high').textContent=F(d.high);$('v-low').textContent=F(d.low);$('v-vpoc').textContent=F(d.vpoc);
    $('sig-target').textContent=((d.target_mode||'vwap')+'').toUpperCase();
  $('lc').textContent=`1-Min (${bars.length})`;$('lb').textContent=`Bars (${bars.length})`;$('lt').textContent=`Tape (${tp.length})`;

    const st=d.stats||{};
    $('s-trades').textContent=st.trades!=null?String(st.trades):'—';
    const wrNonFlat=(st.win_rate_nonflat!=null)?st.win_rate_nonflat:st.win_rate;
    const wrAll=(st.win_rate_all!=null)?st.win_rate_all:st.win_rate;
    $('s-winrate').textContent=`${FP(wrNonFlat)} / ${FP(wrAll)}`;
    $('s-rr').textContent=FN(st.rr,2);
    $('s-avgwin').textContent=st.avg_win!=null?`$${FN(st.avg_win)}`:'—';
    $('s-avgloss').textContent=st.avg_loss!=null?`$${FN(st.avg_loss)}`:'—';
    $('s-pass').textContent=FP(st.pass_rate);
    $('s-blow').textContent=FP(st.blow_rate);
    $('s-timeout').textContent=FP(st.timeout_rate);
    $('s-exp').textContent=st.expectancy!=null?`$${FN(st.expectancy)}`:'—';
    $('s-pf').textContent=FN(st.profit_factor,2);
    $('s-dpass').textContent=FN(st.avg_days_to_pass,1);
    $('s-dblow').textContent=FN(st.avg_days_to_blow,1);
    $('s-sims').textContent=st.simulations!=null?String(st.simulations):'—';
    $('s-maxdays').textContent=st.max_days!=null?String(st.max_days):'—';
    $('s-sampledays').textContent=st.sample_days!=null?String(st.sample_days):'—';
    $('s-lookback').textContent=st.lookback_days!=null?String(st.lookback_days)+' days':'—';

    // MLL tracking row
    $('s-acctbal').textContent=st.account_balance!=null?`$${FN(st.account_balance,0)}`:'—';
    $('s-peakeod').textContent=st.peak_eod_balance!=null?`$${FN(st.peak_eod_balance,0)}`:'—';
    $('s-mllfloor').textContent=st.mll_floor!=null?`$${FN(st.mll_floor,0)}`:'—';
    const mllRem=st.mll_remaining;
    if(mllRem!=null&&isFinite(mllRem)){
        $('s-mllrem').textContent=`$${FN(mllRem,0)}`;
        $('s-mllrem').className=mllRem<=500?'vl r':mllRem<=1000?'vl y':'vl g';
    }else{$('s-mllrem').textContent='—';}
    $('s-traildd').textContent=st.trailing_mll!=null?`$${FN(st.trailing_mll,0)}`:'—';

    // Position manager defaults
    $('pm-dir').textContent='FLAT'; $('pm-dir').className='vl d';
    $('pm-qty').textContent='0'; $('pm-ep').textContent='—'; $('pm-sl').textContent='—';
    $('pm-tp').textContent='—'; $('pm-opnl').textContent='$0.00'; $('pm-opnl').className='vl d';
    $('pm-risk').textContent='—'; $('pm-r').textContent='—';

  if(d.bot) {
    if(d.bot.account_name){$('b-acct').textContent=d.bot.account_name;}
    if(d.bot.daily_pnl!=null){$('b-day').textContent=`$${F(d.bot.daily_pnl)}`; $('b-day').className=d.bot.daily_pnl<0?'r':'g';}
    if(d.bot.open_pnl!=null){$('b-opn').textContent=`$${F(d.bot.open_pnl)}`; $('b-opn').className=d.bot.open_pnl<0?'r':(d.bot.open_pnl>0?'g':'d');}
    if(d.bot.max_dd!=null){$('b-dd').textContent=`-$${F(Math.abs(d.bot.max_dd))}`;}
    if(d.bot.total_pnl!=null){$('b-tot').textContent=`$${F(d.bot.total_pnl)}`; $('b-tot').className=d.bot.total_pnl<0?'r':'g';}
    if(d.bot.trades!=null){$('b-trd').textContent=d.bot.trades;}
    if(d.bot.max_trades!=null){$('b-mtrd').textContent=d.bot.max_trades;}
    if(d.bot.pos) {$('b-pos').innerHTML=`<span class="${d.bot.pos.dir==='LONG'?'g':'r'}">${d.bot.pos.dir}</span> @ ${F(d.bot.pos.ep)}`;}
    else {$('b-pos').innerHTML=`<span class="d">FLAT</span>`;}

        // Position manager (broker-like)
        const p=d.bot.pos;
        if(p){
            const qty=2;  // FIX: match CONTRACTS config
            const riskTicks=(p.sl&&p.ep)?Math.abs((p.ep-p.sl)/0.25):0;
            const rr=(riskTicks>0&&d.bot.open_pnl!=null)?(d.bot.open_pnl/(riskTicks*12.5*qty)):0;  // ES tick value = $12.50
            $('pm-dir').textContent=p.dir||'FLAT';
            $('pm-dir').className=`vl ${p.dir==='LONG'?'g':'r'}`;
            $('pm-qty').textContent=String(qty);
            $('pm-ep').textContent=F(p.ep);
            $('pm-sl').textContent=F(p.sl);
            $('pm-tp').textContent=F(p.tp);
            $('pm-opnl').textContent=`$${F(d.bot.open_pnl||0)}`;
            $('pm-opnl').className=`vl ${(d.bot.open_pnl||0)<0?'r':((d.bot.open_pnl||0)>0?'g':'d')}`;
            $('pm-risk').textContent=isFinite(riskTicks)?riskTicks.toFixed(1):'—';
            $('pm-r').textContent=isFinite(rr)?rr.toFixed(2):'—';
        }
    
    // Active Orders
    const orders=d.bot.active_orders||[];
    const osec=$('orders-section');
    if(orders.length>0){
      osec.style.display='block';
      const typeMap={1:'Limit',2:'Market',3:'Stop Limit',4:'Stop Mkt'};
      const sideMap={0:'Buy',1:'Sell'};
      const statusMap={0:'Pending',1:'Working',2:'Filled',3:'Cancelled',4:'Rejected',5:'Expired'};
      $('ob').innerHTML=orders.map(o=>{
        const side=sideMap[o.side]||o.side;
        const typ=typeMap[o.type]||o.type;
        const stat=statusMap[o.status]||o.status;
        const pr=o.stopPrice||o.limitPrice||o.price||'—';
        const isSL=(o.type===4);
        const isTP=(o.type===1);
        const role=isSL?'<span class="r">Stop Loss</span>':(isTP?'<span class="g">Take Profit</span>':'Entry');
        const sideClass=side==='Buy'?'g':'r';
        const statClass=stat==='Working'?'y':(stat==='Filled'?'g':'d');
        return`<tr><td class="d">${o.id||'—'}</td><td class="${sideClass}">${side}</td><td>${typ}</td><td>${F(pr)}</td><td>${o.size||'—'}</td><td class="${statClass}">${stat}</td><td>${role}</td></tr>`;
      }).join('');
    } else {
      osec.style.display=d.bot.pos?'block':'none';
      $('ob').innerHTML=d.bot.pos?'<tr><td colspan="7" class="d">No active bracket orders</td></tr>':'';
    }
  }

  // Regime panel
  if(d.regime&&live) {
    const rg=d.regime;
    // VIX proxy
    if(rg.vix_proxy!=null){
      const vxCls=rg.vix_status==='EXTREME'?'r':(rg.vix_status==='HIGH'?'y':'g');
      $('r-vix').innerHTML=`<span class="${vxCls}">${rg.vix_proxy.toFixed(1)} ${rg.vix_status}</span>`;
    } else { $('r-vix').textContent='N/A'; }
    // VWAP crossings
    const xCls=rg.vwap_regime==='TRENDING'?'r':(rg.vwap_regime==='BALANCED'?'g':'y');
    $('r-xings').innerHTML=`<span class="${xCls}">${rg.vwap_crossings} (${rg.vwap_regime})</span>`;
    // Calendar
    if(rg.calendar_event){
      const calBeh=rg.high_impact_behavior==='skip'?'SKIP':'REDUCE';
      $('r-cal').innerHTML=`<span class="r">${rg.calendar_event} [${calBeh}]</span>`;
    } else { $('r-cal').innerHTML=`<span class="g">Clear</span>`; }
    // Session type
    if($('r-sesstype')){
      const stCls=rg.session_type==='TRENDING'?'r':(rg.session_type==='RANGING'?'y':'g');
            $('r-sesstype').innerHTML=`<span class="${stCls}">${rg.session_type||'NEUTRAL'}</span>`;
    }
    // Previous session levels
    if($('r-prevlvl')){
      const pvw=rg.prev_vwap!=null?rg.prev_vwap.toFixed(2):'—';
      const pvc=rg.prev_vpoc!=null?rg.prev_vpoc.toFixed(2):'—';
      $('r-prevlvl').textContent=`VWAP ${pvw} | VPOC ${pvc}`;
    }
  }

    // Order history (fills)
    const h=(d.bot_trades||[]).slice().reverse().slice(0,18);
    $('ohb').innerHTML=h.length?h.map(t=>{
        const action=t.action||'—';
        const dir=t.dir||'—';
        const price=F(t.price);
        const pnl=t.pnl!=null?`$${F(t.pnl)}`:'—';
        const reason=t.reason||'—';
        const bi=t.bar_idx!=null?t.bar_idx:'—';
        const cls=action==='ENTER'?'b':(t.pnl<0?'r':(t.pnl>0?'g':'d'));
        return`<tr><td class="${cls}">${action}</td><td>${dir}</td><td>${price}</td><td class="${t.pnl<0?'r':(t.pnl>0?'g':'d')}">${pnl}</td><td class="d">${reason}</td><td class="d">${bi}</td></tr>`;
    }).join(''):'<tr><td colspan="6" class="d">No fills yet</td></tr>';

  $('bb').innerHTML=bars.slice(-10).reverse().map(b=>`<tr><td>${F(b.o)}</td><td class="g">${F(b.h)}</td><td class="r">${F(b.l)}</td><td class="${b.c>=b.o?'g':'r'}">${F(b.c)}</td><td>${b.v}</td><td>${F(b.h-b.l)}</td></tr>`).join('');
  const tv=d.top_vol||[];
  if(tv.length){const mV=Math.max(...tv.map(v=>v.vol));
        $('vb').innerHTML=tv.slice(0,15).map(v=>{const w=Math.round((v.vol/mV)*150);return`<tr><td${v.price===d.vpoc?' class="y"':''}>${F(v.price)}</td><td>${v.vol.toLocaleString()}</td><td><span class="vb" style="width:${w}px;${v.price===d.vpoc?'background:#b07a00':''}"></span></td></tr>`;}).join('');}
  $('tb').innerHTML=tp.slice().reverse().slice(0,20).map(t=>`<tr><td class="d">${t.time}</td><td>${F(t.price)}</td><td>${t.vol}</td><td class="${t.side==='BUY'?'g':(t.side==='SELL'?'r':'d')}">${t.side}</td></tr>`).join('');
  let dbg='';if(d.errors?.length)dbg+=`Errors: ${d.errors.join(', ')}\n`;
  $('dbg').textContent=dbg;
  const prof=d.profile||tv;
        const chartH=(window.innerWidth<1300?320:390)+90;
    candles('c1',bars,chartH,d.bot_trades,prof,d.bot?d.bot.pos:null,{last:d.last,bid:d.bid,ask:d.ask,open:d.open,high:d.high,low:d.low},d.time_stop_mins||90);
        drawMonteCarlo('c-mc',d.stats,300);
    tickC('tc','vp',tp,prof,200,bars);
}catch(e){$('status').innerHTML='<span class="r">'+e+'</span>';}}
go();setInterval(()=>{if(!vd)go();},500);
window.addEventListener('resize',go);
</script></body></html>"""

class H2(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=="/api/data":
            with LOCK:
                sess=get_or_create_session();d=sess.to_dict();d["connected"]=connected;d["errors"]=errors[-5:];d["raw_trades"]=raw_log[-2:];d["rth"]=is_rth()
                d["target_mode"] = TARGET_MODE
                d["time_stop_mins"] = TIME_STOP_MINS
                d["live_bid"]=live_bid;d["live_ask"]=live_ask;d["live_last"]=live_last
            # FIX#1: Read bot state under BOT_STATE_LOCK (separate from session LOCK)
            with BOT_STATE_LOCK:
                d["bot_trades"]=list(global_bot.trade_history)  # shallow copy
                epnl = 0
                pos_snap = global_bot.pos.copy() if global_bot.pos else None  # snapshot
                if pos_snap:
                    ep = pos_snap["ep"]
                    ticks = (live_last - ep) / TICK_SIZE if pos_snap["dir"] == "LONG" else (ep - live_last) / TICK_SIZE
                    epnl = ticks * TICK_VALUE * CONTRACTS
                
                cur_eq = global_bot.total_pnl + epnl
                if cur_eq > global_bot.peak_pnl: global_bot.peak_pnl = cur_eq
                dd = global_bot.peak_pnl - cur_eq
                if dd > global_bot.max_dd: global_bot.max_dd = dd
                
                d["bot"] = {"account_name": global_bot.account_name, "total_pnl": global_bot.total_pnl, "daily_pnl": global_bot.daily_pnl, "trades": global_bot.trades_today, "max_trades": MAX_TRADES, "pos": pos_snap, "open_pnl": epnl, "max_dd": global_bot.max_dd, "gutter_win": global_bot.gutter_win, "gutter_loss": global_bot.gutter_loss, "active_orders": list(global_bot.cached_orders), "account_balance": global_bot.account_balance_with_open, "mll_floor": global_bot.mll_floor, "mll_remaining": global_bot.mll_remaining, "peak_eod_balance": global_bot.peak_eod_balance}
                d["bot_trades"] = list(global_bot.trade_history)

            # FIX#7: Use cached stats instead of computing inline
            d["stats"] = _bg_stats_cache.get("data") or {"ready": False, "trades": 0}
            # Regime snapshot for dashboard
            crossings = REGIME.get("vwap_crossings", 0)
            d["regime"] = {
                "vix_proxy": REGIME.get("vix_proxy"),
                "vix_status": ("EXTREME" if (REGIME.get("vix_proxy") or 0) > 30 else ("HIGH" if (REGIME.get("vix_proxy") or 0) > 22 else "NORMAL")) if REGIME.get("vix_proxy") else None,
                "vwap_crossings": crossings,
                "vwap_regime": "TRENDING" if crossings <= VWAP_CROSS_TRENDING else ("BALANCED" if crossings >= VWAP_CROSS_BALANCED else "NEUTRAL"),
                "calendar_event": REGIME.get("calendar_event"),
                "high_impact_behavior": HIGH_IMPACT_BEHAVIOR,
                "session_type": REGIME.get("session_type", "NEUTRAL"),
                "prev_vwap": REGIME.get("prev_vwap"),
                "prev_vpoc": REGIME.get("prev_vpoc"),
            }
            self.jr(d)
        elif self.path.startswith("/api/day/"):
            ds=self.path.split("/api/day/")[1][:10]
            s=Session.load(ds)
            if not s and global_bot.token:
                print(f"\n  Fetching history sync for {ds}...")
                s = fetch_history_sync(global_bot.token, ds)
                if s: s.save()
            d = s.to_dict() if s else {"error":"No data","date":ds}
            d["target_mode"] = TARGET_MODE
            d["time_stop_mins"] = TIME_STOP_MINS
            if s and len(s.bars) > 15:
                bstate, btrades = run_backtest(s.bars, gutter_win=global_bot.gutter_win, gutter_loss=global_bot.gutter_loss)
                d["bot"] = bstate
                d["bot_trades"] = btrades
            # FIX#7: Use cached stats instead of computing inline
            d["stats"] = _bg_stats_cache.get("data") or {"ready": False, "trades": 0}
            self.jr(d)
        elif self.path=="/api/sessions":
            self.jr({"dates":list_sessions()})
        else:
            self.send_response(200);self.send_header("Content-Type","text/html");self.end_headers();self.wfile.write(HTML.encode())
            
    def do_POST(self):
        if self.path=="/api/toggle_gw":
            with BOT_STATE_LOCK:  # FIX#1: use dedicated bot state lock
                global_bot.gutter_win = not getattr(global_bot, "gutter_win", False)
            self.jr({"success": True})
        elif self.path=="/api/toggle_gl":
            with BOT_STATE_LOCK:  # FIX#1: use dedicated bot state lock
                global_bot.gutter_loss = not getattr(global_bot, "gutter_loss", False)
            self.jr({"success": True})
        else:
            self.send_error(404)
            
    def jr(self,d):
        self.send_response(200);self.send_header("Content-Type","application/json");self.end_headers();self.wfile.write(json.dumps(d,default=str).encode())
    def log_message(self,*a):pass

def serve(port=8080):
    import socket
    class S(HTTPServer):
        allow_reuse_address=True
        def server_bind(self):self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);super().server_bind()
    s=S(("0.0.0.0",port),H2);threading.Thread(target=s.serve_forever,daemon=True).start()

async def main():
    print("="*50);print("  ES LIVE v8 — Historical + Live + VWAP Bot (ES)");print("="*50)
    token=await auth()
    if not token:return
    print("  ✓ Auth\n")
    print(f"  Backfilling {DAYS_BACK} days of RTH bars...")
    await backfill(token)
    await global_bot.setup(token)
    get_or_create_session()
    # Load previous session levels for confluence
    load_prev_session_levels()
    # Check economic calendar for today
    check_calendar_event()
    # Fetch VIX proxy from TopStepX VX futures
    await fetch_vix_proxy(token)
    # FIX#7: Seed stats cache immediately so dashboard isn't empty on first load
    try:
        combined = get_combined_exit_trades(gutter_win=True, gutter_loss=True, min_refresh_sec=0)
        _bg_stats_cache["data"] = build_bot_stats(combined, max_trades=MAX_TRADES)
        print(f"  ✓ Stats seeded ({len(combined)} historical trades)")
    except Exception as e:
        print(f"  ⚠ Initial stats failed: {e}")
    serve(8080);print("  ✓ http://localhost:8080");print(f"  ✓ {CONTRACT}\n")
    await stream(token)

if __name__=="__main__":
    try:asyncio.run(main())
    except KeyboardInterrupt:print("\n  Saving...");save_current();print("  Done.")