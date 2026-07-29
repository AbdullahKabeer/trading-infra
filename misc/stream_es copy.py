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

import asyncio,os,json,threading,atexit,glob
from datetime import datetime,timezone,timedelta,time as dtime
from http.server import HTTPServer,BaseHTTPRequestHandler
from collections import defaultdict
import httpx,websockets

try:
    import pytz; ET=pytz.timezone("America/New_York")
except: ET=timezone(timedelta(hours=-4))

# ============================================================
# BOT CONFIGURATION & CLASS
# ============================================================
BOT_ACTIVE = True
DRY_RUN = False
TARGET_MODE = "vwap"  # 'volume' uses vpoc, 'vwap' uses vwap
Z_THRESH = 1.5
CONTRACTS = 2
MAX_TRADES = 6
STOP_RATIO = 0.5
MIN_STOP_TICKS = 6
MAX_STOP_TICKS = 24
TRAIL_ACTIVATE = 20
TRAIL_DISTANCE = 16
EXIT_MIN_TICKS = 12
TIME_STOP_MINS = 90
TICK_SIZE = 0.25
TICK_VALUE = 12.50
GUTTER_GOAL = 1500
GUTTER_DD = 1900

API="https://api.topstepx.com/api"
HUB="https://rtc.topstepx.com/hubs/market"
CONTRACT="CON.F.US.EP.M26"
DATA_DIR="es_sessions"
DAYS_BACK=5
os.makedirs(DATA_DIR,exist_ok=True)

LOCK=threading.Lock()

class Session:
    def __init__(self,date_str=None):
        self.date=date_str or datetime.now(ET).strftime("%Y-%m-%d")
        self.bars=[];self.cur_bar=None
        self.vol_profile=defaultdict(int)
        self.tape=[]
        self.cum_pv=0.0;self.cum_v=0;self.cum_p2v=0.0;self.vwap=0.0;self.vwap_std=0.0
        self.open=0.0;self.high=0.0;self.low=999999.0
        self.trade_count=0;self.total_volume=0;self.tick_count=0
        self.bid=0.0;self.ask=0.0;self.last=0.0;self.quotes=[]

    def add_bar(self,o,h,l,c,v,ts_str):
        """Add a completed historical bar."""
        self.bars.append({"ts":ts_str,"o":o,"h":h,"l":l,"c":c,"v":v})
        # Update session stats
        if self.open==0:self.open=o
        if h>self.high:self.high=h
        if l<self.low:self.low=l
        self.last=c;self.total_volume+=v;self.trade_count+=v  # approximate
        # Volume profile from bar (distribute evenly across OHLC range)
        lo_lv=round(l*4)/4;hi_lv=round(h*4)/4
        n_levels=max(1,int((hi_lv-lo_lv)/0.25)+1)
        vpl=v//n_levels if n_levels>0 else v
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

    def add_trade(self,price,vol,side_str,now_utc):
        self.trade_count+=1;self.total_volume+=vol;self.last=price
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
            if self.cur_bar:self.bars.append(self.cur_bar)
            self.cur_bar={"ts":bk_str,"o":price,"h":price,"l":price,"c":price,"v":vol}
        else:
            b=self.cur_bar
            if price>b["h"]:b["h"]=price
            if price<b["l"]:b["l"]=price
            b["c"]=price;b["v"]+=vol
        self.tape.append({"time":now_utc.strftime("%H:%M:%S.%f")[:12],"price":price,"vol":vol,"side":side_str})
        if len(self.tape)>500:del self.tape[:-500]

    def add_quote(self,bid_v,ask_v,now_utc):
        self.tick_count+=1
        if bid_v and bid_v>0:self.bid=bid_v
        if ask_v and ask_v>0:self.ask=ask_v
        if self.bid>0 and self.ask>0:self.last=(self.bid+self.ask)/2
        sp=self.ask-self.bid if self.ask>0 and self.bid>0 else 0
        self.quotes.append({"time":now_utc.strftime("%H:%M:%S.%f")[:12],"bid":self.bid,"ask":self.ask,"spread":round(sp,2)})
        if len(self.quotes)>50:del self.quotes[:-50]

    def to_dict(self):
        vpoc=max(self.vol_profile,key=self.vol_profile.get) if self.vol_profile else 0
        top_vol=sorted(self.vol_profile.items(),key=lambda x:-x[1])[:30]
        ab=[{"ts":b.get("ts"),"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"],"v":b["v"]} for b in self.bars[-500:]]
        cb={"ts":self.cur_bar.get("ts"),"o":self.cur_bar["o"],"h":self.cur_bar["h"],"l":self.cur_bar["l"],"c":self.cur_bar["c"],"v":self.cur_bar["v"]} if self.cur_bar else None
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
        for b in d.get("bars",[]):s.bars.append({"ts":b["ts"],"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"],"v":b["v"]})
        cb=d.get("cur_bar")
        s.cur_bar={"ts":cb["ts"],"o":cb["o"],"h":cb["h"],"l":cb["l"],"c":cb["c"],"v":cb["v"]} if cb else None
        s.vol_profile=defaultdict(int)
        for k,v in d.get("vol_profile",{}).items():s.vol_profile[float(k)]=v
        s.tape=d.get("tape",[]);s.cum_pv=d.get("cum_pv",0);s.cum_v=d.get("cum_v",0);s.cum_p2v=d.get("cum_p2v",0)
        s.vwap=d.get("vwap",0);s.vwap_std=d.get("vwap_std",0)
        s.open=d.get("open",0);s.high=d.get("high",0);s.low=d.get("low",999999)
        s.trade_count=d.get("trade_count",0);s.total_volume=d.get("total_volume",0)
        s.tick_count=d.get("tick_count",0);s.bid=d.get("bid",0);s.ask=d.get("ask",0);s.last=d.get("last",0)
        return s

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
        print(f"\n  [SESSION] Fresh RTH session started for {today}")
    return current_session

def save_current():
    if current_session:current_session.save()

