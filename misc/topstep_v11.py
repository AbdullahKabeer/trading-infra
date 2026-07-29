"""
Topstep v11 — VOLUME POC vs TPO POC TARGET
============================================
Same v9 Full Filters strategy, only change is WHAT we're reverting to:

  A) TPO POC target (current — time-based)
  B) Volume POC target (recommended — capital-based)
  C) Composite: average of TPO + Volume POC when close, Volume when diverged
  D) VWAP target (the actual mean — for reference)

The thesis: In modern algo-dominated markets, volume POC is a stronger
magnet because it represents where institutional capital actually
transacted, not just where price sat for a long time.

USAGE: python3 topstep_v11.py --polygon YOUR_KEY --months 6
"""

import numpy as np
import pandas as pd
import argparse
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from collections import defaultdict

TICK_SIZE = 0.25
TICK_VALUE = 12.50

@dataclass
class TopstepAccount:
    name:str; buying_power:int; monthly_fee:int
    profit_target:int; max_drawdown:int; daily_loss_limit:int; max_contracts:int

ACCOUNTS = {"50K": TopstepAccount("50K Combine",50000,49,3000,2000,1000,5)}

# ============================================================
# DATA
# ============================================================

def fetch_polygon(api_key, months=6):
    import requests, time
    print(f"\n  Fetching SPY from Polygon.io ({months}mo)...")
    all_data=[]; end=datetime.now(); cur=end-timedelta(days=months*30)
    while cur<end:
        ce=min(cur+timedelta(days=14),end)
        url=f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/minute/{cur.strftime('%Y-%m-%d')}/{ce.strftime('%Y-%m-%d')}?adjusted=true&sort=asc&limit=50000&apiKey={api_key}"
        try:
            r=requests.get(url,timeout=30).json()
            if r.get("resultsCount",0)>0:
                d=pd.DataFrame(r["results"])
                d["datetime"]=pd.to_datetime(d["t"],unit="ms")
                d=d.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"})
                all_data.append(d[["datetime","open","high","low","close","volume"]])
                print(f"    {len(d)} bars ({cur.date()} → {ce.date()})")
        except Exception as e: print(f"    Error: {e}")
        cur=ce; time.sleep(13)
    if not all_data: raise ValueError("No data")
    df=pd.concat(all_data).sort_values("datetime").reset_index(drop=True)
    return df.drop_duplicates(subset="datetime")

def fetch_yfinance():
    import yfinance as yf
    t=yf.Ticker("SPY"); parts=[]; end=datetime.now()
    for i in range(0,30,7):
        s=end-timedelta(days=7)
        try:
            c=t.history(start=s.strftime("%Y-%m-%d"),end=end.strftime("%Y-%m-%d"),interval="1m")
            if len(c)>0:
                c=c.reset_index()
                c=c.rename(columns={"Datetime":"datetime","Date":"datetime","Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"})
                if "datetime" not in c.columns:
                    for col in c.columns:
                        if "date" in col.lower(): c=c.rename(columns={col:"datetime"}); break
                parts.append(c[["datetime","open","high","low","close","volume"]])
        except: pass
        end=s
    if not parts: raise ValueError("No data")
    return pd.concat(parts).sort_values("datetime").reset_index(drop=True)

def make_sessions(df):
    from datetime import time as dtime
    df["datetime"]=pd.to_datetime(df["datetime"])
    avg=df["close"].mean()
    if avg<1000:
        print(f"  SPY detected (avg ${avg:.0f}). Scaling ×10.")
        for c in ["open","high","low","close"]: df[c]=df[c]*10
    if df["datetime"].dt.tz is None:
        df["datetime"]=df["datetime"].dt.tz_localize("America/New_York",ambiguous="NaT",nonexistent="NaT")
    else:
        df["datetime"]=df["datetime"].dt.tz_convert("America/New_York")
    df=df.dropna(subset=["datetime"])
    df["time"]=df["datetime"].dt.time; df["date"]=df["datetime"].dt.date
    df=df[(df["time"]>=dtime(9,30))&(df["time"]<=dtime(15,59))]
    out=[]
    for _,g in df.groupby("date"):
        if len(g)<30: continue
        s=g.copy().reset_index(drop=True)
        for c in ["open","high","low","close"]:
            s[c]=(s[c]/TICK_SIZE).round()*TICK_SIZE
        tp=(s["high"]+s["low"]+s["close"])/3
        cv=s["volume"].cumsum().replace(0,np.nan)
        s["vwap"]=(tp*s["volume"]).cumsum()/cv
        s["vwap"]=s["vwap"].ffill()
        out.append(s)
    print(f"  {len(out)} sessions")
    return out

