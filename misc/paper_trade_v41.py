"""
TOPSTEP PAPER TRADING BOT v4 — WITH WEB DASHBOARD
====================================================
Everything from v3 + real-time web dashboard you can view from any browser.

NEW:
  - Built-in web server on port 8080
  - Live dashboard at http://localhost:8080
  - JSON API at http://localhost:8080/api/status
  - Auto-refreshes every 10 seconds
  - Mobile friendly
  - Works through ngrok/tunnels for remote viewing

DEPLOY FREE:
  - Oracle Cloud free tier (always-free ARM VM)
  - GitHub Codespaces (120 core-hours/month free)
  - Google Cloud free tier (e2-micro, 720 hrs/month)

USAGE:
  pip install yfinance pandas numpy pytz

  python3 paper_trade_v4.py --symbol SPY                # local
  python3 paper_trade_v4.py --symbol SPY --port 8080    # custom port
"""

import numpy as np
import pandas as pd
import json
import csv
import os
import sys
import signal as signal_mod
import argparse
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, time as dtime, timedelta
from dataclasses import dataclass
from collections import defaultdict
from typing import Optional
import time as time_module

try:
    import pytz
    ET = pytz.timezone("America/New_York")
except ImportError:
    print("pip install pytz"); sys.exit(1)

try:
    import yfinance as yf
except ImportError:
    print("pip install yfinance"); sys.exit(1)

TICK_SIZE = 0.25
TICK_VALUE = 12.50

# ============================================================
# GLOBAL STATE (shared with web server)
# ============================================================
DASHBOARD_STATE = {
    "last_update": "",
    "symbol": "SPY",
    "bar": 0,
    "price": 0,
    "vwap": 0,
    "std": 0,
    "z": None,
    "poc": 0,
    "vpoc": 0,
    "poc_mig": 0,
    "vol_quality": 0,
    "market_open": False,
    "strategies": {},
    "recent_trades": [],
    "recent_signals": [],
    "previous_session": {},  # {strat_name: {total_pnl, trades, ...}}
    "symbols": None,  # set when multi-symbol: [{symbol, data}]
}
STATE_LOCK = threading.Lock()

# ============================================================
# WEB DASHBOARD
# ============================================================



DASHBOARD_FILE = None  # loaded at startup

def load_dashboard_html():
    """Load dashboard.html from same directory as script."""
    global DASHBOARD_FILE
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "dashboard.html"),
        os.path.join(os.getcwd(), "dashboard.html"),
        "dashboard.html",
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, 'r') as f:
                DASHBOARD_FILE = f.read()
            print(f"  Loaded dashboard from: {path}")
            return
    print("  ⚠ dashboard.html not found, using fallback")
    DASHBOARD_FILE = "<html><body style='background:#0d1117;color:white;font-family:monospace;padding:40px'><h1>Dashboard file missing</h1><p>Place dashboard.html in the same directory as paper_trade_v4.py</p><p>API available at <a href='/api/status' style='color:#4d9bff'>/api/status</a></p></body></html>"

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    
    def do_GET(self):
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            with STATE_LOCK:
                self.wfile.write(json.dumps(DASHBOARD_STATE, default=str).encode())
        elif self.path == '/api/trades':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            with STATE_LOCK:
                self.wfile.write(json.dumps(DASHBOARD_STATE.get("recent_trades", []),
                                           default=str).encode())
        elif self.path == '/api/signals':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            with STATE_LOCK:
                self.wfile.write(json.dumps(DASHBOARD_STATE.get("recent_signals", []),
                                           default=str).encode())
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(DASHBOARD_FILE.encode())