def list_sessions():
    return sorted([os.path.basename(f).replace(".json","") for f in glob.glob(os.path.join(DATA_DIR,"*.json"))],reverse=True)

def saver():
    import time
    while True:
        time.sleep(30)
        try:save_current()
        except:pass
threading.Thread(target=saver,daemon=True).start()
atexit.register(save_current)

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

            # Check if we already have this day saved with bars
            existing=Session.load(date_str)
            if existing and len(existing.bars)>100 and not is_today:
                print(f"  {date_str}: {len(existing.bars)} bars (cached)")
                continue

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
                api_bars=data.get("bars",[])

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

class VWAPBot:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10.0)
        self.token = None; self.headers = {}
        self.account_id = None; self.contract_id = None
        self.pos = None; self.trades_today = 0
        self.ready = False
        self.trade_history = []
        self.total_pnl = 0
        self.daily_pnl = 0
        self.peak_pnl = 0
        self.max_dd = 0
        self.gutter_win = False
        self.gutter_loss = False

    async def setup(self, token):
        self.token = token
        self.headers = {"Authorization": f"Bearer {self.token}"}
        r = await self.client.post(f"{API}/Account/search", json={"onlyActiveAccounts": True}, headers=self.headers)
        accts = r.json().get("accounts", [])
        prac = [a for a in accts if "PRAC" in a.get("name", "").upper() and a.get("canTrade")]
        if not prac: prac = [a for a in accts if a.get("canTrade")]
        if prac: self.account_id = prac[0]["id"]
        r = await self.client.post(f"{API}/Contract/available", json={"live": False}, headers=self.headers)
        es = [c for c in r.json().get("contracts", []) if c.get("symbolId") == "F.US.EP" or c.get("description", "").startswith("E-Mini S&P")]
        if es: self.contract_id = es[0]["id"]
        if self.account_id and self.contract_id:
            self.ready = True
            print(f"  [BOT] Ready. Acct: {self.account_id} | Contract: {self.contract_id}")

    async def place_order(self, action: str, size: int):
        side = 0 if action.upper() == "BUY" else 1
        p = {"accountId": self.account_id, "contractId": self.contract_id, "type": 2, "side": side, "size": size, "customTag": f"B_{int(datetime.now().timestamp())}"}
        print(f"\n  [BOT] ORDER -> {action} {size} @ MKT")
        if DRY_RUN: print(f"  [DRY RUN] {p}"); return True
        r = await self.client.post(f"{API}/Order/place", json=p, headers=self.headers)
        res = r.json()
        if res.get("success"): print("  [SUCCESS] Order placed.")
        else: print(f"  [FAILED] {res}")
        return res.get("success", False)

    async def close_position(self, price, reason, bar_idx):
        ticks = (price - self.pos["ep"]) / TICK_SIZE if self.pos["dir"] == "LONG" else (self.pos["ep"] - price) / TICK_SIZE
        pnl = ticks * TICK_VALUE * CONTRACTS
        self.total_pnl += pnl
        self.daily_pnl += pnl
        print(f"\n  [BOT] CLOSING {self.pos['dir']} @ {price:.2f} ({reason}) | PnL: ${pnl:.2f} | Total: ${self.total_pnl:.2f}")
        act = "SELL" if self.pos["dir"] == "LONG" else "BUY"
        await self.place_order(act, CONTRACTS)
        self.trade_history.append({"action": "EXIT", "dir": self.pos["dir"], "price": price, "pnl": pnl, "bar_idx": bar_idx})
        self.pos = None

    async def check_logic(self, price, vwap, vpoc, std, vwap_z, bar_idx, rth):
        if not self.ready or not vwap or not std or bar_idx < 15: return
        if not rth:
            if self.pos: await self.close_position(price, "END OF RTH", bar_idx)
            return
        
        target = vpoc if TARGET_MODE == "volume" else round(vwap / TICK_SIZE) * TICK_SIZE
        if not target: return
        target_z = (price - target) / std if std >= TICK_SIZE else 0

        if self.pos:
            bt = bar_idx - self.pos["bar_idx"]
            
            if self.pos["dir"] == "LONG":
                if self.gutter_win:
                    gut_tp = self.pos["ep"] + ((GUTTER_GOAL - self.daily_pnl) / (CONTRACTS*TICK_VALUE)) * TICK_SIZE
                    if price >= gut_tp: await self.close_position(gut_tp, "GUTTER WIN", bar_idx); return
                if self.gutter_loss:
                    gut_sl = self.pos["ep"] - ((self.daily_pnl + GUTTER_DD) / (CONTRACTS*TICK_VALUE)) * TICK_SIZE
                    if price <= gut_sl: await self.close_position(gut_sl, "GUTTER LOSS", bar_idx); return
                
                if price > self.pos["bp"]: self.pos["bp"] = price

                ur = (self.pos["bp"] - self.pos["ep"]) / TICK_SIZE
                cur = (price - self.pos["ep"]) / TICK_SIZE
                if price <= self.pos["sl"]: await self.close_position(price, "STOP LOSS", bar_idx); return
                if target and price >= target and cur >= EXIT_MIN_TICKS: await self.close_position(price, "TARGET", bar_idx); return
                if ur >= TRAIL_ACTIVATE:
                    tl = round((self.pos["bp"] - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl > self.pos["sl"]: self.pos["sl"] = tl; print(f"  [BOT] Trail Stop -> {tl:.2f}")
                if ur >= 10 and self.pos["sl"] < self.pos["ep"]: self.pos["sl"] = self.pos["ep"]; print(f"  [BOT] Stop -> BE {self.pos['ep']:.2f}")
                if bt > TIME_STOP_MINS and cur > -4: await self.close_position(price, "TIME STOP", bar_idx); return
            else:
                if self.gutter_win:
                    gut_tp = self.pos["ep"] - ((GUTTER_GOAL - self.daily_pnl) / (CONTRACTS*TICK_VALUE)) * TICK_SIZE
                    if price <= gut_tp: await self.close_position(gut_tp, "GUTTER WIN", bar_idx); return
                if self.gutter_loss:
                    gut_sl = self.pos["ep"] + ((self.daily_pnl + GUTTER_DD) / (CONTRACTS*TICK_VALUE)) * TICK_SIZE
                    if price >= gut_sl: await self.close_position(gut_sl, "GUTTER LOSS", bar_idx); return

                if price < self.pos["bp"]: self.pos["bp"] = price
                ur = (self.pos["ep"] - self.pos["bp"]) / TICK_SIZE
                cur = (self.pos["ep"] - price) / TICK_SIZE
                if price >= self.pos["sl"]: await self.close_position(price, "STOP LOSS", bar_idx); return
                if target and price <= target and cur >= EXIT_MIN_TICKS: await self.close_position(price, "TARGET", bar_idx); return
                if ur >= TRAIL_ACTIVATE:
                    tl = round((self.pos["bp"] + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl < self.pos["sl"]: self.pos["sl"] = tl; print(f"  [BOT] Trail Stop -> {tl:.2f}")
                if ur >= 10 and self.pos["sl"] > self.pos["ep"]: self.pos["sl"] = self.pos["ep"]; print(f"  [BOT] Stop -> BE {self.pos['ep']:.2f}")
                if bt > TIME_STOP_MINS and cur > -4: await self.close_position(price, "TIME STOP", bar_idx); return
        else:
            if not BOT_ACTIVE or bar_idx < 45 or bar_idx > 360 or self.trades_today >= MAX_TRADES: return
            if self.gutter_win and round(self.daily_pnl, 2) >= GUTTER_GOAL: return
            if self.gutter_loss and round(self.daily_pnl, 2) <= -GUTTER_DD: return
            z_abs = abs(target_z)
            if z_abs >= Z_THRESH:
                dist = abs(price - target)
                sd = max(min(dist * STOP_RATIO, MAX_STOP_TICKS * TICK_SIZE), MIN_STOP_TICKS * TICK_SIZE)
                if target_z < 0:
                    sl = round((price - sd) / TICK_SIZE) * TICK_SIZE
                    tp = target + 4 * TICK_SIZE
                    print(f"\n  [BOT] ENTRY LONG @ {price:.2f} | Tgt: {tp:.2f} | VPOC Z={abs(target_z):.1f} | VWAP Z={vwap_z:.1f}")
                    if await self.place_order("BUY", CONTRACTS):
                        self.pos = {"dir": "LONG", "ep": price, "sl": sl, "tp": tp, "bp": price, "bar_idx": bar_idx}
                        self.trades_today += 1
                        self.trade_history.append({"action": "ENTER", "dir": "LONG", "price": price, "bar_idx": bar_idx})
                elif target_z > 0:
                    sl = round((price + sd) / TICK_SIZE) * TICK_SIZE
                    tp = target - 4 * TICK_SIZE
                    print(f"\n  [BOT] ENTRY SHORT @ {price:.2f} | Tgt: {tp:.2f} | VPOC Z={abs(target_z):.1f} | VWAP Z={vwap_z:.1f}")
                    if await self.place_order("SELL", CONTRACTS):
                        self.pos = {"dir": "SHORT", "ep": price, "sl": sl, "tp": tp, "bp": price, "bar_idx": bar_idx}
                        self.trades_today += 1
                        self.trade_history.append({"action": "ENTER", "dir": "SHORT", "price": price, "bar_idx": bar_idx})

global_bot = VWAPBot()

def run_backtest(bars, gutter_win=False, gutter_loss=False):
    cum_pv = 0; cum_v = 0; cum_p2v = 0; vp_dict = defaultdict(float); vpoc = 0
    pos = None; trades = []; trades_today = 0; total_pnl = 0
    peak_pnl = 0; max_dd = 0
    daily_pnl = 0
    
    for i, b in enumerate(bars):
        h, l, c, v = b["h"], b["l"], b["c"], b["v"]
        tp = (h+l+c)/3
        cum_pv += tp*v; cum_v += v; cum_p2v += (tp**2)*v
        vwap = cum_pv/cum_v if cum_v > 0 else 0
        var = (cum_p2v/cum_v) - (vwap**2) if cum_v > 0 else 0
        std = var**0.5 if var > 0 else 0
        
        lo_lv = round(l*4)/4; hi_lv = round(h*4)/4
        n_levels = max(1, int((hi_lv - lo_lv)/0.25) + 1)
        vpl = v / n_levels
        lv = lo_lv
        while lv <= hi_lv:
            vp_dict[lv] += vpl; lv += 0.25
        if vp_dict: vpoc = max(vp_dict, key=vp_dict.get)
        
        if i < 15: continue
        target = vpoc if TARGET_MODE == "volume" else round(vwap/TICK_SIZE)*TICK_SIZE
        if not target: continue
        target_z = (c - target)/std if std >= TICK_SIZE else 0
        
        if pos:
            epnl = 0
            ticks = (c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE
            epnl = ticks * TICK_VALUE * CONTRACTS
            cur_eq = total_pnl + epnl
            cur_dd = peak_pnl - cur_eq if cur_eq < peak_pnl else 0

            bt = i - pos["bar_idx"]
            if pos["dir"] == "LONG":
                if gutter_win:
                    gut_tp = pos["ep"] + ((GUTTER_GOAL - daily_pnl)/(CONTRACTS*TICK_VALUE))*TICK_SIZE
                    if h >= gut_tp:
                        ticks = (gut_tp - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                        trades.append({"action":"EXIT","dir":"LONG","price":gut_tp,"pnl":pnl,"bar_idx":i}); pos = None; continue
                if gutter_loss:
                    gut_sl = pos["ep"] - ((daily_pnl + GUTTER_DD)/(CONTRACTS*TICK_VALUE))*TICK_SIZE
                    if l <= gut_sl:
                        ticks = (gut_sl - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                        trades.append({"action":"EXIT","dir":"LONG","price":gut_sl,"pnl":pnl,"bar_idx":i}); pos = None; continue
                if l <= pos["sl"]: 
                    ticks = (pos["sl"] - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"LONG","price":pos["sl"],"pnl":pnl,"bar_idx":i}); pos = None; continue
                if c > pos["bp"]: pos["bp"] = c
                ur = (pos["bp"] - pos["ep"])/TICK_SIZE; cur = (c - pos["ep"])/TICK_SIZE
                if target and c >= target and cur >= EXIT_MIN_TICKS:
                    ticks = (c - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"LONG","price":c,"pnl":pnl,"bar_idx":i}); pos = None; continue
                if ur >= TRAIL_ACTIVATE:
                    tl = round((pos["bp"] - TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl > pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] < pos["ep"]: pos["sl"] = pos["ep"]
                if bt > TIME_STOP_MINS and cur > -4:
                    ticks = (c - pos["ep"])/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"LONG","price":c,"pnl":pnl,"bar_idx":i}); pos = None; continue
            else:
                if gutter_win:
                    gut_tp = pos["ep"] - ((GUTTER_GOAL - daily_pnl)/(CONTRACTS*TICK_VALUE))*TICK_SIZE
                    if l <= gut_tp:
                        ticks = (pos["ep"] - gut_tp)/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                        trades.append({"action":"EXIT","dir":"SHORT","price":gut_tp,"pnl":pnl,"bar_idx":i}); pos = None; continue
                if gutter_loss:
                    gut_sl = pos["ep"] + ((daily_pnl + GUTTER_DD)/(CONTRACTS*TICK_VALUE))*TICK_SIZE
                    if h >= gut_sl:
                        ticks = (pos["ep"] - gut_sl)/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                        trades.append({"action":"EXIT","dir":"SHORT","price":gut_sl,"pnl":pnl,"bar_idx":i}); pos = None; continue
                if h >= pos["sl"]:
                    ticks = (pos["ep"] - pos["sl"])/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"SHORT","price":pos["sl"],"pnl":pnl,"bar_idx":i}); pos = None; continue
                if c < pos["bp"]: pos["bp"] = c
                ur = (pos["ep"] - pos["bp"])/TICK_SIZE; cur = (pos["ep"] - c)/TICK_SIZE
                if target and c <= target and cur >= EXIT_MIN_TICKS:
                    ticks = (pos["ep"] - c)/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"SHORT","price":c,"pnl":pnl,"bar_idx":i}); pos = None; continue
                if ur >= TRAIL_ACTIVATE:
                    tl = round((pos["bp"] + TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl < pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                if bt > TIME_STOP_MINS and cur > -4:
                    ticks = (pos["ep"] - c)/TICK_SIZE; pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action":"EXIT","dir":"SHORT","price":c,"pnl":pnl,"bar_idx":i}); pos = None; continue
        else:
            if not BOT_ACTIVE or i < 45 or i > 360 or trades_today >= MAX_TRADES: continue
            if gutter_win and round(daily_pnl, 2) >= GUTTER_GOAL: continue
            if gutter_loss and round(daily_pnl, 2) <= -GUTTER_DD: continue
            if abs(target_z) >= Z_THRESH:
                dist = abs(c - target)
                sd = max(min(dist * STOP_RATIO, MAX_STOP_TICKS * TICK_SIZE), MIN_STOP_TICKS * TICK_SIZE)
                if target_z < 0:
                    sl = round((c - sd)/TICK_SIZE)*TICK_SIZE; tp = target + 4*TICK_SIZE
                    pos = {"dir":"LONG","ep":c,"sl":sl,"tp":tp,"bp":c,"bar_idx":i}
                    trades.append({"action":"ENTER","dir":"LONG","price":c,"bar_idx":i}); trades_today += 1
                elif target_z > 0:
                    sl = round((c + sd)/TICK_SIZE)*TICK_SIZE; tp = target - 4*TICK_SIZE
                    pos = {"dir":"SHORT","ep":c,"sl":sl,"tp":tp,"bp":c,"bar_idx":i}
                    trades.append({"action":"ENTER","dir":"SHORT","price":c,"bar_idx":i}); trades_today += 1
        
        cur_eq = total_pnl
        if pos:
            cur_ticks = (c - pos["ep"])/TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c)/TICK_SIZE
            cur_eq += cur_ticks * TICK_VALUE * CONTRACTS
        if cur_eq > peak_pnl: peak_pnl = cur_eq
        if peak_pnl - cur_eq > max_dd: max_dd = peak_pnl - cur_eq
    
    if pos:
        c = bars[-1]["c"]
        ticks = (c - pos["ep"])/TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c)/TICK_SIZE
        pnl = ticks*TICK_VALUE*CONTRACTS; total_pnl += pnl
        trades.append({"action":"EXIT","dir":pos["dir"],"price":c,"pnl":pnl,"bar_idx":len(bars)-1})
        
    return {"total_pnl": total_pnl, "daily_pnl": total_pnl, "trades": trades_today, "max_trades": MAX_TRADES, "pos": None, "open_pnl": 0, "max_dd": max_dd, "gutter_win": gutter_win, "gutter_loss": gutter_loss}, trades

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
                return json.loads(response.read().decode("utf-8")).get("bars", [])

        bars = _get(CONTRACT)
        if not bars and day < datetime.strptime("2026-03-14", "%Y-%m-%d").date():
            bars = _get("CON.F.US.EP.H26")

        if not bars: return None
        bars.reverse()
        s = Session(d_str)
        for b in bars:
            s.add_bar(b["o"], b["h"], b["l"], b["c"], b["v"], b["t"])
        return s
    except Exception as e:
        print(f"\n  Sync fetch error for {d_str}: {e}")
        return None