# ============================================================
# INDICATORS
# ============================================================

class TPO:
    def __init__(self):
        self.hist=defaultdict(int); self.cp=-1; self.cls=set(); self.poc=None
        self.poc_hist=[]
    def reset(self):
        self.hist=defaultdict(int); self.cp=-1; self.cls=set(); self.poc=None
        self.poc_hist=[]
    def update(self,idx,hi,lo,close):
        p=idx//30
        if p!=self.cp:
            if self.cp>=0:
                for lv in self.cls: self.hist[lv]+=1
            self.cp=p; self.cls=set()
        lv=round(lo); h=round(hi)
        while lv<=h: self.cls.add(lv); lv+=1
        tmp=dict(self.hist)
        for lv in self.cls: tmp[lv]=tmp.get(lv,0)+1
        if tmp:
            mx=max(tmp.values())
            self.poc=min([p for p,c in tmp.items() if c==mx],key=lambda p:abs(p-close))
        self.poc_hist.append(self.poc)
        return self.poc
    def migration_speed(self,lb=20):
        if len(self.poc_hist)<lb: return 0
        r=[p for p in self.poc_hist[-lb:] if p is not None]
        if len(r)<2: return 0
        return abs(r[-1]-r[0])/len(r)

class VolPOC:
    def __init__(self):
        self.vh=defaultdict(float); self.vpoc=None
        self.vpoc_hist=[]
    def reset(self):
        self.vh=defaultdict(float); self.vpoc=None; self.vpoc_hist=[]
    def update(self,hi,lo,close,vol):
        lo_b=round(lo); hi_b=round(hi)
        n=max(1,int(hi_b-lo_b)+1)
        vpl=vol/n; lv=lo_b
        while lv<=hi_b: self.vh[lv]+=vpl; lv+=1
        if self.vh: self.vpoc=max(self.vh,key=self.vh.get)
        self.vpoc_hist.append(self.vpoc)
        return self.vpoc
    def migration_speed(self,lb=20):
        if len(self.vpoc_hist)<lb: return 0
        r=[p for p in self.vpoc_hist[-lb:] if p is not None]
        if len(r)<2: return 0
        return abs(r[-1]-r[0])/len(r)

class VWAPStdDev:
    def __init__(self):
        self.ctv=0; self.cv=0; self.ct2v=0; self.vwap=None; self.std=None; self.n=0
    def reset(self):
        self.ctv=0; self.cv=0; self.ct2v=0; self.vwap=None; self.std=None; self.n=0
    def update(self,hi,lo,close,vol):
        tp=(hi+lo+close)/3
        self.ctv+=tp*vol; self.cv+=vol; self.ct2v+=(tp**2)*vol; self.n+=1
        if self.cv>0:
            self.vwap=self.ctv/self.cv
            self.std=np.sqrt(max(self.ct2v/self.cv-self.vwap**2,0))
        return self.vwap,self.std
    def z_from_level(self,price,level):
        if self.std is None or self.std<TICK_SIZE or self.n<15: return None
        return (price-level)/self.std

# ============================================================
# TRADE
# ============================================================

class Dir(Enum):
    LONG=1; SHORT=-1

@dataclass
class Trade:
    etime:object; xtime:object; d:Dir
    ep:float; xp:float=0; cts:int=1
    sl:float=0; tp:float=0
    pnl:float=0; ticks:float=0
    strat:str=""; closed:bool=False
    ebar:int=0; zs:float=0
    def close(self,p,t):
        self.xp=p; self.xtime=t
        self.ticks=((p-self.ep) if self.d==Dir.LONG else (self.ep-p))/TICK_SIZE
        self.pnl=self.ticks*TICK_VALUE*self.cts; self.closed=True

# ============================================================
# UNIFIED STRATEGY — parameterized target mode
# ============================================================