def start_web_server(port=8080):
    import socket
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True
        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            super().server_bind()
    server = ReusableHTTPServer(('0.0.0.0', port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def update_dashboard(result, strategies, data_source, prev_session=None):
    """Update global dashboard state from latest bar result."""
    with STATE_LOCK:
        DASHBOARD_STATE["last_update"] = datetime.now(ET).strftime("%H:%M:%S ET")
        DASHBOARD_STATE["symbol"] = data_source
        DASHBOARD_STATE["bar"] = result.get("bar", 0)
        DASHBOARD_STATE["price"] = result.get("price", 0)
        DASHBOARD_STATE["vwap"] = result.get("vwap", 0)
        DASHBOARD_STATE["std"] = result.get("std", 0)
        DASHBOARD_STATE["z"] = result.get("z")
        DASHBOARD_STATE["poc"] = result.get("poc", 0)
        DASHBOARD_STATE["vpoc"] = result.get("vpoc", 0)
        DASHBOARD_STATE["poc_mig"] = result.get("poc_mig", 0)
        DASHBOARD_STATE["vol_quality"] = result.get("vol_quality", 0)
        DASHBOARD_STATE["market_open"] = is_market_hours()
        DASHBOARD_STATE["previous_session"] = prev_session or {}
        
        for name, state in strategies.items():
            DASHBOARD_STATE["strategies"][name] = {
                "total_pnl": state.total_pnl,
                "daily_pnl": state.daily_pnl,
                "equity_high": state.equity_high,
                "trailing_floor": state.trailing_floor,
                "cushion": state.cushion,
                "total_trades": state.total_trades,
                "wins": state.wins,
                "losses": state.losses,
                "win_rate": state.win_rate,
                "avg_rr": state.avg_rr,
                "profit_factor": state.profit_factor,
                "day_count": state.day_count,
                "winning_days": state.winning_days,
                "day_trades": state.day_trades,
                "max_trades": 4,
                "passed": state.passed,
                "blown": state.blown,
                "blown_reason": state.blown_reason,
                "remaining_daily": state.remaining_daily,
                "daily_locked": state.daily_locked,
                "position": state.position,
                "best_day_pnl": state.best_day_pnl,
                "effective_target": state.effective_target,
                "consistency_ok": state.consistency_ok,
                "to_target": state.to_target,
            }


# ============================================================
# INDICATORS (same as v3)
# ============================================================

class TPO:
    def __init__(self):
        self.hist=defaultdict(int);self.cp=-1;self.cls=set();self.poc=None;self.poc_hist=[]
    def reset(self):
        self.hist=defaultdict(int);self.cp=-1;self.cls=set();self.poc=None;self.poc_hist=[]
    def update(self,idx,hi,lo,close):
        p=idx//30
        if p!=self.cp:
            if self.cp>=0:
                for lv in self.cls:self.hist[lv]+=1
            self.cp=p;self.cls=set()
        lv=round(lo);h=round(hi)
        while lv<=h:self.cls.add(lv);lv+=1
        tmp=dict(self.hist)
        for lv in self.cls:tmp[lv]=tmp.get(lv,0)+1
        if tmp:
            mx=max(tmp.values())
            self.poc=min([p for p,c in tmp.items() if c==mx],key=lambda p:abs(p-close))
        self.poc_hist.append(self.poc);return self.poc
    def migration_speed(self,lb=20):
        if len(self.poc_hist)<lb:return 0
        r=[p for p in self.poc_hist[-lb:] if p is not None]
        if len(r)<2:return 0
        return abs(r[-1]-r[0])/len(r)

class VolPOC:
    def __init__(self):self.vh=defaultdict(float);self.vpoc=None
    def reset(self):self.vh=defaultdict(float);self.vpoc=None
    def update(self,hi,lo,close,vol):
        if vol<=0:return self.vpoc
        lo_b=round(lo);hi_b=round(hi);n=max(1,int(hi_b-lo_b)+1);vpl=vol/n;lv=lo_b
        while lv<=hi_b:self.vh[lv]+=vpl;lv+=1
        if self.vh:self.vpoc=max(self.vh,key=self.vh.get)
        return self.vpoc

class VWAPCalc:
    def __init__(self):
        self.ctv=0;self.cv=0;self.ct2v=0;self.vwap=None;self.std=None;self.n=0
        self.zvb=0;self.tb=0
    def reset(self):
        self.ctv=0;self.cv=0;self.ct2v=0;self.vwap=None;self.std=None;self.n=0
        self.zvb=0;self.tb=0
    def update(self,hi,lo,close,vol):
        self.tb+=1
        if vol<=0:self.zvb+=1;return self.vwap,self.std
        tp=(hi+lo+close)/3;self.ctv+=tp*vol;self.cv+=vol;self.ct2v+=(tp**2)*vol;self.n+=1
        if self.cv>0:
            self.vwap=self.ctv/self.cv;self.std=np.sqrt(max(self.ct2v/self.cv-self.vwap**2,0))
        return self.vwap,self.std
    def z_from_level(self,price,level):
        if self.std is None or self.std<TICK_SIZE or self.n<15:return None
        return (price-level)/self.std
    @property
    def volume_quality(self):
        return (self.tb-self.zvb)/self.tb if self.tb>0 else 1.0

class SessionATR:
    def __init__(self):self.ranges=[];self.pc=None;self.session_atrs=[];self.rolling=None
    def reset_day(self):
        if self.ranges and len(self.ranges)>=10:
            self.session_atrs.append(np.mean(self.ranges[-30:]))
            if len(self.session_atrs)>=5:self.rolling=np.mean(self.session_atrs[-20:])
        self.ranges=[];self.pc=None
    def update(self,hi,lo,close):
        tr=max(hi-lo,abs(hi-self.pc) if self.pc else hi-lo,abs(lo-self.pc) if self.pc else hi-lo)
        self.ranges.append(tr);self.pc=close
    def atr(self):return np.mean(self.ranges[-30:]) if len(self.ranges)>=10 else None

# ============================================================
# STRATEGY STATE & LOGIC (same as v3, imported for brevity)
# ============================================================

@dataclass
class StrategyState:
    name:str
    # Account params (50K Combine defaults)
    profit_target:float=3000; max_dd:float=2000; daily_limit:float=1000
    max_position:int=5  # 5 standard contracts max for 50K
    # P&L tracking
    total_pnl:float=0; equity_high:float=0; trailing_floor:float=-2000
    daily_pnl:float=0; day_count:int=0; winning_days:int=0; total_trades:int=0
    wins:int=0; losses:int=0; gross_profit:float=0; gross_loss:float=0
    passed:bool=False; blown:bool=False; blown_reason:str=""; day_trades:int=0
    position:Optional[dict]=None
    # 50% CONSISTENCY RULE: best single day can't exceed 50% of profit target
    best_day_pnl:float=0  # tracks highest single-day P&L
    effective_target:float=3000  # adjusts upward if consistency rule violated
    # DAILY LOSS LIMIT — soft rule (blocks new entries, doesn't blow account)
    daily_locked:bool=False  # set True when daily P&L hits -$1000
    
    def add_trade(self,pnl):
        self.total_pnl+=pnl; self.daily_pnl+=pnl; self.total_trades+=1
        if pnl>0: self.wins+=1; self.gross_profit+=pnl
        else: self.losses+=1; self.gross_loss+=abs(pnl)
        # Trailing drawdown (on realized P&L — unrealized tracked in manage_pos)
        if self.total_pnl>self.equity_high:
            self.equity_high=self.total_pnl
            self.trailing_floor=self.equity_high-self.max_dd
        if self.total_pnl<=self.trailing_floor:
            self.blown=True
            self.blown_reason=f"TrailDD ${self.total_pnl:.0f}<=${self.trailing_floor:.0f}"
        # Daily loss limit — OBSERVATION ONLY (TopstepX removed hard limit Aug 2024+)
        # Just tracks if you would have been locked. No blocking, no blowing.
        if self.daily_pnl<=-self.daily_limit and not self.daily_locked:
            self.daily_locked=True  # flag for dashboard display only
        # 50% CONSISTENCY RULE — checked in REAL TIME
        # Current day's P&L might be the best day so far
        current_best=max(self.best_day_pnl, self.daily_pnl)
        consistency_limit=self.profit_target*0.5  # $1,500 for 50K
        if current_best>consistency_limit:
            self.effective_target=max(self.profit_target, current_best*2)
        else:
            self.effective_target=self.profit_target
        # Check passed with EFFECTIVE target (adjusted for consistency rule)
        if self.total_pnl>=self.effective_target:
            self.passed=True
    
    def new_day(self):
        # Track best day P&L for consistency rule
        if self.daily_pnl>self.best_day_pnl:
            self.best_day_pnl=self.daily_pnl
        # 50% CONSISTENCY RULE:
        # Best trading day cannot exceed 50% of the profit target
        # If it does, the effective target INCREASES to best_day × 2
        consistency_limit=self.profit_target*0.5  # $1,500 for 50K
        if self.best_day_pnl>consistency_limit:
            self.effective_target=max(self.profit_target, self.best_day_pnl*2)
        else:
            self.effective_target=self.profit_target
        # Winning day: $150+ net P&L (for Express payout eligibility tracking)
        if self.daily_pnl>=150: self.winning_days+=1
        self.daily_pnl=0; self.day_count+=1; self.day_trades=0; self.daily_locked=False
    
    @property
    def win_rate(self): return self.wins/max(self.total_trades,1)
    @property
    def cushion(self): return self.total_pnl-self.trailing_floor
    @property
    def remaining_daily(self): return self.daily_limit+self.daily_pnl
    @property
    def profit_factor(self): return self.gross_profit/max(self.gross_loss,1)
    @property
    def avg_rr(self):
        aw=self.gross_profit/max(self.wins,1); al=self.gross_loss/max(self.losses,1)
        return aw/max(al,1)
    @property
    def consistency_ok(self):
        """True if best day is within 50% of profit target"""
        return self.best_day_pnl<=self.profit_target*0.5
    @property
    def to_target(self):
        return self.effective_target-self.total_pnl

STRATEGY_CONFIGS={
    "VWAP_Target":{"target":"vwap","z_base":1.5,"dynamic_z":False,"max_trades":4,
                   "entry_start":45,"entry_end":360,"confluence_check":False,"min_dist":0,"max_dist":999},
    "Volume_POC":{"target":"vol_poc","z_base":1.5,"dynamic_z":False,"max_trades":4,
                  "entry_start":45,"entry_end":360,"confluence_check":False,"min_dist":0,"max_dist":999},
    "TPO_POC_Filtered":{"target":"tpo_poc","z_base":1.5,"dynamic_z":False,"max_trades":4,
                        "entry_start":45,"entry_end":360,"confluence_check":True,"min_dist":0,"max_dist":999},
    "Dynamic_Tight":{"target":"tpo_poc","z_base":1.5,"dynamic_z":True,"z_high":2.0,
                     "low_atr":0.7,"high_atr":1.3,"max_trades":4,"entry_start":45,
                     "entry_end":360,"confluence_check":False,"min_dist":10,"max_dist":32},
}

def get_target(cfg,vwap,tpo_poc,vol_poc):
    t=cfg["target"]
    if t=="vwap":return round(vwap/TICK_SIZE)*TICK_SIZE if vwap else None
    elif t=="vol_poc":return vol_poc
    elif t=="tpo_poc":return tpo_poc
    return None

def manage_pos(state,price,high,low,target,bar_idx):
    pos=state.position
    if pos is None:return None
    bh=bar_idx-pos["entry_bar"];d=pos["direction"]
    
    # CRITICAL: Calculate unrealized P&L using intrabar HIGH for the best case
    # and track unrealized equity for trailing drawdown ratcheting
    if d=="LONG":
        if pos["best_price"]<price:pos["best_price"]=price
        # Best unrealized uses the bar HIGH (best possible price during this bar)
        best_ur_ticks=(high-pos["entry_price"])/TICK_SIZE
        best_ur_pnl=best_ur_ticks*TICK_VALUE*pos["contracts"]
        # Ratchet trailing floor on unrealized equity high
        unrealized_equity=state.total_pnl+best_ur_pnl
        if unrealized_equity>state.equity_high:
            state.equity_high=unrealized_equity
            state.trailing_floor=state.equity_high-2000
        # Check if unrealized LOW would blow the account
        worst_ur_ticks=(low-pos["entry_price"])/TICK_SIZE
        worst_ur_pnl=worst_ur_ticks*TICK_VALUE*pos["contracts"]
        worst_equity=state.total_pnl+worst_ur_pnl
        if worst_equity<=state.trailing_floor:
            # Blown by unrealized drawdown - exit at the floor level
            blown_pnl=state.trailing_floor-state.total_pnl
            state.add_trade(blown_pnl);state.position=None
            return{"action":"EXIT_DD","pnl":blown_pnl,"ticks":blown_pnl/(TICK_VALUE*pos["contracts"]),
                   "exit_price":pos["entry_price"]+blown_pnl/(TICK_VALUE*pos["contracts"])*TICK_SIZE,"pos":pos}
        
        ur=(pos["best_price"]-pos["entry_price"])/TICK_SIZE;cur=(price-pos["entry_price"])/TICK_SIZE
        if low<=pos["stop_loss"]:
            tk=(pos["stop_loss"]-pos["entry_price"])/TICK_SIZE;pnl=tk*TICK_VALUE*pos["contracts"]
            state.add_trade(pnl);state.position=None
            return{"action":"EXIT_STOP","pnl":pnl,"ticks":tk,"exit_price":pos["stop_loss"],"pos":pos}
        if target and price>=target and cur>=12:
            pnl=cur*TICK_VALUE*pos["contracts"];state.add_trade(pnl);state.position=None
            return{"action":"EXIT_TARGET","pnl":pnl,"ticks":cur,"exit_price":price,"pos":pos}
        if ur>=20:
            tl=round((pos["best_price"]-16*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
            if tl>pos["stop_loss"]:
                if low<=tl:
                    tk=(tl-pos["entry_price"])/TICK_SIZE;pnl=tk*TICK_VALUE*pos["contracts"]
                    state.add_trade(pnl);state.position=None
                    return{"action":"EXIT_TRAIL","pnl":pnl,"ticks":tk,"exit_price":tl,"pos":pos}
                pos["stop_loss"]=tl
        if ur>=10 and pos["stop_loss"]<pos["entry_price"]:pos["stop_loss"]=pos["entry_price"]
        if bh>90 and cur>-4:
            pnl=cur*TICK_VALUE*pos["contracts"];state.add_trade(pnl);state.position=None
            return{"action":"EXIT_TIME","pnl":pnl,"ticks":cur,"exit_price":price,"pos":pos}
    else:
        if pos["best_price"]==0 or price<pos["best_price"]:pos["best_price"]=price
        # Best unrealized for SHORT uses bar LOW
        best_ur_ticks=(pos["entry_price"]-low)/TICK_SIZE
        best_ur_pnl=best_ur_ticks*TICK_VALUE*pos["contracts"]
        unrealized_equity=state.total_pnl+best_ur_pnl
        if unrealized_equity>state.equity_high:
            state.equity_high=unrealized_equity
            state.trailing_floor=state.equity_high-2000
        # Worst unrealized for SHORT uses bar HIGH
        worst_ur_ticks=(pos["entry_price"]-high)/TICK_SIZE
        worst_ur_pnl=worst_ur_ticks*TICK_VALUE*pos["contracts"]
        worst_equity=state.total_pnl+worst_ur_pnl
        if worst_equity<=state.trailing_floor:
            blown_pnl=state.trailing_floor-state.total_pnl
            state.add_trade(blown_pnl);state.position=None
            return{"action":"EXIT_DD","pnl":blown_pnl,"ticks":blown_pnl/(TICK_VALUE*pos["contracts"]),
                   "exit_price":pos["entry_price"]-blown_pnl/(TICK_VALUE*pos["contracts"])*TICK_SIZE,"pos":pos}
        
        ur=(pos["entry_price"]-pos["best_price"])/TICK_SIZE;cur=(pos["entry_price"]-price)/TICK_SIZE
        if high>=pos["stop_loss"]:
            tk=(pos["entry_price"]-pos["stop_loss"])/TICK_SIZE;pnl=tk*TICK_VALUE*pos["contracts"]
            state.add_trade(pnl);state.position=None
            return{"action":"EXIT_STOP","pnl":pnl,"ticks":tk,"exit_price":pos["stop_loss"],"pos":pos}
        if target and price<=target and cur>=12:
            pnl=cur*TICK_VALUE*pos["contracts"];state.add_trade(pnl);state.position=None
            return{"action":"EXIT_TARGET","pnl":pnl,"ticks":cur,"exit_price":price,"pos":pos}
        if ur>=20:
            tl=round((pos["best_price"]+16*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
            if tl<pos["stop_loss"]:
                if high>=tl:
                    tk=(pos["entry_price"]-tl)/TICK_SIZE;pnl=tk*TICK_VALUE*pos["contracts"]
                    state.add_trade(pnl);state.position=None
                    return{"action":"EXIT_TRAIL","pnl":pnl,"ticks":tk,"exit_price":tl,"pos":pos}
                pos["stop_loss"]=tl
        if ur>=10 and pos["stop_loss"]>pos["entry_price"]:pos["stop_loss"]=pos["entry_price"]
        if bh>90 and cur>-4:
            pnl=cur*TICK_VALUE*pos["contracts"];state.add_trade(pnl);state.position=None
            return{"action":"EXIT_TIME","pnl":pnl,"ticks":cur,"exit_price":price,"pos":pos}
    return None

def check_entry(sn,cfg,state,bi,price,target,z,std,pm,tpo_poc,vol_poc,atr_obj):
    if state.passed or state.blown or state.position:return None
    if state.day_trades>=cfg["max_trades"] or bi<cfg["entry_start"] or bi>cfg["entry_end"]:return None
    if z is None:return None
    zt=cfg["z_base"]
    if cfg.get("dynamic_z"):
        ca=atr_obj.atr() if atr_obj else None;rl=atr_obj.rolling if atr_obj else None
        if ca and rl and rl>0:
            r=ca/rl
            if r<cfg.get("low_atr",0.7) or r>cfg.get("high_atr",1.3):zt=cfg.get("z_high",2.0)
    if abs(z)<zt:return{"action":"BELOW_THRESH","reason":f"|z|={abs(z):.2f}<{zt:.2f}","z":z,"z_thresh":zt}
    dt=abs(price-target)/TICK_SIZE
    if dt<cfg["min_dist"] or dt>cfg["max_dist"]:return{"action":"SKIP","reason":f"dist {dt:.0f}t"}
    if pm>0.15:return{"action":"SKIP","reason":f"POC mig {pm:.2f}"}
    if cfg["confluence_check"] and tpo_poc and vol_poc and abs(tpo_poc-vol_poc)>3:
        return{"action":"SKIP","reason":f"POC diverged {abs(tpo_poc-vol_poc):.1f}"}
    dist=abs(price-target);sd=max(min(dist*0.5,24*TICK_SIZE),6*TICK_SIZE)
    lpc=(sd/TICK_SIZE)*TICK_VALUE;cts=max(1,min(int(800/lpc),2)) if lpc>0 else 1
    # Daily risk note (observational only — does not block trades)
    # if lpc*cts>state.remaining_daily*0.95: logged but not blocked
    now_str=datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    if price<target:
        stop=round((price-sd)/TICK_SIZE)*TICK_SIZE
        state.position={"direction":"LONG","entry_price":price,"entry_time":now_str,
            "contracts":cts,"stop_loss":stop,"target":target+4*TICK_SIZE,
            "entry_bar":bi,"z_at_entry":abs(z),"best_price":price,"strategy":sn}
        state.day_trades+=1
        return{"action":"ENTRY","direction":"LONG","price":price,"stop":stop,"target":target,"contracts":cts,"z":abs(z)}
    elif price>target:
        stop=round((price+sd)/TICK_SIZE)*TICK_SIZE
        state.position={"direction":"SHORT","entry_price":price,"entry_time":now_str,
            "contracts":cts,"stop_loss":stop,"target":target-4*TICK_SIZE,
            "entry_bar":bi,"z_at_entry":abs(z),"best_price":price,"strategy":sn}
        state.day_trades+=1
        return{"action":"ENTRY","direction":"SHORT","price":price,"stop":stop,"target":target,"contracts":cts,"z":abs(z)}
    return None

def force_close(state,price):
    pos=state.position
    if not pos:return None
    tk=((price-pos["entry_price"]) if pos["direction"]=="LONG" else (pos["entry_price"]-price))/TICK_SIZE
    pnl=tk*TICK_VALUE*pos["contracts"];state.add_trade(pnl)
    r={"action":"EXIT_EOD","pnl":pnl,"ticks":tk,"exit_price":price,"pos":pos}
    state.position=None;return r

# ============================================================
# BOT
# ============================================================

class CSVLogger:
    def __init__(self,fp,fields):
        self.fp=fp;self.fields=fields
        if not os.path.exists(fp):
            with open(fp,'w',newline='') as f:csv.DictWriter(f,fieldnames=fields).writeheader()
    def log(self,row):
        full={k:row.get(k,"") for k in self.fields}
        with open(self.fp,'a',newline='') as f:csv.DictWriter(f,fieldnames=self.fields).writerow(full)

class Bot:
    def __init__(self,symbol="SPY",log_dir="paper_logs",port=8080):
        self.symbol=symbol;self.is_spy=symbol.upper()=="SPY"
        self.scale=10.0 if self.is_spy else 1.0
        self.log_dir=log_dir;self.port=port
        os.makedirs(log_dir,exist_ok=True)
        
        self.logger=logging.getLogger("paperbot");self.logger.setLevel(logging.DEBUG)
        if not self.logger.handlers:
            fh=logging.FileHandler(os.path.join(log_dir,"paper_log.txt"))
            fh.setFormatter(logging.Formatter("%(asctime)s|%(levelname)s|%(message)s"));self.logger.addHandler(fh)
        
        self.tpo=TPO();self.vpoc=VolPOC();self.vwap=VWAPCalc();self.atr=SessionATR()
        self.bar_count=0;self.today=None;self.data_source=symbol
        self.strategies={n:StrategyState(name=n) for n in STRATEGY_CONFIGS}
        self.prev_session={}  # snapshot of yesterday's end-of-day state per strategy
        
        tf=["timestamp","strategy","action","direction","contracts","entry_price",
            "exit_price","stop","target","z_score","pnl","ticks","total_pnl",
            "equity_high","trailing_floor","daily_pnl","cushion","bar_idx"]
        sf=["timestamp","strategy","signal","reason","price","vwap","z_score",
            "z_thresh","target","poc_migration","bar_idx"]
        bf=["timestamp","bar_idx","open","high","low","close","volume","zero_vol",
            "vwap","vwap_std","z_vwap","tpo_poc","vol_poc","poc_migration","atr","vol_quality"]
        df=["date","strategy","daily_pnl","total_pnl","equity_high","trailing_floor",
            "trades_today","win_rate","profit_factor","winning_day","day_count","status"]
        
        self.trade_csv=CSVLogger(os.path.join(log_dir,"paper_trades.csv"),tf)
        self.signal_csv=CSVLogger(os.path.join(log_dir,"paper_signals.csv"),sf)
        self.bar_csv=CSVLogger(os.path.join(log_dir,"paper_bars.csv"),bf)
        self.daily_csv=CSVLogger(os.path.join(log_dir,"paper_daily.csv"),df)
    
    def save_state(self):
        st={"today":str(self.today),"bar_count":self.bar_count,"data_source":self.data_source,
            "atr_hist":self.atr.session_atrs[-20:],"strategies":{}}
        for n,s in self.strategies.items():st["strategies"][n]={k:v for k,v in s.__dict__.items()}
        with open(os.path.join(self.log_dir,"paper_state.json"),'w') as f:
            json.dump(st,f,indent=2,default=str)
    
    def load_state(self):
        p=os.path.join(self.log_dir,"paper_state.json")
        if not os.path.exists(p):return False
        with open(p) as f:st=json.load(f)
        self.data_source=st.get("data_source",self.symbol)
        self.atr.session_atrs=st.get("atr_hist",[])
        if len(self.atr.session_atrs)>=5:self.atr.rolling=np.mean(self.atr.session_atrs[-20:])
        for n,sd in st.get("strategies",{}).items():
            if n in self.strategies:
                for k,v in sd.items():setattr(self.strategies[n],k,v)
        return True
    
    def new_day(self):
        # Snapshot previous session before resetting
        self.prev_session={}
        for n,s in self.strategies.items():
            self.prev_session[n]={
                "total_pnl":s.total_pnl,"daily_pnl":s.daily_pnl,
                "equity_high":s.equity_high,"trailing_floor":s.trailing_floor,
                "total_trades":s.total_trades,"wins":s.wins,"losses":s.losses,
                "win_rate":s.win_rate,"profit_factor":s.profit_factor,
                "avg_rr":s.avg_rr,"day_count":s.day_count,
                "winning_days":s.winning_days,
                "passed":s.passed,"blown":s.blown,"blown_reason":s.blown_reason,
            }
            if s.day_count>0:
                self.daily_csv.log({"date":str(self.today),"strategy":n,
                    "daily_pnl":f"{s.daily_pnl:.0f}","total_pnl":f"{s.total_pnl:.0f}",
                    "equity_high":f"{s.equity_high:.0f}","trailing_floor":f"{s.trailing_floor:.0f}",
                    "trades_today":s.day_trades,"win_rate":f"{s.win_rate:.2f}",
                    "profit_factor":f"{s.profit_factor:.2f}",
                    "winning_day":"YES" if s.daily_pnl>=150 else "NO",
                    "day_count":s.day_count,
                    "status":"PASS" if s.passed else ("BLOW" if s.blown else "LIVE")})
        self.atr.reset_day();self.tpo.reset();self.vpoc.reset();self.vwap.reset()
        self.bar_count=0;self.today=datetime.now(ET).date()
        for s in self.strategies.values():s.new_day();s.position=None
        self.save_state()
    
    def log_action(self,sn,state,action,ts):
        if action["action"]=="ENTRY":
            row={"timestamp":str(ts),"strategy":sn,"action":"ENTRY","direction":action["direction"],
                 "contracts":action["contracts"],"entry_price":f"{action['price']:.2f}","exit_price":"",
                 "stop":f"{action['stop']:.2f}","target":f"{action['target']:.2f}",
                 "z_score":f"{action['z']:.2f}","pnl":"0","ticks":"0",
                 "total_pnl":f"{state.total_pnl:.0f}","equity_high":f"{state.equity_high:.0f}",
                 "trailing_floor":f"{state.trailing_floor:.0f}","daily_pnl":f"{state.daily_pnl:.0f}",
                 "cushion":f"{state.cushion:.0f}","bar_idx":self.bar_count}
            self.trade_csv.log(row)
            with STATE_LOCK:DASHBOARD_STATE["recent_trades"].append(row);DASHBOARD_STATE["recent_trades"]=DASHBOARD_STATE["recent_trades"][-30:]
            self.logger.info(f"[{sn}] ENTRY {action['direction']} {action['contracts']}ct @{action['price']:.2f} z={action['z']:.1f}")
        elif action["action"].startswith("EXIT"):
            pos=action.get("pos",{})
            row={"timestamp":str(ts),"strategy":sn,"action":action["action"],
                 "direction":pos.get("direction",""),"contracts":pos.get("contracts",0),
                 "entry_price":f"{pos.get('entry_price',0):.2f}",
                 "exit_price":f"{action.get('exit_price',0):.2f}",
                 "stop":f"{pos.get('stop_loss',0):.2f}","target":f"{pos.get('target',0):.2f}",
                 "z_score":f"{pos.get('z_at_entry',0):.2f}",
                 "pnl":f"{action['pnl']:.0f}","ticks":f"{action['ticks']:.0f}",
                 "total_pnl":f"{state.total_pnl:.0f}","equity_high":f"{state.equity_high:.0f}",
                 "trailing_floor":f"{state.trailing_floor:.0f}","daily_pnl":f"{state.daily_pnl:.0f}",
                 "cushion":f"{state.cushion:.0f}","bar_idx":self.bar_count}
            self.trade_csv.log(row)
            with STATE_LOCK:DASHBOARD_STATE["recent_trades"].append(row);DASHBOARD_STATE["recent_trades"]=DASHBOARD_STATE["recent_trades"][-30:]
            self.logger.info(f"[{sn}] {action['action']} ${action['pnl']:+.0f} Total=${state.total_pnl:.0f}")
    
    def process_bar(self,op,hi,lo,cl,vol,ts):
        op*=self.scale;hi*=self.scale;lo*=self.scale;cl*=self.scale
        op=round(op/TICK_SIZE)*TICK_SIZE;hi=round(hi/TICK_SIZE)*TICK_SIZE
        lo=round(lo/TICK_SIZE)*TICK_SIZE;cl=round(cl/TICK_SIZE)*TICK_SIZE
        zv=vol==0
        tpo_poc=self.tpo.update(self.bar_count,hi,lo,cl)
        vol_poc=self.vpoc.update(hi,lo,cl,vol)
        vwap_v,std_v=self.vwap.update(hi,lo,cl,vol)
        self.atr.update(hi,lo,cl);pm=self.tpo.migration_speed(20)
        z_vwap=self.vwap.z_from_level(cl,vwap_v) if vwap_v else None
        vq=self.vwap.volume_quality
        
        self.bar_csv.log({"timestamp":str(ts),"bar_idx":self.bar_count,
            "open":f"{op:.2f}","high":f"{hi:.2f}","low":f"{lo:.2f}","close":f"{cl:.2f}",
            "volume":vol,"zero_vol":"Y" if zv else "N",
            "vwap":f"{vwap_v:.2f}" if vwap_v else "","vwap_std":f"{std_v:.2f}" if std_v else "",
            "z_vwap":f"{z_vwap:.2f}" if z_vwap else "","tpo_poc":f"{tpo_poc:.2f}" if tpo_poc else "",
            "vol_poc":f"{vol_poc:.2f}" if vol_poc else "","poc_migration":f"{pm:.3f}",
            "atr":f"{self.atr.atr():.2f}" if self.atr.atr() else "","vol_quality":f"{vq:.2f}"})
        
        results={}
        for sn,state in self.strategies.items():
            cfg=STRATEGY_CONFIGS[sn]
            if state.passed or state.blown or self.bar_count<15:results[sn]=None;continue
            target=get_target(cfg,vwap_v,tpo_poc,vol_poc)
            z=self.vwap.z_from_level(cl,target) if target and std_v else None
            ex=manage_pos(state,cl,hi,lo,target,self.bar_count)
            if ex:self.log_action(sn,state,ex,ts);results[sn]=ex;continue
            en=check_entry(sn,cfg,state,self.bar_count,cl,target,z,std_v,pm,tpo_poc,vol_poc,self.atr)
            if en:
                if en["action"]=="ENTRY":self.log_action(sn,state,en,ts)
                elif en["action"] in ("SKIP","BELOW_THRESH"):
                    sig_row={"timestamp":str(ts),"strategy":sn,"signal":en["action"],
                             "reason":en.get("reason",""),"price":f"{cl:.2f}",
                             "vwap":f"{vwap_v:.2f}" if vwap_v else "",
                             "z_score":f"{z:.2f}" if z else "","z_thresh":f"{en.get('z_thresh',cfg['z_base']):.2f}",
                             "target":f"{target:.2f}" if target else "","poc_migration":f"{pm:.3f}",
                             "bar_idx":self.bar_count}
                    self.signal_csv.log(sig_row)
                    with STATE_LOCK:DASHBOARD_STATE["recent_signals"].append(sig_row);DASHBOARD_STATE["recent_signals"]=DASHBOARD_STATE["recent_signals"][-20:]
                results[sn]=en
            else:results[sn]=None
        
        self.bar_count+=1
        if self.bar_count%10==0:self.save_state()
        
        result={"bar":self.bar_count-1,"time":ts,"price":cl,"vwap":vwap_v,"std":std_v,
                "z":z_vwap,"poc":tpo_poc,"vpoc":vol_poc,"poc_mig":pm,
                "vol_quality":vq,"strategies":results}
        update_dashboard(result,self.strategies,self.data_source,self.prev_session)
        return result
    
    def flatten_all(self,price,ts):
        for n,s in self.strategies.items():
            if s.position:
                ex=force_close(s,price)
                if ex:self.log_action(n,s,ex,ts)

# ============================================================
# DATA & MAIN
# ============================================================

def check_vol(sym):
    try:
        d=yf.Ticker(sym).history(period="2d",interval="1m")
        if len(d)==0:return 0,None
        return (d["Volume"]>0).mean(),d
    except:return 0,None

def get_bar(sym):
    try:
        d=yf.Ticker(sym).history(period="1d",interval="1m")
        if len(d)>0:
            l=d.iloc[-1]
            return{"open":float(l["Open"]),"high":float(l["High"]),"low":float(l["Low"]),
                   "close":float(l["Close"]),"volume":int(l["Volume"]),"timestamp":d.index[-1]}
    except:pass
    return None

def get_today(sym):
    try:return yf.Ticker(sym).history(period="1d",interval="1m")
    except:return None

def is_market_hours():
    now=datetime.now(ET)
    if now.weekday()>=5:return False
    return dtime(9,30)<=now.time()<=dtime(16,0)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--symbol",nargs="+",default=["SPY"],help="One or more symbols (e.g. --symbol SPY ES=F)")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--port",type=int,default=8080)
    parser.add_argument("--interval",type=int,default=60)
    args=parser.parse_args()
    
    sym_list=args.symbol
    print("="*70)
    print("  TOPSTEP PAPER TRADING BOT v5 — MULTI-SYMBOL")
    print(f"  Symbols: {', '.join(sym_list)} | Port: {args.port}")
    print("="*70)
    
    # Volume check + create bot per symbol
    bots={}
    for sym in sym_list:
        print(f"\n  Checking {sym} volume...")
        vq,_=check_vol(sym)
        print(f"  {sym} volume quality: {vq:.0%}")
        actual_sym=sym
        if vq<0.5 and sym.upper()!="SPY":
            print(f"  ⚠ {sym} has poor volume. Falling back to SPY.")
            actual_sym="SPY"
        log_dir=f"paper_logs_{actual_sym.replace('=','').replace('/','')}"
        bots[actual_sym]=Bot(symbol=actual_sym,log_dir=log_dir,port=args.port)
        if args.resume and bots[actual_sym].load_state():
            print(f"  ✓ {actual_sym} resumed from saved state")
    
    # Deduplicate if fallback happened
    bots={k:v for k,v in bots.items()}
    print(f"\n  Running {len(bots)} symbol(s): {', '.join(bots.keys())}")
    
    load_dashboard_html()
    server=start_web_server(args.port)
    
    # Initialize dashboard state with ALL symbols immediately
    # so the first API call returns both tabs
    def _serialize_bot(sym,bot):
        return {"symbol":sym,"data":{
            "symbol":sym,"bar":0,"price":0,"vwap":0,"std":0,"z":None,
            "poc":0,"vpoc":0,"poc_mig":0,"vol_quality":0,
            "market_open":False,"strategies":{
                n:{"total_pnl":s.total_pnl,"daily_pnl":s.daily_pnl,
                   "equity_high":s.equity_high,"trailing_floor":s.trailing_floor,
                   "cushion":s.cushion,"total_trades":s.total_trades,
                   "wins":s.wins,"losses":s.losses,"win_rate":s.win_rate,
                   "avg_rr":s.avg_rr,"profit_factor":s.profit_factor,
                   "day_count":s.day_count,"winning_days":s.winning_days,
                   "day_trades":s.day_trades,"max_trades":4,
                   "passed":s.passed,"blown":s.blown,"blown_reason":s.blown_reason,
                   "remaining_daily":s.remaining_daily,"daily_locked":s.daily_locked,"position":s.position,
                   "best_day_pnl":s.best_day_pnl,"effective_target":s.effective_target,
                   "consistency_ok":s.consistency_ok,"to_target":s.to_target}
                for n,s in bot.strategies.items()},
            "previous_session":bot.prev_session,
            "recent_trades":[],"recent_signals":[]}}
    with STATE_LOCK:
        DASHBOARD_STATE["market_open"]=is_market_hours()
        DASHBOARD_STATE["last_update"]=datetime.now(ET).strftime("%H:%M:%S ET")
        DASHBOARD_STATE["symbols"]=[_serialize_bot(sym,bot) for sym,bot in bots.items()]
    
    print(f"\n  📊 Dashboard: http://localhost:{args.port}")
    print(f"  📊 API:       http://localhost:{args.port}/api/status")
    print(f"\n  Waiting for market hours. Ctrl+C to stop.\n")
    
    running=True
    def handler(s,f):
        nonlocal running;running=False
    signal_mod.signal(signal_mod.SIGINT,handler)
    
    last_bts={sym:None for sym in bots}
    session=False
    
    while running:
        try:
            if not is_market_hours():
                if session:
                    for sym,bot in bots.items():
                        bar=get_bar(sym)
                        if bar:bot.flatten_all(bar["close"]*bot.scale,bar["timestamp"])
                        bot.new_day()
                    session=False
                # Always update dashboard with all symbols (even on fresh startup with no session)
                with STATE_LOCK:
                    DASHBOARD_STATE["market_open"]=False
                    DASHBOARD_STATE["last_update"]=datetime.now(ET).strftime("%H:%M:%S ET")
                    DASHBOARD_STATE["symbols"]=[{"symbol":sym,"data":{
                        "symbol":sym,"bar":0,"price":0,"vwap":0,"std":0,"z":None,
                        "poc":0,"vpoc":0,"poc_mig":0,"vol_quality":0,
                        "market_open":False,"strategies":{
                            n:{"total_pnl":s.total_pnl,"daily_pnl":s.daily_pnl,
                               "equity_high":s.equity_high,"trailing_floor":s.trailing_floor,
                               "cushion":s.cushion,"total_trades":s.total_trades,
                               "wins":s.wins,"losses":s.losses,"win_rate":s.win_rate,
                               "avg_rr":s.avg_rr,"profit_factor":s.profit_factor,
                               "day_count":s.day_count,"winning_days":s.winning_days,
                               "day_trades":s.day_trades,"max_trades":4,
                               "passed":s.passed,"blown":s.blown,"blown_reason":s.blown_reason,
                               "remaining_daily":s.remaining_daily,"daily_locked":s.daily_locked,"position":s.position,"best_day_pnl":s.best_day_pnl,"effective_target":s.effective_target,"consistency_ok":s.consistency_ok,"to_target":s.to_target}
                            for n,s in bot.strategies.items()},
                        "previous_session":bot.prev_session,
                        "recent_trades":[],"recent_signals":[]
                    }} for sym,bot in bots.items()]
                time_module.sleep(30);continue
            
            if not session:
                for sym,bot in bots.items():
                    bot.new_day()
                    data=get_today(sym)
                    if data is not None and len(data)>1:
                        for i in range(len(data)-1):
                            r=data.iloc[i]
                            bot.process_bar(float(r["Open"]),float(r["High"]),
                                           float(r["Low"]),float(r["Close"]),
                                           int(r["Volume"]),data.index[i])
                        print(f"  {sym}: backfilled {len(data)-1} bars")
                session=True
            
            # Poll each symbol
            for sym,bot in bots.items():
                bar=get_bar(sym)
                if not bar:continue
                bt=str(bar["timestamp"])
                if bt==last_bts.get(sym):continue
                last_bts[sym]=bt
                bot.process_bar(bar["open"],bar["high"],bar["low"],
                               bar["close"],bar["volume"],bar["timestamp"])
            
            # Multi-symbol dashboard update
            with STATE_LOCK:
                DASHBOARD_STATE["market_open"]=True
                DASHBOARD_STATE["last_update"]=datetime.now(ET).strftime("%H:%M:%S ET")
                sym_data=[]
                for sym,bot in bots.items():
                    sym_data.append({"symbol":sym,"data":{
                        "symbol":sym,"bar":bot.bar_count,
                        "price":DASHBOARD_STATE.get("price",0),
                        "vwap":bot.vwap.vwap,"std":bot.vwap.std,
                        "z":bot.vwap.z_from_level(bot.vwap.vwap,bot.vwap.vwap) if bot.vwap.vwap else None,
                        "poc":bot.tpo.poc,"vpoc":bot.vpoc.vpoc,
                        "poc_mig":bot.tpo.migration_speed(20),
                        "vol_quality":bot.vwap.volume_quality,
                        "market_open":True,
                        "strategies":{
                            n:{"total_pnl":s.total_pnl,"daily_pnl":s.daily_pnl,
                               "equity_high":s.equity_high,"trailing_floor":s.trailing_floor,
                               "cushion":s.cushion,"total_trades":s.total_trades,
                               "wins":s.wins,"losses":s.losses,"win_rate":s.win_rate,
                               "avg_rr":s.avg_rr,"profit_factor":s.profit_factor,
                               "day_count":s.day_count,"winning_days":s.winning_days,
                               "day_trades":s.day_trades,"max_trades":4,
                               "passed":s.passed,"blown":s.blown,"blown_reason":s.blown_reason,
                               "remaining_daily":s.remaining_daily,"daily_locked":s.daily_locked,"position":s.position,"best_day_pnl":s.best_day_pnl,"effective_target":s.effective_target,"consistency_ok":s.consistency_ok,"to_target":s.to_target}
                            for n,s in bot.strategies.items()},
                        "previous_session":bot.prev_session,
                        "recent_trades":DASHBOARD_STATE.get("recent_trades",[]),
                        "recent_signals":DASHBOARD_STATE.get("recent_signals",[]),
                    }})
                if len(bots)==1:
                    # Single symbol: flat format for backward compat
                    DASHBOARD_STATE.update(sym_data[0]["data"])
                    DASHBOARD_STATE["symbols"]=None
                else:
                    DASHBOARD_STATE["symbols"]=sym_data
            
            # Console
            statuses=[]
            for sym,bot in bots.items():
                best=max(bot.strategies.values(),key=lambda s:s.total_pnl)
                statuses.append(f"{sym}:{best.name[:8]} ${best.total_pnl:+.0f}")
            print(f"\r  {' | '.join(statuses)} | http://localhost:{args.port}  ",end="",flush=True)
            
            # Check completion
            all_done=all(all(s.passed or s.blown for s in bot.strategies.values()) for bot in bots.values())
            if all_done:
                print("\n\n  All strategies on all symbols completed.")
                for bot in bots.values():bot.save_state()
                break
            
            # TOPSTEP RULE: All positions must be closed by 3:10 PM CT = 4:10 PM ET
            # We flatten at 3:50 PM ET (2:50 PM CT) — 20 min buffer before deadline
            # East coast VM uses ET via pytz, so this is timezone-correct
            now=datetime.now(ET)
            if now.hour==15 and now.minute>=50:
                for sym,bot in bots.items():
                    bar=get_bar(sym)
                    if bar:bot.flatten_all(bar["close"]*bot.scale,bar["timestamp"])
            
            time_module.sleep(args.interval)
        except KeyboardInterrupt:break
        except Exception as e:
            logging.getLogger("paperbot").error(f"Error: {e}",exc_info=True)
            time_module.sleep(30)
    
    for bot in bots.values():bot.save_state()
    server.shutdown()
    print(f"\n\n{'='*70}")
    print(f"  FINAL SUMMARY")
    print(f"{'='*70}")
    for sym,bot in bots.items():
        print(f"\n  {sym}:")
        print(f"  {'Strategy':<22} {'PnL':>8} {'Trades':>7} {'WR':>5} {'R:R':>5} {'Days':>5} {'Status':>6}")
        print(f"  {'-'*22} {'-'*8} {'-'*7} {'-'*5} {'-'*5} {'-'*5} {'-'*6}")
        for n,s in bot.strategies.items():
            st="PASS" if s.passed else("BLOW" if s.blown else "LIVE")
            print(f"  {n:<22} ${s.total_pnl:>+6.0f} {s.total_trades:>7} {s.win_rate:>4.0%} {s.avg_rr:>4.1f} {s.day_count:>5} {st:>6}")
        print(f"  Logs: {bot.log_dir}/")
    print(f"\n  Dashboard was at: http://localhost:{args.port}")
    print(f"{'='*70}")

if __name__=="__main__":main()