async def auth():
    u=os.environ.get("PROJECT_X_USERNAME","");k=os.environ.get("PROJECT_X_API_KEY","")
    if not u or not k:print("  Set env vars");return None
    async with httpx.AsyncClient(timeout=30) as h:
        r=await h.post(f"{API}/Auth/loginKey",json={"userName":u,"apiKey":k})
        d=r.json()
        if d.get("success") and d.get("token"):return d["token"]
        print(f"  Auth failed: {d}");return None

async def stream(token):
    global connected
    async with httpx.AsyncClient(timeout=30) as h:
        r=await h.post(f"{HUB}/negotiate?negotiateVersion=1&access_token={token}")
        ct=r.json().get("connectionToken",r.json().get("connectionId",""))
    ws_url=HUB.replace("https://","wss://")+f"?id={ct}&access_token={token}"
    async for ws in websockets.connect(ws_url,additional_headers={"Authorization":f"Bearer {token}"},ping_interval=15,ping_timeout=30):
        try:
            await ws.send(json.dumps({"protocol":"json","version":1})+"\x1e")
            await asyncio.wait_for(ws.recv(),timeout=10)
            with LOCK:connected=True
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
        except websockets.ConnectionClosed:print("\n  Reconnecting...");connected=False;await asyncio.sleep(3)
        except Exception as e:print(f"\n  {e}");connected=False;await asyncio.sleep(3)

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
    for td in dicts:
        p=td.get("price")
        if p: live_last=p
    if not rth:return
    with LOCK:
        sess=get_or_create_session()
        for td in dicts:
            price=td.get("price",0);vol=td.get("volume",0);sr=td.get("side","")
            if not price or price<=0:continue
            if vol<=0:vol=1
            if isinstance(sr,int):s="BUY" if sr==1 else("SELL" if sr==2 else str(sr))
            elif isinstance(sr,str):s=sr.upper();s="BUY" if s in("B","BUY")else("SELL" if s in("S","SELL","A")else s)
            else:s=str(sr)
            sess.add_trade(price,vol,s,now)
        cz = round((live_last - sess.vwap)/sess.vwap_std, 2) if sess.vwap_std > 0 else 0

    await global_bot.check_logic(live_last, sess.vwap, sess.vpoc, sess.vwap_std, cz, len(sess.bars), rth)