class POCReversion:
    """
    Same v9 Full Filters strategy. Only difference: what level we target.
    
    target_mode:
      "tpo"      — target TPO POC (time-based)
      "volume"   — target Volume POC (capital-based)
      "composite" — average when close, volume when diverged
      "vwap"     — target VWAP (the actual mean)
    """

    def __init__(self, target_mode="tpo", contracts=2, z_thresh=1.5,
                 stop_ratio=0.5, trail_act=20, trail_dist=16,
                 poc_exit_min=12, max_trades=4,
                 entry_start=45, entry_end=360,
                 max_risk=800, time_stop=90,
                 max_poc_migration=0.15,
                 confluence_dist=3.0):

        self.mode=target_mode
        self.cts=contracts; self.name=f"target_{target_mode}"
        self.z_thresh=z_thresh; self.stop_r=stop_ratio
        self.trail_act=trail_act; self.trail_dist=trail_dist
        self.poc_exit=poc_exit_min; self.max_t=max_trades
        self.e_start=entry_start; self.e_end=entry_end
        self.max_risk=max_risk; self.time_stop=time_stop
        self.max_mig=max_poc_migration
        self.conf_dist=confluence_dist

        self.tpo=TPO(); self.vpoc=VolPOC(); self.vsd=VWAPStdDev()
        self.dt=0; self.bp=None

    def reset_day(self):
        self.tpo.reset(); self.vpoc.reset(); self.vsd.reset()
        self.dt=0; self.bp=None

    def _get_target(self, tpo_poc, vol_poc, vwap):
        """Select target level based on mode."""
        if self.mode == "tpo":
            return tpo_poc
        elif self.mode == "volume":
            return vol_poc
        elif self.mode == "composite":
            if tpo_poc is None: return vol_poc
            if vol_poc is None: return tpo_poc
            # If close (within confluence distance), average them
            if abs(tpo_poc - vol_poc) <= self.conf_dist:
                mid = (tpo_poc + vol_poc) / 2
                return round(mid / TICK_SIZE) * TICK_SIZE
            else:
                # Diverged — use volume POC (higher conviction)
                return vol_poc
        elif self.mode == "vwap":
            return round(vwap / TICK_SIZE) * TICK_SIZE if vwap else None
        return tpo_poc

    def _get_stability_poc(self):
        """Use TPO migration for stability check regardless of target mode."""
        return self.tpo.migration_speed(20)

    def _cts(self, sd):
        l = (sd/TICK_SIZE)*TICK_VALUE
        return max(1, min(int(self.max_risk/l), self.cts)) if l > 0 else 1

    def on_bar(self, idx, df, pos):
        row = df.iloc[idx]; price = row["close"]

        # Update all indicators
        tpo_poc = self.tpo.update(idx, row["high"], row["low"], price)
        vol_poc = self.vpoc.update(row["high"], row["low"], price, row["volume"])
        vwap, std = self.vsd.update(row["high"], row["low"], price, row["volume"])

        if idx < 15 or tpo_poc is None: return None
        if idx >= 370:
            if pos and not pos.closed: return {"action":"close"}
            return None

        # Get target based on mode
        target = self._get_target(tpo_poc, vol_poc, vwap)
        if target is None: return None

        # Z-score: distance from TARGET measured in VWAP std devs
        z = self.vsd.z_from_level(price, target)

        # ---- POSITION MANAGEMENT ----
        if pos and not pos.closed:
            # Use CURRENT target for exits (it develops)
            bars = idx - pos.ebar
            if pos.d == Dir.LONG:
                if self.bp is None or price > self.bp: self.bp = price
                ur = (self.bp - pos.ep) / TICK_SIZE
                cur = (price - pos.ep) / TICK_SIZE
                if row["low"] <= pos.sl: return {"action":"close","price":pos.sl}
                # Exit at current target (not entry-time target)
                if target and price >= target and cur >= self.poc_exit:
                    return {"action":"close","price":price}
                if ur >= self.trail_act:
                    tl = round((self.bp - self.trail_dist*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl > pos.sl:
                        if row["low"] <= tl: return {"action":"close","price":tl}
                        pos.sl = tl
                if ur >= 10 and pos.sl < pos.ep: pos.sl = pos.ep
                if bars > self.time_stop and cur > -4: return {"action":"close"}
            else:
                if self.bp is None or price < self.bp: self.bp = price
                ur = (pos.ep - self.bp) / TICK_SIZE
                cur = (pos.ep - price) / TICK_SIZE
                if row["high"] >= pos.sl: return {"action":"close","price":pos.sl}
                if target and price <= target and cur >= self.poc_exit:
                    return {"action":"close","price":price}
                if ur >= self.trail_act:
                    tl = round((self.bp + self.trail_dist*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl < pos.sl:
                        if row["high"] >= tl: return {"action":"close","price":tl}
                        pos.sl = tl
                if ur >= 10 and pos.sl > pos.ep: pos.sl = pos.ep
                if bars > self.time_stop and cur > -4: return {"action":"close"}
            return None

        # ---- ENTRY FILTERS ----
        if self.dt >= self.max_t: return None
        if idx < self.e_start or idx > self.e_end: return None
        if z is None: return None
        if abs(z) < self.z_thresh: return None

        # POC stability (always check TPO migration as session type proxy)
        if self._get_stability_poc() > self.max_mig:
            return None

        # Confluence filter — only for TPO mode (volume mode doesn't need it)
        if self.mode == "tpo" and vol_poc is not None:
            if abs(tpo_poc - vol_poc) > self.conf_dist:
                return None

        # ---- ENTRY ----
        dist = abs(price - target)
        sd = dist * self.stop_r
        sd = max(sd, 6*TICK_SIZE); sd = min(sd, 24*TICK_SIZE)

        if price < target:
            e = price; s = round((e-sd)/TICK_SIZE)*TICK_SIZE
            c = self._cts(e-s)
            self.dt += 1; self.bp = e
            return {"action":"buy","price":e,"stop":s,
                    "target":target+4*TICK_SIZE,"contracts":c,
                    "_entry_bar":idx,"_z":abs(z)}
        elif price > target:
            e = price; s = round((e+sd)/TICK_SIZE)*TICK_SIZE
            c = self._cts(s-e)
            self.dt += 1; self.bp = e
            return {"action":"sell","price":e,"stop":s,
                    "target":target-4*TICK_SIZE,"contracts":c,
                    "_entry_bar":idx,"_z":abs(z)}
        return None


# ============================================================
# ENGINE
# ============================================================

@dataclass
class St:
    pnl:float=0; eh:float=0; df:float=-2000
    dy:float=0; ok:bool=False; dead:bool=False; why:str=""; days:int=0
    def add(self,p):
        self.pnl+=p; self.dy+=p
        if self.pnl>self.eh: self.eh=self.pnl; self.df=self.eh-2000
        if self.pnl<=self.df: self.dead=True; self.why=f"DD ${self.pnl:.0f}<=${self.df:.0f}"
        if self.dy<=-1000: self.dead=True; self.why=f"Daily ${self.dy:.0f}"
        if self.pnl>=3000: self.ok=True
    def nd(self): self.dy=0; self.days+=1

@dataclass
class R:
    nm:str; tr:list; eq:list; dp:list
    ok:bool; dead:bool; why:str; pnl:float
    wr:float; aw:float; al:float; pf:float; mdd:float
    d:int; nt:int; rr:float=0

def run(strat,sess):
    s=St(); tr=[]; eq=[0.0]; dp=[]; pos=None
    for df in sess:
        if s.ok or s.dead: break
        s.nd(); strat.reset_day(); pos=None
        for i in range(len(df)):
            if s.ok or s.dead: break
            sig=strat.on_bar(i,df,pos)
            if sig is None: continue
            dt=df.iloc[i]["datetime"] if "datetime" in df.columns else i
            if sig["action"]=="close" and pos and not pos.closed:
                pos.close(sig.get("price",df.iloc[i]["close"]),dt)
                s.add(pos.pnl); tr.append(pos); eq.append(s.pnl); pos=None
            elif sig["action"] in ("buy","sell") and pos is None:
                ct=sig.get("contracts",getattr(strat,'cts',1))
                sd=abs(sig["price"]-sig["stop"])
                pl=(sd/TICK_SIZE)*TICK_VALUE*ct
                if pl>(1000+s.dy)*0.95: continue
                d=Dir.LONG if sig["action"]=="buy" else Dir.SHORT
                pos=Trade(etime=dt,xtime=None,d=d,ep=sig["price"],cts=ct,
                         sl=sig["stop"],tp=sig.get("target",sig["price"]),
                         strat=strat.name,ebar=sig.get("_entry_bar",i),
                         zs=sig.get("_z",0))
        if pos and not pos.closed:
            pos.close(df.iloc[-1]["close"],df.iloc[-1].get("datetime",0))
            s.add(pos.pnl); tr.append(pos); eq.append(s.pnl); pos=None
        dp.append(s.dy)
    w=[t for t in tr if t.pnl>0]; l=[t for t in tr if t.pnl<=0]
    wr=len(w)/max(len(tr),1)
    aw=np.mean([t.pnl for t in w]) if w else 0
    al=np.mean([abs(t.pnl) for t in l]) if l else 0
    gw=sum(t.pnl for t in w); gl=abs(sum(t.pnl for t in l))
    e=np.array(eq); mdd=abs((e-np.maximum.accumulate(e)).min())
    return R(nm=strat.name,tr=tr,eq=eq,dp=dp,ok=s.ok,dead=s.dead,
             why=s.why if s.dead else "",pnl=s.pnl,wr=wr,aw=aw,al=al,
             pf=gw/max(gl,1),mdd=mdd,d=s.days,nt=len(tr),
             rr=(aw/al) if al>0 else 0)

def mc(cls,params,sess,n=50):
    res=[]; rng=np.random.default_rng(42); ns=len(sess)
    for _ in range(n):
        idx=rng.choice(ns,size=min(ns,45),replace=True)
        res.append(run(cls(**params),[sess[i] for i in idx]))
    p=[r for r in res if r.ok]
    return {"pr":len(p)/n,"br":len([r for r in res if r.dead])/n,
            "pnl":np.mean([r.pnl for r in res]),
            "trades":np.mean([r.nt for r in res]),
            "wr":np.mean([r.wr for r in res if r.nt>0]),
            "rr":np.mean([r.rr for r in res if r.nt>0]),
            "days":np.mean([r.d for r in p]) if p else 0,
            "all":res}

def validate(cls,params,sessions,n_mc=50):
    split=int(len(sessions)*0.6)
    train=sessions[:split]; test=sessions[split:]
    print(f"    Train: {len(train)} | Test: {len(test)}")
    tr=mc(cls,params,train,n=n_mc)
    te=mc(cls,params,test,n=n_mc)
    return tr,te

# ============================================================
# MAIN
# ============================================================

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--polygon",type=str)
    parser.add_argument("--yfinance",action="store_true")
    parser.add_argument("--months",type=int,default=6)
    args=parser.parse_args()

    print("="*70)
    print("  TOPSTEP v11 — VOLUME POC vs TPO POC TARGET")
    print("  Which magnet does price actually revert to?")
    print("="*70)

    if args.polygon: raw=fetch_polygon(args.polygon,args.months)
    elif args.yfinance: raw=fetch_yfinance()
    else:
        print("\n  python3 topstep_v11.py --polygon YOUR_KEY --months 6")
        print("  python3 topstep_v11.py --yfinance"); return

    sess=make_sessions(raw)
    if len(sess)<10: print("Not enough!"); return

    # Shared params — everything identical except target_mode
    shared = {
        "contracts":2, "z_thresh":1.5,
        "stop_ratio":0.5, "trail_act":20, "trail_dist":16,
        "poc_exit_min":12, "max_trades":4,
        "entry_start":45, "entry_end":360,
        "max_risk":800, "time_stop":90,
        "max_poc_migration":0.15, "confluence_dist":3.0,
    }

    strategies = {
        "A) TPO POC (current)": {
            "class": POCReversion,
            "params": {**shared, "target_mode":"tpo"},
        },
        "B) Volume POC": {
            "class": POCReversion,
            "params": {**shared, "target_mode":"volume"},
        },
        "C) Composite (avg/vol)": {
            "class": POCReversion,
            "params": {**shared, "target_mode":"composite"},
        },
        "D) VWAP (reference)": {
            "class": POCReversion,
            "params": {**shared, "target_mode":"vwap"},
        },
    }

    # Walk-forward
    print(f"\n{'='*70}")
    print(f"  WALK-FORWARD VALIDATION ({len(sess)} sessions)")
    print(f"{'='*70}")

    results={}
    for name,cfg in strategies.items():
        print(f"\n  {name}...")
        tr,te=validate(cfg["class"],cfg["params"],sess,n_mc=50)
        results[name]={"train":tr,"test":te}
        tr_wr=f"{tr['wr']:.0%}" if not np.isnan(tr['wr']) else "—"
        te_wr=f"{te['wr']:.0%}" if not np.isnan(te['wr']) else "—"
        tr_rr=f"{tr['rr']:.1f}" if not np.isnan(tr['rr']) else "—"
        te_rr=f"{te['rr']:.1f}" if not np.isnan(te['rr']) else "—"
        print(f"    TRAIN → Pass:{tr['pr']:.0%} Blow:{tr['br']:.0%} PnL:${tr['pnl']:.0f} WR:{tr_wr} R:R:{tr_rr} Trades:{tr['trades']:.0f}")
        print(f"    TEST  → Pass:{te['pr']:.0%} Blow:{te['br']:.0%} PnL:${te['pnl']:.0f} WR:{te_wr} R:R:{te_rr} Trades:{te['trades']:.0f}")
        delta=(te['pr']-tr['pr'])*100
        overfit="YES ✗" if delta<-10 else ("MAYBE" if delta<-5 else "NO ✓")
        print(f"    Delta: {delta:+.0f}%  Overfit: {overfit}")

    # Summary
    print(f"\n{'='*70}")
    print(f"  TARGET COMPARISON — WHICH POC IS THE BETTER MAGNET?")
    print(f"{'='*70}")
    print(f"\n  {'Target':<25} {'Train%':>7} {'Test%':>7} {'Delta':>7} {'TestEV':>8} {'TestBlow':>9} {'R:R':>5}")
    print(f"  {'-'*25} {'-'*7} {'-'*7} {'-'*7} {'-'*8} {'-'*9} {'-'*5}")

    acc=ACCOUNTS["50K"]
    for name,r in results.items():
        tr=r["train"]; te=r["test"]
        tp=tr["pr"]*100; ts=te["pr"]*100; delta=ts-tp
        m=te["days"]/21 if te["days"]>0 else 2
        ev=te["pr"]*(5000-m*acc.monthly_fee-149)+(1-te["pr"])*(-1.5*acc.monthly_fee)
        te_rr=f"{te['rr']:.1f}" if not np.isnan(te['rr']) else "—"
        print(f"  {name:<25} {tp:>6.0f}% {ts:>6.0f}% {delta:>+6.0f}% ${ev:>7.0f} {te['br']:>8.0%} {te_rr:>5}")

    # Chronological
    print(f"\n{'='*70}")
    print(f"  CHRONOLOGICAL (full dataset)")
    print(f"{'='*70}")

    for name,cfg in strategies.items():
        s=cfg["class"](**cfg["params"])
        r=run(s,sess)
        st="PASSED ✓" if r.ok else ("BLOWN ✗" if r.dead else "IN PROGRESS")
        print(f"\n  {name}: {st}")
        print(f"    PnL:${r.pnl:.0f} Trades:{r.nt} WR:{r.wr:.0%} Days:{r.d} DD:${r.mdd:.0f} R:R:{r.rr:.1f}")
        if r.dead: print(f"    {r.why}")
        for t in r.tr[:10]:
            d="L" if t.d==Dir.LONG else "S"
            z=f" z={t.zs:.1f}" if t.zs else ""
            print(f"      {d} {t.cts}ct @{t.ep:.2f}→{t.xp:.2f} ${t.pnl:+.0f} ({t.ticks:+.0f}t){z}")

    # POC Divergence Analysis
    print(f"\n{'='*70}")
    print(f"  POC DIVERGENCE ANALYSIS")
    print(f"  How often do TPO POC and Volume POC disagree?")
    print(f"{'='*70}")

    divergences = []
    for df in sess:
        tpo=TPO(); vpoc=VolPOC()
        for i in range(len(df)):
            row=df.iloc[i]
            tp=tpo.update(i,row["high"],row["low"],row["close"])
            vp=vpoc.update(row["high"],row["low"],row["close"],row["volume"])
            if tp is not None and vp is not None and i>=45:
                divergences.append(abs(tp-vp))

    divs=np.array(divergences)
    print(f"\n  Measurements: {len(divs)}")
    print(f"  Mean divergence:    {divs.mean():.1f} pts ({divs.mean()/TICK_SIZE:.0f} ticks)")
    print(f"  Median divergence:  {np.median(divs):.1f} pts")
    print(f"  Within 3 pts:       {(divs<=3).mean():.0%} of the time")
    print(f"  Within 5 pts:       {(divs<=5).mean():.0%}")
    print(f"  Diverged > 5 pts:   {(divs>5).mean():.0%}")
    print(f"  Diverged > 10 pts:  {(divs>10).mean():.0%}")

    print(f"\n{'='*70}\n  DONE\n{'='*70}")

if __name__=="__main__": main()
