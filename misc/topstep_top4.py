"""
TOP 4 STRATEGIES — FINAL COMPARISON
=====================================
Head-to-head of the best strategies across all versions:

1. VWAP Target (v11) — 76% test pass rate
2. Volume POC Target (v11) — 44% test pass rate  
3. TPO POC Full Filters (v9) — 36% test pass rate
4. Dynamic Tight Guard (v10) — 32% test, 38% blow (survival play)

For each: Train / Test / Full Period results + visual report

USAGE: python3 topstep_top4.py --polygon YOUR_KEY --months 6
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
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
        self.hist=defaultdict(int); self.cp=-1; self.cls=set(); self.poc=None; self.poc_hist=[]
    def reset(self):
        self.hist=defaultdict(int); self.cp=-1; self.cls=set(); self.poc=None; self.poc_hist=[]
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
        self.poc_hist.append(self.poc); return self.poc
    def migration_speed(self,lb=20):
        if len(self.poc_hist)<lb: return 0
        r=[p for p in self.poc_hist[-lb:] if p is not None]
        if len(r)<2: return 0
        return abs(r[-1]-r[0])/len(r)

class VolPOC:
    def __init__(self):
        self.vh=defaultdict(float); self.vpoc=None
    def reset(self): self.vh=defaultdict(float); self.vpoc=None
    def update(self,hi,lo,close,vol):
        lo_b=round(lo); hi_b=round(hi)
        n=max(1,int(hi_b-lo_b)+1); vpl=vol/n; lv=lo_b
        while lv<=hi_b: self.vh[lv]+=vpl; lv+=1
        if self.vh: self.vpoc=max(self.vh,key=self.vh.get)
        return self.vpoc

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

class SessionATR:
    def __init__(self):
        self.ranges=[]; self.pc=None; self.session_atrs=[]; self.rolling=None
    def reset_day(self):
        if self.ranges and len(self.ranges)>=10:
            self.session_atrs.append(np.mean(self.ranges[-30:]))
            if len(self.session_atrs)>=5: self.rolling=np.mean(self.session_atrs[-20:])
        self.ranges=[]; self.pc=None
    def update(self,hi,lo,close):
        tr=max(hi-lo, abs(hi-self.pc) if self.pc else hi-lo, abs(lo-self.pc) if self.pc else hi-lo)
        self.ranges.append(tr); self.pc=close
    def atr(self): return np.mean(self.ranges[-30:]) if len(self.ranges)>=10 else None

# ============================================================
# TRADE
# ============================================================

class Dir(Enum):
    LONG=1; SHORT=-1

@dataclass
class Trade:
    etime:object; xtime:object; d:Dir
    ep:float; xp:float=0; cts:int=1; sl:float=0; tp:float=0
    pnl:float=0; ticks:float=0; strat:str=""; closed:bool=False
    ebar:int=0; zs:float=0
    def close(self,p,t):
        self.xp=p; self.xtime=t
        self.ticks=((p-self.ep) if self.d==Dir.LONG else (self.ep-p))/TICK_SIZE
        self.pnl=self.ticks*TICK_VALUE*self.cts; self.closed=True

# ============================================================
# STRATEGY: Unified with target_mode
# ============================================================

class UnifiedStrategy:
    def __init__(self, target_mode="vwap", contracts=2, z_thresh=1.5,
                 stop_ratio=0.5, trail_act=20, trail_dist=16,
                 exit_min=12, max_trades=4, entry_start=45, entry_end=360,
                 max_risk=800, time_stop=90, max_poc_migration=0.15,
                 confluence_dist=3.0,
                 # v10 dynamic z params
                 dynamic_z=False, z_high=2.0, low_atr=0.7, high_atr=1.3,
                 min_dist_ticks=10, max_dist_ticks=32):

        self.mode=target_mode; self.cts=contracts
        self.name=target_mode; self.z_thresh=z_thresh
        self.stop_r=stop_ratio; self.trail_act=trail_act; self.trail_dist=trail_dist
        self.exit_min=exit_min; self.max_t=max_trades
        self.e_start=entry_start; self.e_end=entry_end
        self.max_risk=max_risk; self.time_stop=time_stop
        self.max_mig=max_poc_migration; self.conf_dist=confluence_dist
        self.dyn_z=dynamic_z; self.z_hi=z_high
        self.low_atr=low_atr; self.high_atr=high_atr
        self.min_dist=min_dist_ticks; self.max_dist=max_dist_ticks

        self.tpo=TPO(); self.vpoc=VolPOC(); self.vsd=VWAPStdDev()
        self.atr=SessionATR()
        self.dt=0; self.bp=None

    def reset_day(self):
        self.atr.reset_day()
        self.tpo.reset(); self.vpoc.reset(); self.vsd.reset()
        self.dt=0; self.bp=None

    def _get_target(self, tpo_poc, vol_poc, vwap):
        if self.mode=="vwap":
            return round(vwap/TICK_SIZE)*TICK_SIZE if vwap else None
        elif self.mode=="volume_poc":
            return vol_poc
        elif self.mode=="tpo_poc":
            return tpo_poc
        return None

    def _get_z_thresh(self):
        if not self.dyn_z: return self.z_thresh
        a=self.atr.atr()
        if a is None or self.atr.rolling is None: return self.z_thresh
        ratio=a/self.atr.rolling if self.atr.rolling>0 else 1.0
        if ratio<self.low_atr or ratio>self.high_atr: return self.z_hi
        return self.z_thresh

    def _cts(self,sd):
        l=(sd/TICK_SIZE)*TICK_VALUE
        return max(1,min(int(self.max_risk/l),self.cts)) if l>0 else 1

    def on_bar(self,idx,df,pos):
        row=df.iloc[idx]; price=row["close"]
        tpo_poc=self.tpo.update(idx,row["high"],row["low"],price)
        vol_poc=self.vpoc.update(row["high"],row["low"],price,row["volume"])
        vwap,std=self.vsd.update(row["high"],row["low"],price,row["volume"])
        self.atr.update(row["high"],row["low"],price)

        if idx<15: return None
        if idx>=370:
            if pos and not pos.closed: return {"action":"close"}
            return None

        target=self._get_target(tpo_poc,vol_poc,vwap)
        if target is None: return None
        z=self.vsd.z_from_level(price,target)
        dist_ticks=abs(price-target)/TICK_SIZE

        # Position management
        if pos and not pos.closed:
            bars=idx-pos.ebar
            if pos.d==Dir.LONG:
                if self.bp is None or price>self.bp: self.bp=price
                ur=(self.bp-pos.ep)/TICK_SIZE; cur=(price-pos.ep)/TICK_SIZE
                if row["low"]<=pos.sl: return {"action":"close","price":pos.sl}
                if target and price>=target and cur>=self.exit_min:
                    return {"action":"close","price":price}
                if ur>=self.trail_act:
                    tl=round((self.bp-self.trail_dist*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl>pos.sl:
                        if row["low"]<=tl: return {"action":"close","price":tl}
                        pos.sl=tl
                if ur>=10 and pos.sl<pos.ep: pos.sl=pos.ep
                if bars>self.time_stop and cur>-4: return {"action":"close"}
            else:
                if self.bp is None or price<self.bp: self.bp=price
                ur=(pos.ep-self.bp)/TICK_SIZE; cur=(pos.ep-price)/TICK_SIZE
                if row["high"]>=pos.sl: return {"action":"close","price":pos.sl}
                if target and price<=target and cur>=self.exit_min:
                    return {"action":"close","price":price}
                if ur>=self.trail_act:
                    tl=round((self.bp+self.trail_dist*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl<pos.sl:
                        if row["high"]>=tl: return {"action":"close","price":tl}
                        pos.sl=tl
                if ur>=10 and pos.sl>pos.ep: pos.sl=pos.ep
                if bars>self.time_stop and cur>-4: return {"action":"close"}
            return None

        # Entry
        if self.dt>=self.max_t or idx<self.e_start or idx>self.e_end: return None
        if z is None: return None

        zt=self._get_z_thresh()
        if abs(z)<zt: return None

        # Distance guardrails (for dynamic tight guard)
        if self.dyn_z:
            if dist_ticks<self.min_dist or dist_ticks>self.max_dist: return None

        # Stability
        if self.tpo.migration_speed(20)>self.max_mig: return None

        # Confluence (only for TPO POC mode)
        if self.mode=="tpo_poc" and vol_poc is not None:
            if abs(tpo_poc-vol_poc)>self.conf_dist: return None

        dist=abs(price-target); sd=dist*self.stop_r
        sd=max(sd,6*TICK_SIZE); sd=min(sd,24*TICK_SIZE)

        if price<target:
            e=price; s=round((e-sd)/TICK_SIZE)*TICK_SIZE; c=self._cts(e-s)
            self.dt+=1; self.bp=e
            return {"action":"buy","price":e,"stop":s,"target":target+4*TICK_SIZE,
                    "contracts":c,"_entry_bar":idx,"_z":abs(z)}
        elif price>target:
            e=price; s=round((e+sd)/TICK_SIZE)*TICK_SIZE; c=self._cts(s-e)
            self.dt+=1; self.bp=e
            return {"action":"sell","price":e,"stop":s,"target":target-4*TICK_SIZE,
                    "contracts":c,"_entry_bar":idx,"_z":abs(z)}
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

def bt(strat,sess):
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
                ct=sig.get("contracts",strat.cts)
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
        res.append(bt(cls(**params),[sess[i] for i in idx]))
    p=[r for r in res if r.ok]
    return {"pr":len(p)/n,"br":len([r for r in res if r.dead])/n,
            "pnl":np.mean([r.pnl for r in res]),
            "trades":np.mean([r.nt for r in res]),
            "wr":np.mean([r.wr for r in res if r.nt>0]),
            "rr":np.mean([r.rr for r in res if r.nt>0]),
            "days":np.mean([r.d for r in p]) if p else 0,
            "all":res}

# ============================================================
# REPORT GENERATION
# ============================================================

def make_report(name, train_mc, test_mc, full_mc, chrono_full, filename):
    """Generate a full report PNG for one strategy."""
    acc=ACCOUNTS["50K"]
    theo=acc.max_drawdown/(acc.max_drawdown+acc.profit_target)

    fig=plt.figure(figsize=(22,24)); fig.patch.set_facecolor("#0d1117")
    gs=gridspec.GridSpec(4,3,hspace=0.4,wspace=0.3)

    def sty(ax,t):
        ax.set_facecolor("#0d1117")
        ax.set_title(t,color="white",fontsize=11,fontweight="bold",pad=10)
        ax.tick_params(colors="#8b949e")
        for s in ["top","right"]: ax.spines[s].set_visible(False)
        for s in ["left","bottom"]: ax.spines[s].set_color("#30363d")
        ax.xaxis.label.set_color("#8b949e"); ax.yaxis.label.set_color("#8b949e")

    # Row 1: Equity curves — Train / Test / Full
    for i,(label,data,color) in enumerate([
        ("TRAIN (in-sample)",train_mc,"#00d4aa"),
        ("TEST (out-of-sample)",test_mc,"#ff6b6b"),
        ("FULL PERIOD",full_mc,"#ffd93d")]):
        ax=fig.add_subplot(gs[0,i]); sty(ax,f"{label}\nEquity Curves (30 sims)")
        for res in data["all"][:30]:
            cl="#00ff88" if res.ok else ("#ff4444" if res.dead else "#555555")
            ax.plot(res.eq,color=cl,alpha=0.5 if res.ok else 0.2,linewidth=0.8)
        ax.axhline(y=3000,color="#ffd700",linestyle="--",alpha=0.7)
        ax.axhline(y=-2000,color="#ff4444",linestyle="--",alpha=0.7)
        ax.set_xlabel("Trade #"); ax.set_ylabel("P&L ($)")
        # Add pass/blow text
        ax.text(0.02,0.98,f"Pass: {data['pr']:.0%}\nBlow: {data['br']:.0%}",
                transform=ax.transAxes,va="top",color="white",fontsize=10,
                bbox=dict(boxstyle="round",facecolor="#1a1f29",edgecolor="#30363d"))

    # Row 2: P&L distributions — Train / Test / Full
    for i,(label,data,color) in enumerate([
        ("TRAIN P&L",train_mc,"#00d4aa"),
        ("TEST P&L",test_mc,"#ff6b6b"),
        ("FULL P&L",full_mc,"#ffd93d")]):
        ax=fig.add_subplot(gs[1,i]); sty(ax,f"{label} Distribution")
        pnls=[r.pnl for r in data["all"]]
        ax.hist(pnls,bins=20,color=color,alpha=0.7,edgecolor=color)
        ax.axvline(x=3000,color="#ffd700",linestyle="--",alpha=0.7)
        ax.axvline(x=0,color="white",alpha=0.3)
        ax.set_xlabel("Final P&L ($)"); ax.set_ylabel("Frequency")
        ax.text(0.02,0.98,f"Avg: ${np.mean(pnls):.0f}\nMed: ${np.median(pnls):.0f}",
                transform=ax.transAxes,va="top",color="white",fontsize=9,
                bbox=dict(boxstyle="round",facecolor="#1a1f29",edgecolor="#30363d"))

    # Row 3: Chronological equity curve (full) + Daily P&L + Stats
    ax=fig.add_subplot(gs[2,:2]); sty(ax,"CHRONOLOGICAL — Full Period")
    status="PASSED ✓" if chrono_full.ok else ("BLOWN ✗" if chrono_full.dead else "IN PROGRESS")
    eq=chrono_full.eq
    colors_line=[]
    for i in range(len(eq)):
        if eq[i]>=3000: colors_line.append("#00ff88")
        elif eq[i]<=-2000: colors_line.append("#ff4444")
        else: colors_line.append("#4ecdc4")
    ax.plot(eq,color="#4ecdc4",linewidth=1.5)
    ax.axhline(y=3000,color="#ffd700",linestyle="--",alpha=0.7,label="Target $3K")
    ax.axhline(y=-2000,color="#ff4444",linestyle="--",alpha=0.7,label="Max DD -$2K")
    ax.fill_between(range(len(eq)),eq,0,where=[e>0 for e in eq],alpha=0.1,color="#00d4aa")
    ax.fill_between(range(len(eq)),eq,0,where=[e<=0 for e in eq],alpha=0.1,color="#ff4444")
    ax.set_xlabel("Trade #"); ax.set_ylabel("P&L ($)")
    ax.legend(fontsize=8,facecolor="#0d1117",edgecolor="#30363d",labelcolor="white")
    ax.text(0.02,0.98,f"{status}\nPnL: ${chrono_full.pnl:.0f} | {chrono_full.nt} trades | {chrono_full.d} days",
            transform=ax.transAxes,va="top",color="white",fontsize=11,fontweight="bold",
            bbox=dict(boxstyle="round",facecolor="#1a1f29",edgecolor="#30363d"))

    # Daily P&L bar chart
    ax=fig.add_subplot(gs[2,2]); sty(ax,"Daily P&L")
    if chrono_full.dp:
        colors_bar=["#00d4aa" if d>0 else "#ff4444" for d in chrono_full.dp]
        ax.bar(range(len(chrono_full.dp)),chrono_full.dp,color=colors_bar,alpha=0.7)
        ax.axhline(y=0,color="white",alpha=0.3)
        ax.set_xlabel("Day"); ax.set_ylabel("P&L ($)")

    # Row 4: Summary table + EV comparison
    ax=fig.add_subplot(gs[3,:2]); sty(ax,"Strategy Statistics"); ax.axis("off")
    hdr=["Period","Pass%","Blow%","Win%","R:R","Avg PnL","Trades","EV"]
    rows=[]
    for label,data in [("TRAIN",train_mc),("TEST",test_mc),("FULL",full_mc)]:
        m=data["days"]/21 if data["days"]>0 else 2
        ev=data["pr"]*(5000-m*acc.monthly_fee-149)+(1-data["pr"])*(-1.5*acc.monthly_fee)
        wr_s=f"{data['wr']:.0%}" if not np.isnan(data['wr']) else "—"
        rr_s=f"{data['rr']:.1f}" if not np.isnan(data['rr']) else "—"
        rows.append([label,f"{data['pr']:.0%}",f"{data['br']:.0%}",wr_s,rr_s,
                      f"${data['pnl']:.0f}",f"{data['trades']:.0f}",f"${ev:.0f}"])
    # Add chrono
    rows.append(["CHRONO",
                  "PASS" if chrono_full.ok else ("BLOW" if chrono_full.dead else "LIVE"),
                  f"{'—'}", f"{chrono_full.wr:.0%}", f"{chrono_full.rr:.1f}",
                  f"${chrono_full.pnl:.0f}", f"{chrono_full.nt}", "—"])

    t=ax.table(cellText=rows,colLabels=hdr,loc="center",cellLoc="center",
               colColours=["#1a1f29"]*len(hdr))
    t.auto_set_font_size(False); t.set_fontsize(10); t.scale(1.2,2.5)
    for cell in t.get_celld().values():
        cell.set_facecolor("#0d1117"); cell.set_edgecolor("#30363d")
        cell.set_text_props(color="white")

    # Overfit indicator
    ax=fig.add_subplot(gs[3,2]); sty(ax,"Overfit Check"); ax.axis("off")
    delta=(test_mc["pr"]-train_mc["pr"])*100
    overfit="NO ✓" if delta>=-5 else ("MAYBE" if delta>=-10 else "YES ✗")
    color_of="#00d4aa" if "NO" in overfit else ("#ffd93d" if "MAYBE" in overfit else "#ff4444")

    ax.text(0.5,0.7,f"Train→Test Delta",ha="center",color="#8b949e",fontsize=12,
            transform=ax.transAxes)
    ax.text(0.5,0.5,f"{delta:+.0f}%",ha="center",color=color_of,fontsize=36,
            fontweight="bold",transform=ax.transAxes)
    ax.text(0.5,0.3,f"Overfit: {overfit}",ha="center",color=color_of,fontsize=16,
            fontweight="bold",transform=ax.transAxes)

    fig.suptitle(f"TOPSTEP 50K — {name.upper()}\n124 Sessions | Walk-Forward 60/40 | Monte Carlo 50 sims",
                 color="white",fontsize=16,fontweight="bold",y=0.99)
    plt.savefig(filename,dpi=150,bbox_inches="tight",facecolor="#0d1117")
    plt.close()
    print(f"  Saved: {filename}")


def make_comparison_report(all_data, filename):
    """Side-by-side comparison of all 4 strategies."""
    acc=ACCOUNTS["50K"]
    theo=acc.max_drawdown/(acc.max_drawdown+acc.profit_target)
    names=list(all_data.keys())
    pal=["#00d4aa","#4ecdc4","#ffd93d","#ff6b6b"]
    cm={n:pal[i] for i,n in enumerate(names)}

    fig=plt.figure(figsize=(24,20)); fig.patch.set_facecolor("#0d1117")
    gs=gridspec.GridSpec(3,2,hspace=0.35,wspace=0.25)

    def sty(ax,t):
        ax.set_facecolor("#0d1117")
        ax.set_title(t,color="white",fontsize=13,fontweight="bold",pad=12)
        ax.tick_params(colors="#8b949e")
        for s in ["top","right"]: ax.spines[s].set_visible(False)
        for s in ["left","bottom"]: ax.spines[s].set_color("#30363d")
        ax.xaxis.label.set_color("#8b949e"); ax.yaxis.label.set_color("#8b949e")

    # Pass rates: Train vs Test vs Full
    ax=fig.add_subplot(gs[0,0]); sty(ax,"Pass Rate: Train / Test / Full")
    x=np.arange(len(names)); w=0.25
    for i,(period,key) in enumerate([("Train","train"),("Test","test"),("Full","full")]):
        vals=[all_data[n][key]["pr"]*100 for n in names]
        cols=["#00d4aa","#ff6b6b","#ffd93d"][i]
        bars=ax.bar(x+i*w-w,vals,w,color=cols,alpha=0.8,label=period)
        for b,v in zip(bars,vals):
            ax.text(b.get_x()+b.get_width()/2,b.get_height()+1,f"{v:.0f}%",
                    ha="center",color="white",fontsize=8,fontweight="bold")
    ax.set_xticks(x); ax.set_xticklabels([n[:20] for n in names],rotation=20,ha="right",fontsize=9)
    ax.axhline(y=theo*100,color="white",linestyle="--",alpha=0.3,label=f"Random: {theo:.0%}")
    ax.set_ylabel("Pass Rate (%)"); ax.set_ylim(0,100)
    ax.legend(fontsize=8,facecolor="#0d1117",edgecolor="#30363d",labelcolor="white")

    # Blow rates
    ax=fig.add_subplot(gs[0,1]); sty(ax,"Blow Rate: Train / Test / Full")
    for i,(period,key) in enumerate([("Train","train"),("Test","test"),("Full","full")]):
        vals=[all_data[n][key]["br"]*100 for n in names]
        cols=["#00d4aa","#ff6b6b","#ffd93d"][i]
        bars=ax.bar(x+i*w-w,vals,w,color=cols,alpha=0.8,label=period)
        for b,v in zip(bars,vals):
            ax.text(b.get_x()+b.get_width()/2,b.get_height()+1,f"{v:.0f}%",
                    ha="center",color="white",fontsize=8,fontweight="bold")
    ax.set_xticks(x); ax.set_xticklabels([n[:20] for n in names],rotation=20,ha="right",fontsize=9)
    ax.set_ylabel("Blow Rate (%)"); ax.set_ylim(0,100)
    ax.legend(fontsize=8,facecolor="#0d1117",edgecolor="#30363d",labelcolor="white")

    # EV comparison
    ax=fig.add_subplot(gs[1,0]); sty(ax,"Expected Value Per Attempt ($)")
    for i,(period,key) in enumerate([("Train","train"),("Test","test"),("Full","full")]):
        evs=[]
        for n in names:
            d=all_data[n][key]
            m=d["days"]/21 if d["days"]>0 else 2
            evs.append(d["pr"]*(5000-m*acc.monthly_fee-149)+(1-d["pr"])*(-1.5*acc.monthly_fee))
        cols=["#00d4aa","#ff6b6b","#ffd93d"][i]
        bars=ax.bar(x+i*w-w,evs,w,color=cols,alpha=0.8,label=period)
        for b,v in zip(bars,evs):
            ax.text(b.get_x()+b.get_width()/2,b.get_height()+(30 if v>=0 else -80),
                    f"${v:.0f}",ha="center",color="white",fontsize=7,fontweight="bold")
    ax.set_xticks(x); ax.set_xticklabels([n[:20] for n in names],rotation=20,ha="right",fontsize=9)
    ax.set_ylabel("EV ($)"); ax.axhline(y=0,color="white",alpha=0.3)
    ax.legend(fontsize=8,facecolor="#0d1117",edgecolor="#30363d",labelcolor="white")

    # Win Rate vs R:R scatter (test set)
    ax=fig.add_subplot(gs[1,1]); sty(ax,"Win Rate vs R:R (Test Set)")
    for n in names:
        d=all_data[n]["test"]
        wr=d["wr"]*100 if not np.isnan(d["wr"]) else 0
        rr=d["rr"] if not np.isnan(d["rr"]) else 0
        ax.scatter(wr,rr,s=200,c=cm[n],edgecolors="white",linewidth=1,zorder=3)
        ax.annotate(n[:18],(wr,rr),textcoords="offset points",xytext=(8,6),
                    fontsize=9,color="#8b949e")
    ax.set_xlabel("Win Rate (%)"); ax.set_ylabel("R:R")
    ax.axhline(y=1.0,color="#ffd700",linestyle="--",alpha=0.3)

    # Full comparison table
    ax=fig.add_subplot(gs[2,:]); sty(ax,"COMPLETE COMPARISON TABLE"); ax.axis("off")
    hdr=["Strategy","Train Pass","Test Pass","Full Pass","Delta","Test Blow","Test EV",
         "Chrono","Chrono PnL","Days"]
    rows=[]
    for n in names:
        tr=all_data[n]["train"]; te=all_data[n]["test"]; fu=all_data[n]["full"]
        ch=all_data[n]["chrono"]
        delta=(te["pr"]-tr["pr"])*100
        m=te["days"]/21 if te["days"]>0 else 2
        ev=te["pr"]*(5000-m*acc.monthly_fee-149)+(1-te["pr"])*(-1.5*acc.monthly_fee)
        ch_status="PASS" if ch.ok else ("BLOW" if ch.dead else "LIVE")
        rows.append([n[:22],f"{tr['pr']:.0%}",f"{te['pr']:.0%}",f"{fu['pr']:.0%}",
                      f"{delta:+.0f}%",f"{te['br']:.0%}",f"${ev:.0f}",
                      ch_status,f"${ch.pnl:.0f}",f"{ch.d}"])
    t=ax.table(cellText=rows,colLabels=hdr,loc="center",cellLoc="center",
               colColours=["#1a1f29"]*len(hdr))
    t.auto_set_font_size(False); t.set_fontsize(9); t.scale(1.1,2.5)
    for cell in t.get_celld().values():
        cell.set_facecolor("#0d1117"); cell.set_edgecolor("#30363d")
        cell.set_text_props(color="white")

    fig.suptitle("TOPSTEP TOP 4 STRATEGIES — HEAD TO HEAD\n124 Sessions | 6 Months SPY Data | Walk-Forward Validated",
                 color="white",fontsize=18,fontweight="bold",y=0.99)
    plt.savefig(filename,dpi=150,bbox_inches="tight",facecolor="#0d1117")
    plt.close()
    print(f"  Saved: {filename}")


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
    print("  TOP 4 STRATEGIES — FINAL COMPARISON")
    print("="*70)

    if args.polygon: raw=fetch_polygon(args.polygon,args.months)
    elif args.yfinance: raw=fetch_yfinance()
    else: print("\n  python3 topstep_top4.py --polygon YOUR_KEY --months 6"); return

    sess=make_sessions(raw)
    if len(sess)<10: print("Not enough!"); return

    split=int(len(sess)*0.6)
    train=sess[:split]; test=sess[split:]
    print(f"\n  Total: {len(sess)} sessions")
    print(f"  Train: {len(train)} | Test: {len(test)}")

    # Top 4 strategies
    strategies={
        "1. VWAP Target": {
            "target_mode":"vwap","contracts":2,"z_thresh":1.5,
            "max_trades":4,"entry_start":45,"entry_end":360,
        },
        "2. Volume POC": {
            "target_mode":"volume_poc","contracts":2,"z_thresh":1.5,
            "max_trades":4,"entry_start":45,"entry_end":360,
        },
        "3. TPO POC (Full Filt)": {
            "target_mode":"tpo_poc","contracts":2,"z_thresh":1.5,
            "max_trades":4,"entry_start":45,"entry_end":360,
            "confluence_dist":3.0,
        },
        "4. Dynamic Tight Guard": {
            "target_mode":"tpo_poc","contracts":2,"z_thresh":1.5,
            "max_trades":4,"entry_start":45,"entry_end":360,
            "dynamic_z":True,"z_high":2.0,"low_atr":0.7,"high_atr":1.3,
            "min_dist_ticks":10,"max_dist_ticks":32,
        },
    }

    # Shared params
    shared={"stop_ratio":0.5,"trail_act":20,"trail_dist":16,
            "exit_min":12,"max_risk":800,"time_stop":90,
            "max_poc_migration":0.15}

    all_data={}

    for name,params in strategies.items():
        full_params={**shared,**params}
        print(f"\n{'='*70}")
        print(f"  {name}")
        print(f"{'='*70}")

        # Train MC
        print(f"  Running TRAIN Monte Carlo...")
        train_mc=mc(UnifiedStrategy,full_params,train,n=50)
        print(f"    Pass:{train_mc['pr']:.0%} Blow:{train_mc['br']:.0%} PnL:${train_mc['pnl']:.0f}")

        # Test MC
        print(f"  Running TEST Monte Carlo...")
        test_mc=mc(UnifiedStrategy,full_params,test,n=50)
        print(f"    Pass:{test_mc['pr']:.0%} Blow:{test_mc['br']:.0%} PnL:${test_mc['pnl']:.0f}")

        # Full MC
        print(f"  Running FULL Monte Carlo...")
        full_mc=mc(UnifiedStrategy,full_params,sess,n=50)
        print(f"    Pass:{full_mc['pr']:.0%} Blow:{full_mc['br']:.0%} PnL:${full_mc['pnl']:.0f}")

        # Chronological full
        print(f"  Running chronological...")
        strat=UnifiedStrategy(**full_params)
        chrono=bt(strat,sess)
        status="PASSED ✓" if chrono.ok else ("BLOWN ✗" if chrono.dead else "IN PROGRESS")
        print(f"    {status}: PnL=${chrono.pnl:.0f} Trades:{chrono.nt} WR:{chrono.wr:.0%} Days:{chrono.d}")

        delta=(test_mc["pr"]-train_mc["pr"])*100
        overfit="NO ✓" if delta>=-5 else ("MAYBE" if delta>=-10 else "YES ✗")
        print(f"    Delta: {delta:+.0f}%  Overfit: {overfit}")

        all_data[name]={"train":train_mc,"test":test_mc,"full":full_mc,"chrono":chrono}

        # Individual report
        safe_name=name.replace(" ","_").replace("(","").replace(")","").replace(".","").replace("/","")
        make_report(name,train_mc,test_mc,full_mc,chrono,f"report_{safe_name}.png")

    # Comparison report
    print(f"\n{'='*70}")
    print(f"  GENERATING COMPARISON REPORT")
    print(f"{'='*70}")
    make_comparison_report(all_data,"topstep_top4_comparison.png")

    # Final summary
    print(f"\n{'='*70}")
    print(f"  FINAL RANKING")
    print(f"{'='*70}")

    acc=ACCOUNTS["50K"]
    print(f"\n  {'Rank':<5} {'Strategy':<25} {'Test Pass':>10} {'Test Blow':>10} {'Test EV':>9} {'Delta':>7} {'Chrono':>8}")
    print(f"  {'-'*5} {'-'*25} {'-'*10} {'-'*10} {'-'*9} {'-'*7} {'-'*8}")

    ranked=sorted(all_data.items(),key=lambda x:x[1]["test"]["pr"],reverse=True)
    for rank,(name,data) in enumerate(ranked,1):
        te=data["test"]; ch=data["chrono"]
        m=te["days"]/21 if te["days"]>0 else 2
        ev=te["pr"]*(5000-m*acc.monthly_fee-149)+(1-te["pr"])*(-1.5*acc.monthly_fee)
        delta=(te["pr"]-data["train"]["pr"])*100
        ch_s="PASS" if ch.ok else ("BLOW" if ch.dead else "LIVE")
        print(f"  {rank:<5} {name:<25} {te['pr']:>9.0%} {te['br']:>9.0%} ${ev:>7.0f} {delta:>+6.0f}% {ch_s:>8}")

    print(f"\n  Files generated:")
    for name in strategies:
        safe=name.replace(" ","_").replace("(","").replace(")","").replace(".","").replace("/","")
        print(f"    report_{safe}.png")
    print(f"    topstep_top4_comparison.png")

    print(f"\n{'='*70}")
    print(f"  DONE")
    print(f"{'='*70}")

if __name__=="__main__": main()