async def on_quote(args,rth=True):
    global live_bid,live_ask,live_last
    now=datetime.now(timezone.utc)
    for item in args:
        if isinstance(item,dict):
            b=item.get("bestBid");a=item.get("bestAsk")
            if b:live_bid=b
            if a:live_ask=a
            if live_bid and live_ask:live_last=(live_bid+live_ask)/2
    if not rth:return
    with LOCK:
        sess=get_or_create_session()
        for item in args:
            if isinstance(item,dict):
                sess.add_quote(item.get("bestBid"),item.get("bestAsk"),now)
    with LOCK:
        print(f"\r  {sess.bid:>8.2f}/{sess.ask:>8.2f} Last:{sess.last:>8.2f} Bars:{len(sess.bars)} Tr:{sess.trade_count} V:{sess.total_volume}  ",end="",flush=True)

# ============================================================
# HTML — same static layout as v5/v6
# ============================================================
HTML=r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>ES</title>
<style>
body{background:#111;color:#eee;font:12px/1.5 monospace;padding:16px;margin:0;}
table{border-collapse:collapse;width:100%;margin:4px 0;}td,th{padding:3px 8px;text-align:left;border-bottom:1px solid #222;}
th{color:#666;font-size:10px;}.g{color:#0b6;}.r{color:#e33;}.b{color:#48f;}.d{color:#555;}.y{color:#da0;}
h3{color:#777;font-size:11px;margin:12px 0 2px;border-bottom:1px solid #222;padding-bottom:2px;}
pre{background:#0a0a0a;padding:6px;border:1px solid #1a1a1a;font-size:10px;margin:4px 0;white-space:pre-wrap;}
.cbox{background:#0a0a0a;border:1px solid #1a1a1a;margin:4px 0;}.cbox canvas{display:block;}
.tw{display:flex;margin:4px 0;}.tw .cbox{flex:1;min-width:0;}.tw .pbox{width:160px;background:#0a0a0a;border:1px solid #1a1a1a;border-left:none;}.tw .pbox canvas{display:block;}
.vb{display:inline-block;height:10px;background:#48f;margin-left:4px;vertical-align:middle;}
.nav-day{display:flex;align-items:center;gap:8px;margin:8px 0;padding:8px;background:#0a0a0a;border:1px solid #1a1a1a;border-radius:4px;flex-wrap:wrap;}
.nav-day button{background:#222;color:#eee;border:1px solid #333;padding:4px 12px;cursor:pointer;font:12px monospace;border-radius:3px;}
.nav-day button:hover{background:#333;}.nav-day button.on{background:#48f;border-color:#48f;}
.nav-day .dt{font-size:14px;font-weight:bold;min-width:110px;text-align:center;}
.nav-day .days{display:flex;gap:3px;margin-left:auto;}
.nav-day .dbtn{padding:2px 8px;font-size:10px;background:#1a1a1a;border:1px solid #222;color:#888;cursor:pointer;border-radius:2px;}
.nav-day .dbtn:hover,.nav-day .dbtn.on{color:#eee;border-color:#48f;}
</style></head><body>
<div id="status">Loading...</div>
<div class="nav-day">
  <button onclick="prev()">◄</button>
  <div class="dt" id="day-label">—</div>
  <button onclick="next()">►</button>
  <button onclick="goLive()" id="btn-live" class="on">Live</button>
  <button onclick="toggleGW()" id="btn-gw" class="">Gutter Win ($1.5k)</button>
  <button onclick="toggleGL()" id="btn-gl" class="">Gutter Loss ($1.9k)</button>
  <input type="date" id="cal" style="background:#222;color:#eee;border:1px solid #333;padding:2px;border-radius:3px;font-family:monospace;font-size:11px;" onchange="goD(this.value)">
  <div class="days" id="days"></div>
</div>
<h3 id="hl">ES —</h3>
<div id="live247" style="background:#0a0a0a;border:1px solid #1a1a1a;margin-bottom:8px;padding:6px;font-size:14px;display:flex;gap:16px;">
  <span style="color:#888">Global Live (24/7):</span>
  <span>Last: <span id="glb-last" style="color:#eee;font-weight:bold">—</span></span>
  <span>Bid: <span id="glb-bid" class="g">—</span></span>
  <span>Ask: <span id="glb-ask" class="r">—</span></span>
</div>
<div id="bot-pl" style="background:#0a0a0a;border:1px solid #1a1a1a;margin-bottom:8px;padding:6px;font-size:14px;display:flex;gap:16px;">
  <span style="color:#888">VWAP Bot P&L:</span>
  <span>Daily: <span id="b-day" class="d">$0.00</span></span>
  <span>Open: <span id="b-opn" class="d">$0.00</span></span>
  <span>Max DD: <span id="b-dd" class="r">$0.00</span></span>
  <span>Total: <span id="b-tot" class="d">$0.00</span></span>
  <span>Trades: <span id="b-trd">0</span> / <span id="b-mtrd">4</span></span>
  <span>Pos: <span id="b-pos" class="d">FLAT</span></span>
</div>
<table><tr><th>Last</th><th>Bid</th><th>Ask</th><th>Sprd</th><th>VWAP</th><th>σ</th><th>Z</th><th>Open</th><th>High</th><th>Low</th><th>VPOC</th></tr>
<tr><td id="v-last" style="font-size:18px;font-weight:bold">—</td><td id="v-bid" class="g">—</td><td id="v-ask" class="r">—</td><td id="v-sprd">—</td><td id="v-vwap" class="b">—</td><td id="v-std">—</td><td id="v-z">—</td><td id="v-open">—</td><td id="v-high" class="g">—</td><td id="v-low" class="r">—</td><td id="v-vpoc" class="y">—</td></tr></table>
<h3 id="lc">1-Min</h3><div class="cbox"><canvas id="c1"></canvas></div>
<h3 id="lp" style="display:none">VWAP Bot P&L Curve</h3><div class="cbox" style="display:none"><canvas id="c-pnl" style="height:120px"></canvas></div>
<h3>Tick + Profile</h3><div class="tw"><div class="cbox"><canvas id="tc"></canvas></div><div class="pbox"><canvas id="vp"></canvas></div></div>
<h3 id="lb">Bars</h3><table><tr><th>O</th><th>H</th><th>L</th><th>C</th><th>Vol</th><th>Rng</th></tr><tbody id="bb"></tbody></table>
<h3>Volume Profile</h3><table><tr><th>Price</th><th>Vol</th><th></th></tr><tbody id="vb"></tbody></table>
<h3 id="lt">Tape</h3><table><tr><th>Time</th><th>Price</th><th>Vol</th><th>Side</th></tr><tbody id="tb"></tbody></table>
<pre id="dbg"></pre>
<script>
const F=(n,d=2)=>(n!=null&&isFinite(n)&&n!==0)?Number(n).toFixed(d):'—';
const $=id=>document.getElementById(id);
let vd=null,sessions=[];
function prev(){const i=sessions.indexOf(vd||sessions[0]);if(i<sessions.length-1){vd=sessions[i+1];go();}}
function next(){const i=sessions.indexOf(vd||sessions[0]);if(i>0){vd=sessions[i-1];go();}else{vd=null;go();}}
function goLive(){vd=null;go();}
function goD(d){vd=d;go();}
async function toggleGW(){await fetch('/api/toggle_gw',{method:'POST'});go();}
async function toggleGL(){await fetch('/api/toggle_gl',{method:'POST'});go();}

function candles(id,bars,H,bot_trades){
  const c=$(id);if(!c)return;const dp=devicePixelRatio||1,W=c.parentElement.offsetWidth;
  c.width=W*dp;c.height=H*dp;c.style.width=W+'px';c.style.height=H+'px';
  const x=c.getContext('2d');x.scale(dp,dp);x.clearRect(0,0,W,H);
  if(!bars||!bars.length){x.fillStyle='#333';x.font='11px monospace';x.textAlign='center';x.fillText('No data',W/2,H/2);return;}
  const pad={t:10,b:14,l:52,r:8},cW=W-pad.l-pad.r;
  const vis=bars,slot=cW/Math.max(390,bars.length),bw=Math.max(1,slot*0.8);
  const mn=Math.min(...vis.map(b=>b.l))-0.25,mx=Math.max(...vis.map(b=>b.h))+0.25,rng=mx-mn||1,cH=H-pad.t-pad.b;
  const tY=v=>pad.t+(1-(v-mn)/rng)*cH;
  x.fillStyle='#444';x.font='9px monospace';x.textAlign='right';
  for(let i=0;i<=5;i++){const v=mn+(rng/5)*i,y=tY(v);x.fillText(v.toFixed(2),pad.l-3,y+3);x.strokeStyle='#1a1a1a';x.lineWidth=.5;x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();}
  vis.forEach((b,i)=>{const cx=pad.l+i*slot+slot/2,up=b.c>=b.o,col=up?'#0b6':'#e33';
    x.strokeStyle=col;x.lineWidth=1;x.beginPath();x.moveTo(cx,tY(b.h));x.lineTo(cx,tY(b.l));x.stroke();
    const bt=tY(Math.max(b.o,b.c)),bb2=tY(Math.min(b.o,b.c));
    x.fillStyle=col;x.fillRect(cx-bw/2,bt,bw,Math.max(1,bb2-bt));});

  if(bot_trades&&bot_trades.length){
    bot_trades.forEach(t=>{
      const cx=pad.l+t.bar_idx*slot+slot/2, cy=tY(t.price);
      x.beginPath();x.arc(cx,cy,4,0,Math.PI*2);
      x.fillStyle=t.action==='ENTER'?(t.dir==='LONG'?'#0f4':'#f22'):'#da0';
      x.fill();x.strokeStyle='#fff';x.lineWidth=1;x.stroke();
    });
  }

  x.fillStyle='#555';x.font='9px monospace';x.textAlign='center';
  const lC=Math.max(2,Math.floor(cW/60)), step=Math.max(1,Math.floor(vis.length/lC));
  for(let i=0;i<vis.length;i+=step){
    if(!vis[i].ts)continue;
    let d=new Date(vis[i].ts.replace(' ','T'));
    if(!isNaN(d))x.fillText(d.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false}),pad.l+i*slot+slot/2,H-3);
  }}

function drawPnl(id,trades,H,numBars){
  const c=$(id);if(!c)return;const dp=devicePixelRatio||1,W=c.parentElement.offsetWidth;
  c.width=W*dp;c.height=H*dp;c.style.width=W+'px';c.style.height=H+'px';
  const x=c.getContext('2d');x.scale(dp,dp);x.clearRect(0,0,W,H);
  if(!trades||!trades.length)return;
  const pad={t:10,b:14,l:52,r:8},cW=W-pad.l-pad.r,slot=cW/Math.max(390,numBars);
  let pts=[{idx:0,pnl:0}], cp=0;
  trades.forEach(t=>{if(t.action==="EXIT"){cp+=t.pnl;pts.push({idx:t.bar_idx,pnl:cp});}});
  pts.push({idx:numBars,pnl:cp});
  const mn=Math.min(...pts.map(p=>p.pnl)), mx=Math.max(...pts.map(p=>p.pnl));
  let rng=Math.max(Math.abs(mx),Math.abs(mn))*1.1;if(rng===0)rng=100;
  const tY=v=>pad.t+(1-(v-(-rng))/(rng*2))*(H-pad.t-pad.b);
  const zy=tY(0);
  x.strokeStyle='#333';x.lineWidth=1;x.beginPath();x.moveTo(pad.l,zy);x.lineTo(W-pad.r,zy);x.stroke();
  x.fillStyle='#444';x.font='9px monospace';x.textAlign='right';
  x.fillText(rng.toFixed(0),pad.l-3,pad.t+8);x.fillText('0',pad.l-3,zy+3);x.fillText((-rng).toFixed(0),pad.l-3,H-pad.b-2);
  x.strokeStyle='#48f';x.lineWidth=2;x.beginPath();
  pts.forEach((p,i)=>{const cx=pad.l+p.idx*slot;if(i===0)x.moveTo(cx,tY(p.pnl));else x.lineTo(cx,tY(p.pnl));});
  x.stroke();
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
  if(ap.length<2){x.fillStyle='#333';x.font='11px monospace';x.textAlign='center';x.fillText('Waiting...',W/2,H/2);return;}
  const mn=Math.min(...ap)-0.25,mx=Math.max(...ap)+0.25,rng=mx-mn||1;
  const tY=v=>pad.t+(1-(v-mn)/rng)*cH;
  x.fillStyle='#444';x.font='9px monospace';x.textAlign='right';
  for(let i=0;i<=5;i++){const v=mn+(rng/5)*i,y=tY(v);x.fillText(v.toFixed(2),pad.l-3,y+3);x.strokeStyle='#1a1a1a';x.lineWidth=.5;x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();}
  if(tape&&tape.length>=2){const sx=cW/Math.max(tape.length-1,1);
    x.beginPath();tape.forEach((t,i)=>{const px=pad.l+i*sx,py=tY(t.price);if(!i)x.moveTo(px,py);else x.lineTo(px,py);});x.strokeStyle='#ccc';x.lineWidth=1;x.stroke();
    tape.forEach((t,i)=>{const px=pad.l+i*sx,py=tY(t.price);x.beginPath();x.arc(px,py,1.5,0,Math.PI*2);x.fillStyle=t.side==='BUY'?'#0b6':(t.side==='SELL'?'#e33':'#555');x.fill();});}
  else{x.fillStyle='#555';x.font='10px monospace';x.textAlign='center';x.fillText('No trades in tape (Live off/After hours)',pad.l+cW/2,H/2);}
  const pc=$(pid);if(!pc)return;const pW=pc.parentElement.offsetWidth;
  pc.width=pW*dp;pc.height=H*dp;pc.style.width=pW+'px';pc.style.height=H+'px';
  const p=pc.getContext('2d');p.scale(dp,dp);p.clearRect(0,0,pW,H);
  if(!prof||!prof.length)return;const inR=prof.filter(v=>v.price>=mn&&v.price<=mx);if(!inR.length)return;
  const mV=Math.max(...inR.map(v=>v.vol)),bH=Math.max(1,(cH/rng)*0.25+0.5);
  inR.forEach(v=>{const y=tY(v.price),w=(v.vol/mV)*(pW-6);p.fillStyle=v.vol===mV?'#da0':'rgba(72,136,255,0.5)';p.fillRect(2,y-bH/2,w,bH);
    if(inR.length<=30){p.fillStyle='#666';p.font='7px monospace';p.fillText(Math.round(v.vol)+'',w+4,y+3);}});
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
  $('lc').textContent=`1-Min (${bars.length})`;$('lb').textContent=`Bars (${bars.length})`;$('lt').textContent=`Tape (${tp.length})`;
  
  if(d.bot) {
    if(d.bot.daily_pnl!=null){$('b-day').textContent=`$${F(d.bot.daily_pnl)}`; $('b-day').className=d.bot.daily_pnl<0?'r':'g';}
    if(d.bot.open_pnl!=null){$('b-opn').textContent=`$${F(d.bot.open_pnl)}`; $('b-opn').className=d.bot.open_pnl<0?'r':(d.bot.open_pnl>0?'g':'d');}
    if(d.bot.max_dd!=null){$('b-dd').textContent=`-$${F(Math.abs(d.bot.max_dd))}`;}
    if(d.bot.total_pnl!=null){$('b-tot').textContent=`$${F(d.bot.total_pnl)}`; $('b-tot').className=d.bot.total_pnl<0?'r':'g';}
    if(d.bot.trades!=null){$('b-trd').textContent=d.bot.trades;}
    if(d.bot.max_trades!=null){$('b-mtrd').textContent=d.bot.max_trades;}
    if(d.bot.pos) {$('b-pos').innerHTML=`<span class="${d.bot.pos.dir==='LONG'?'g':'r'}">${d.bot.pos.dir}</span> @ ${F(d.bot.pos.ep)}`;}
    else {$('b-pos').innerHTML=`<span class="d">FLAT</span>`;}
    
    const gw = $('btn-gw');
    if(gw) gw.className = d.bot.gutter_win ? 'on' : '';
    const gl = $('btn-gl');
    if(gl) gl.className = d.bot.gutter_loss ? 'on' : '';
  }

  $('bb').innerHTML=bars.slice(-10).reverse().map(b=>`<tr><td>${F(b.o)}</td><td class="g">${F(b.h)}</td><td class="r">${F(b.l)}</td><td class="${b.c>=b.o?'g':'r'}">${F(b.c)}</td><td>${b.v}</td><td>${F(b.h-b.l)}</td></tr>`).join('');
  const tv=d.top_vol||[];
  if(tv.length){const mV=Math.max(...tv.map(v=>v.vol));
    $('vb').innerHTML=tv.slice(0,15).map(v=>{const w=Math.round((v.vol/mV)*150);return`<tr><td${v.price===d.vpoc?' class="y"':''}>${F(v.price)}</td><td>${v.vol.toLocaleString()}</td><td><span class="vb" style="width:${w}px;${v.price===d.vpoc?'background:#da0':''}"></span></td></tr>`;}).join('');}
  $('tb').innerHTML=tp.slice().reverse().slice(0,20).map(t=>`<tr><td class="d">${t.time}</td><td>${F(t.price)}</td><td>${t.vol}</td><td class="${t.side==='BUY'?'g':(t.side==='SELL'?'r':'d')}">${t.side}</td></tr>`).join('');
  let dbg='';if(d.errors?.length)dbg+=`Errors: ${d.errors.join(', ')}\n`;
  $('dbg').textContent=dbg;
  const prof=d.profile||tv;
  candles('c1',bars,260,d.bot_trades);
  $('lp').style.display=d.bot?"block":"none";
  $('c-pnl').parentElement.style.display=d.bot?"block":"none";
  if(d.bot_trades)drawPnl('c-pnl',d.bot_trades,120,bars.length);
  tickC('tc','vp',tp,prof,240,bars);
}catch(e){$('status').innerHTML='<span class="r">'+e+'</span>';}}
go();setInterval(()=>{if(!vd)go();},500);
window.addEventListener('resize',go);
</script></body></html>"""

class H2(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=="/api/data":
            with LOCK:
                sess=get_or_create_session();d=sess.to_dict();d["connected"]=connected;d["errors"]=errors[-5:];d["raw_trades"]=raw_log[-2:];d["rth"]=is_rth()
                d["live_bid"]=live_bid;d["live_ask"]=live_ask;d["live_last"]=live_last
                d["bot_trades"]=global_bot.trade_history
                epnl = 0
                if global_bot.pos:
                    ep = global_bot.pos["ep"]
                    ticks = (live_last - ep) / TICK_SIZE if global_bot.pos["dir"] == "LONG" else (ep - live_last) / TICK_SIZE
                    epnl = ticks * TICK_VALUE * CONTRACTS
                
                cur_eq = global_bot.total_pnl + epnl
                if cur_eq > global_bot.peak_pnl: global_bot.peak_pnl = cur_eq
                dd = global_bot.peak_pnl - cur_eq
                if dd > global_bot.max_dd: global_bot.max_dd = dd
                
                d["bot"] = {"total_pnl": global_bot.total_pnl, "daily_pnl": global_bot.daily_pnl, "trades": global_bot.trades_today, "max_trades": MAX_TRADES, "pos": global_bot.pos, "open_pnl": epnl, "max_dd": global_bot.max_dd, "gutter_win": global_bot.gutter_win, "gutter_loss": global_bot.gutter_loss}
                d["bot_trades"] = global_bot.trade_history
            self.jr(d)
        elif self.path.startswith("/api/day/"):
            ds=self.path.split("/api/day/")[1][:10]
            s=Session.load(ds)
            if not s and global_bot.token:
                print(f"\n  Fetching history sync for {ds}...")
                s = fetch_history_sync(global_bot.token, ds)
                if s: s.save()
            d = s.to_dict() if s else {"error":"No data","date":ds}
            if s and len(s.bars) > 15:
                bstate, btrades = run_backtest(s.bars, gutter_win=global_bot.gutter_win, gutter_loss=global_bot.gutter_loss)
                d["bot"] = bstate
                d["bot_trades"] = btrades
            self.jr(d)
        elif self.path=="/api/sessions":
            self.jr({"dates":list_sessions()})
        else:
            self.send_response(200);self.send_header("Content-Type","text/html");self.end_headers();self.wfile.write(HTML.encode())
            
    def do_POST(self):
        if self.path=="/api/toggle_gw":
            global_bot.gutter_win = not getattr(global_bot, "gutter_win", False)
            self.jr({"success": True})
        elif self.path=="/api/toggle_gl":
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
    print("="*50);print("  ES LIVE v7 — Historical + Live + VWAP Bot");print("="*50)
    token=await auth()
    if not token:return
    print("  ✓ Auth\n")
    print(f"  Backfilling {DAYS_BACK} days of RTH bars...")
    await backfill(token)
    await global_bot.setup(token)
    get_or_create_session()
    serve(8080);print("  ✓ http://localhost:8080");print(f"  ✓ {CONTRACT}\n")
    await stream(token)

if __name__=="__main__":
    try:asyncio.run(main())
    except KeyboardInterrupt:print("\n  Saving...");save_current();print("  Done.")