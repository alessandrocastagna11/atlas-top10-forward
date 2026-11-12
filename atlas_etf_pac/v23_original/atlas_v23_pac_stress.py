#!/usr/bin/env python3
"""ATLAS V2.3-PAC Stress Filter Frozen — stdlib-only reproducible backtest.

V2.2-PAC R3 is preserved except for the core decision after a second
satellite exit. V2.3 adds a causal 3-block stress detector using only
information from the previous completed month:

1) Credit: Moody's Baa-Aaa spread is >= rolling 10y 80th percentile OR
   widens by >= 0.50 percentage points over 3 months.
2) Breadth: <=2 of 6 Fama/French Size x Book-to-Market portfolios have a
   positive compounded return over the previous 3 completed months.
3) Rates: Shiller GS10 minus annualized Fama/French monthly RF is <= the
   rolling 10y 20th percentile.

At the second satellite exit:
- stress score >= 2: core exits immediately with that execution;
- score == 1: core enters WARNING for max 10 sessions;
- score == 0: gate remains active and keeps monitoring the same episode;
- WARNING exits core if score becomes >=2, drawdown worsens by 3pp,
  Fast10 fires, or 10 sessions expire while satellite remains OUT;
- legacy additional 5pp bear deterioration remains an emergency override.

The gate is episode-scoped: it is disabled on satellite re-entry, core exit,
core re-entry, or state reset. It cannot spuriously re-trigger after a core
re-entry without a new second satellite exit.

PAC/cost/tax assumptions are unchanged from V2.2-PAC R3.
No third-party Python packages are required.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DEFAULT_CONFIG = ROOT / "config.json"


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def years_between(a: str, b: str) -> float:
    return (parse_date(b) - parse_date(a)).days / 365.25


def rolling_mean(a, i, n):
    j0=max(0,i-n+1)
    xs=a[j0:i+1]
    return sum(xs)/len(xs)


def sma(a, i, n):
    if i<n-1:
        return math.nan
    return sum(a[i-n+1:i+1])/n


def mom(a, i, n):
    if i<n or not math.isfinite(a[i]) or not math.isfinite(a[i-n]) or a[i-n]<=0:
        return math.nan
    return a[i]/a[i-n]-1.0


def percentile(values, q):
    xs=sorted(x for x in values if math.isfinite(x))
    if not xs:
        return math.nan
    pos=(len(xs)-1)*q
    lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi:
        return xs[lo]
    return xs[lo]+(xs[hi]-xs[lo])*(pos-lo)


def max_dd(values):
    peak=-math.inf; out=0.0
    for v in values:
        peak=max(peak,v); out=min(out,v/peak-1.0)
    return out


def xirr(flows):
    t0=parse_date(flows[0][0])
    arr=[((parse_date(d)-t0).days/365.25,v) for d,v in flows]
    def f(r): return sum(v/((1+r)**t) for t,v in arr)
    lo,hi=-0.9999,1.0; flo,fhi=f(lo),f(hi); guard=0
    while flo*fhi>0 and hi<1e6 and guard<50:
        hi*=2; fhi=f(hi); guard+=1
    for _ in range(250):
        mid=(lo+hi)/2; fm=f(mid)
        if flo*fm<=0:
            hi=mid; fhi=fm
        else:
            lo=mid; flo=fm
    return (lo+hi)/2


def previous_month_key(date_str):
    y=int(date_str[:4]); m=int(date_str[5:7])-1
    if m==0:
        y-=1; m=12
    return f"{y}-{m:02d}"


def add_months(month_key, delta):
    y=int(month_key[:4]); m=int(month_key[5:7])+delta
    while m<=0:
        m+=12; y-=1
    while m>12:
        m-=12; y+=1
    return f"{y}-{m:02d}"


def load_ff(start_date,end_date):
    path=DATA/"F-F_Research_Data_Factors_daily.csv"
    if not path.exists(): raise FileNotFoundError(f"Missing {path}. Run data/fetch_pinned_data.py first.")
    out=[]
    with path.open("r",encoding="utf-8-sig",errors="replace",newline="") as f:
        for line in f:
            p=[x.strip() for x in line.split(",")]
            if len(p)<5 or len(p[0])!=8 or not p[0].isdigit(): continue
            ds=p[0]; d=f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
            if d<start_date or d>end_date: continue
            try: mktrf=float(p[1])/100.0; rf=float(p[4])/100.0
            except ValueError: continue
            out.append({"date":d,"ret":mktrf+rf,"rf":rf})
    if not out: raise RuntimeError("No Fama/French rows in requested date range.")
    return out


def load_shiller_monthly():
    path=DATA/"shiller.csv"
    if not path.exists(): raise FileNotFoundError(f"Missing {path}. Run data/fetch_pinned_data.py first.")
    bond={}; gs10={}
    with path.open("r",encoding="utf-8-sig",errors="replace",newline="") as f:
        for line in f:
            p=line.replace('"','').rstrip("\n\r").split(",")
            ds=(p[0] if p else "").strip()
            if "." not in ds: continue
            ys,ms=ds.split(".",1)
            if not ys.isdigit() or not ms.isdigit(): continue
            y=int(ys)
            if len(ms)==2 and ms.startswith("0"): m=int(ms)
            elif ms=="1": m=10
            else: m=int(ms)
            if not 1<=m<=12: continue
            k=f"{y}-{m:02d}"
            if len(p)>18:
                try: cpi=float(p[4]); real_bond=float(p[18])
                except ValueError: pass
                else:
                    if cpi>0 and real_bond>0: bond[k]=cpi*real_bond
            if len(p)>6:
                try: g=float(p[6])
                except ValueError: pass
                else:
                    if math.isfinite(g): gs10[k]=g
    return bond,gs10


def load_fred_monthly(filename):
    path=DATA/filename
    if not path.exists(): raise FileNotFoundError(f"Missing {path}. Run data/fetch_pinned_data.py first.")
    out={}
    with path.open("r",encoding="utf-8-sig",errors="replace",newline="") as f:
        next(f,None)
        for line in f:
            p=[x.strip() for x in line.split(",")]
            if len(p)<2 or len(p[0])!=10: continue
            try: v=float(p[1])
            except ValueError: continue
            out[p[0][:7]]=v
    return out


def load_six_portfolios_monthly():
    path=DATA/"6_Portfolios_2x3.csv"
    if not path.exists(): raise FileNotFoundError(f"Missing {path}. Run data/fetch_pinned_data.py first.")
    out={}; in_vw=False
    with path.open("r",encoding="utf-8-sig",errors="replace",newline="") as f:
        for line in f:
            if "Average Value Weighted Returns -- Monthly" in line:
                in_vw=True; continue
            if not in_vw: continue
            if "Average Equal Weighted Returns" in line: break
            p=[x.strip() for x in line.split(",")]
            if not p or len(p[0])!=6 or not p[0].isdigit() or len(p)<7: continue
            try: vals=[float(x)/100.0 for x in p[1:7]]
            except ValueError: continue
            if all(v>-0.90 for v in vals):
                out[f"{p[0][:4]}-{p[0][4:6]}"]=vals
    return out


@dataclass
class Sleeve:
    units: float=0.0
    basis: float=0.0
    cash: float=0.0


class Simulator:
    def __init__(self,cfg,output_dir):
        self.cfg=cfg; self.output_dir=output_dir
        self.rows=load_ff(cfg["start_date"],cfg["end_date"]); self.n=len(self.rows)
        self.price=[0.0]*self.n; self.price[0]=100.0
        for i in range(1,self.n): self.price[i]=self.price[i-1]*(1+self.rows[i]["ret"])

        bm,self.gs10=load_shiller_monthly()
        self.bond=[]; last=math.nan
        for r in self.rows:
            k=previous_month_key(r["date"])
            if k in bm: last=bm[k]
            self.bond.append(last)

        self.aaa=load_fred_monthly("AAA.csv"); self.baa=load_fred_monthly("BAA.csv")
        self.six=load_six_portfolios_monthly()

        self.credit_months=[]
        for k in sorted(self.aaa):
            if k in self.baa: self.credit_months.append((k,self.baa[k]-self.aaa[k]))
        self.credit_index={k:i for i,(k,_) in enumerate(self.credit_months)}

        rf_prod={}
        for r in self.rows:
            k=r["date"][:7]; rf_prod[k]=rf_prod.get(k,1.0)*(1+r["rf"])
        self.rf_month_annualized={k:(v**12-1)*100.0 for k,v in rf_prod.items()}
        self.term_months=[]
        for k in sorted(self.gs10):
            if k in self.rf_month_annualized:
                self.term_months.append((k,self.gs10[k]-self.rf_month_annualized[k]))
        self.term_index={k:i for i,(k,_) in enumerate(self.term_months)}

        self.dd=[];self.dd5=[];self.dd10=[];self.sma150=[];self.bull=[];self.r10=[]
        peak=-math.inf
        for i,p in enumerate(self.price):
            peak=max(peak,p); self.dd.append(p/peak-1)
            self.dd5.append(rolling_mean(self.dd,i,5)); self.dd10.append(rolling_mean(self.dd,i,10))
            s=sma(self.price,i,150); self.sma150.append(s)
            self.bull.append(i>=154 and math.isfinite(s) and p>s and s>self.sma150[i-5])
            self.r10.append(mom(self.price,i,10))

        self.events=[]; self.pac_flows=[]; self.stress_events=[]
        self.fixed_fees=0.0; self.slippage_cost=0.0; self.taxes=0.0
        self.order_counts={"INITIAL":0,"PAC_ORDER":0,"STRATEGY_EQ_BUY":0,"STRATEGY_EQ_SELL":0,"STRATEGY_BOND_BUY":0,"STRATEGY_BOND_SELL":0}

    def credit_signal(self,month):
        ix=self.credit_index.get(month)
        if ix is None: return {"flag":False,"spread":math.nan,"p80":math.nan,"change_3m":math.nan}
        start=max(0,ix-self.cfg["credit_lookback_months"]+1)
        hist=[x[1] for x in self.credit_months[start:ix+1]]
        p80=percentile(hist,self.cfg["credit_percentile"]) if len(hist)>=self.cfg["stress_min_history_months"] else math.nan
        ch3=self.credit_months[ix][1]-self.credit_months[ix-3][1] if ix>=3 else math.nan
        spread=self.credit_months[ix][1]
        flag=(math.isfinite(p80) and spread>=p80) or (math.isfinite(ch3) and ch3>=self.cfg["credit_3m_widening_pp"])
        return {"flag":flag,"spread":spread,"p80":p80,"change_3m":ch3}

    def breadth_signal(self,month):
        healthy=0;valid=0
        for j in range(6):
            prod=1.0;ok=True
            for z in range(self.cfg["breadth_return_months"]):
                vals=self.six.get(add_months(month,-z))
                if vals is None: ok=False;break
                prod*=1+vals[j]
            if ok:
                valid+=1
                if prod-1>0: healthy+=1
        flag=valid==6 and healthy<=self.cfg["breadth_max_healthy"]
        return {"flag":flag,"healthy":healthy,"valid":valid}

    def rate_signal(self,month):
        ix=self.term_index.get(month)
        if ix is None:return {"flag":False,"term":math.nan,"p20":math.nan}
        start=max(0,ix-self.cfg["rates_lookback_months"]+1)
        hist=[x[1] for x in self.term_months[start:ix+1]]
        p20=percentile(hist,self.cfg["rates_percentile"]) if len(hist)>=self.cfg["stress_min_history_months"] else math.nan
        term=self.term_months[ix][1]
        return {"flag":math.isfinite(p20) and term<=p20,"term":term,"p20":p20}

    def stress_regime(self,i):
        month=previous_month_key(self.rows[i]["date"])
        c=self.credit_signal(month); b=self.breadth_signal(month); r=self.rate_signal(month)
        score=int(c["flag"])+int(b["flag"])+int(r["flag"])
        return {"month":month,"score":score,"credit":c["flag"],"breadth":b["flag"],"rates":r["flag"],
                "credit_spread":c["spread"],"credit_p80":c["p80"],"credit_change_3m":c["change_3m"],
                "breadth_healthy":b["healthy"],"term_spread":r["term"],"term_p20":r["p20"]}

    def log_stress(self,i,event,rg,note=""):
        self.stress_events.append({"date":self.rows[i]["date"],"event":event,"market_drawdown":self.dd[i],"note":note,**rg})

    def log_order(self,i,kind,side,asset,gross,fee,slip,tax=0.0,note=""):
        self.events.append({"date":self.rows[i]["date"],"kind":kind,"side":side,"asset":asset,"gross_eur":gross,
                            "fixed_fee_eur":fee,"slippage_eur":slip,"tax_eur":tax,"note":note})
        self.order_counts[kind]+=1; self.fixed_fees+=fee; self.slippage_cost+=slip; self.taxes+=tax

    def buy_eq(self,sl,amount,i,kind,note=""):
        fee=self.cfg["pac_fixed_fee_eur"] if kind=="PAC_ORDER" else self.cfg["strategy_fixed_fee_eur"]
        if amount<=fee:return False
        sr=self.cfg["slippage_each_side"]; gross=(amount-fee)/(1+sr)
        if gross<=0:return False
        slip=gross*sr; sl.units+=gross/self.price[i]; sl.basis+=gross; sl.cash-=gross+fee+slip
        self.log_order(i,kind,"BUY","EQUITY_PROXY",gross,fee,slip,0.0,note); return True

    def sell_eq(self,sl,i,note=""):
        if sl.units<=0:return False
        fee=self.cfg["strategy_fixed_fee_eur"];sr=self.cfg["slippage_each_side"]
        raw=sl.units*self.price[i];slip=raw*sr;gross=raw-slip;gain=gross-fee-sl.basis;tax=max(gain,0)*self.cfg["tax_equity"]
        sl.cash+=gross-fee-tax;sl.units=0;sl.basis=0
        self.log_order(i,"STRATEGY_EQ_SELL","SELL","EQUITY_PROXY",gross,fee,slip,tax,note);return True

    def choose_defensive(self,signal_i):
        m=mom(self.bond,signal_i,126)
        return "BOND" if math.isfinite(m) and m>0 else "CASH"

    def run(self):
        c=self.cfg
        core=Sleeve(cash=c["initial_eur"]*c["core_fraction"]); sat=Sleeve(cash=c["initial_eur"]*c["satellite_fraction"])
        def_units=0.0;def_basis=0.0;def_cash=0.0;stamp=0.0;contributions=c["initial_eur"]
        flows=[(self.rows[0]["date"],-c["initial_eur"])]
        self.buy_eq(core,core.cash,0,"INITIAL","initial core");self.buy_eq(sat,sat.cash,0,"INITIAL","initial satellite")

        sat_in=True;core_in=True;best=self.dd[0];trough=self.dd10[0];exit_count=0;bear=False;second_dd=None
        reset_bull=0;core_zero=False;second_stage=False;p_sat=None;p_core_exit=None;p_core_entry=False
        warning=None;gate_active=False
        prev_month=self.rows[0]["date"][:7];prev_nav=None;tw=1.0;twr=[];nav_rows=[]

        def def_value(i): return def_cash+(def_units*self.bond[i] if def_units>0 and math.isfinite(self.bond[i]) else 0.0)
        def total_nav(i): return core.cash+sat.cash+core.units*self.price[i]+sat.units*self.price[i]+def_value(i)-stamp
        def buy_def(amount,i,asset,note):
            nonlocal def_units,def_basis,def_cash
            if asset=="CASH" or not math.isfinite(self.bond[i]):def_cash+=amount;return
            fee=c["strategy_fixed_fee_eur"]
            if amount<=fee:def_cash+=amount;return
            sr=c["slippage_each_side"];gross=(amount-fee)/(1+sr);slip=gross*sr
            def_units=gross/self.bond[i];def_basis=gross;def_cash+=max(0,amount-gross-fee-slip)
            self.log_order(i,"STRATEGY_BOND_BUY","BUY","SHILLER_TREASURY_PROXY",gross,fee,slip,0.0,note)
        def sell_def(i,note):
            nonlocal def_units,def_basis,def_cash
            if def_units<=0:return
            fee=c["strategy_fixed_fee_eur"];sr=c["slippage_each_side"]
            raw=def_units*self.bond[i];slip=raw*sr;gross=raw-slip;gain=gross-fee-def_basis;tax=max(gain,0)*c["tax_bond"]
            def_cash+=gross-fee-tax;def_units=0;def_basis=0
            self.log_order(i,"STRATEGY_BOND_SELL","SELL","SHILLER_TREASURY_PROXY",gross,fee,slip,tax,note)

        for i in range(self.n):
            external=0.0
            if i>0:
                for sl in (core,sat):
                    if sl.cash>0:
                        g=sl.cash*self.rows[i]["rf"];sl.cash+=g-max(g,0)*c["tax_equity"]
                if def_cash>0:
                    g=def_cash*self.rows[i]["rf"];def_cash+=g-max(g,0)*c["tax_equity"]
                stamp+=max(total_nav(i),0)*c["annual_stamp_proxy"]/252.0

                if p_sat=="EXIT" and sat_in:
                    if self.sell_eq(sat,i,"satellite exit"):
                        sat_in=False;exit_count+=1;trough=self.dd10[i];reset_bull=0
                        if exit_count>=2:
                            bear=True;second_dd=self.dd[i];gate_active=True
                            if core_in:
                                rg=self.stress_regime(i);self.log_stress(i,"SECOND_SATELLITE_EXIT",rg)
                                if rg["score"]>=c["stress_immediate_score"]:
                                    p_core_exit=i;second_stage=True;gate_active=False
                                    self.log_stress(i,"CORE_EXIT_STRESS_2OF3",rg,"immediate")
                                elif rg["score"]==1:
                                    warning={"start":i,"start_dd":self.dd[i]}
                                    self.log_stress(i,"CORE_WARNING_START",rg)
                elif p_sat=="ENTRY" and not sat_in:
                    if sat.cash>c["strategy_fixed_fee_eur"]:self.buy_eq(sat,sat.cash,i,"STRATEGY_EQ_BUY","satellite re-entry")
                    sat_in=True;best=self.dd[i];warning=None;gate_active=False
                p_sat=None

                if p_core_exit is not None and core_in:
                    self.sell_eq(core,i,"core exit")
                    amount=core.cash;core.cash=0;buy_def(amount,i,self.choose_defensive(p_core_exit),"core defensive entry")
                    core_in=False;core_zero=True;p_core_exit=None;warning=None;gate_active=False
                if p_core_entry and not core_in:
                    sell_def(i,"core defensive exit");core.cash+=def_cash;def_cash=0
                    if core.cash>c["strategy_fixed_fee_eur"]:self.buy_eq(core,core.cash,i,"STRATEGY_EQ_BUY","core re-entry")
                    core_in=True;p_core_entry=False;warning=None;gate_active=False

            month=self.rows[i]["date"][:7]
            if month!=prev_month:
                external=c["monthly_pac_eur"];contributions+=external;flows.append((self.rows[i]["date"],-external))
                tn=max(total_nav(i),1e-9);cv=core.cash+core.units*self.price[i]+def_value(i);sv=sat.cash+sat.units*self.price[i]
                core_gap=.5*(tn+external)-cv;sat_gap=.5*(tn+external)-sv
                if core_gap>=sat_gap:
                    if core_in:
                        core.cash+=external;self.buy_eq(core,core.cash,i,"PAC_ORDER","monthly PAC -> core")
                        self.pac_flows.append({"date":self.rows[i]["date"],"amount_eur":external,"target":"CORE","executed_order":1})
                    else:
                        def_cash+=external;self.pac_flows.append({"date":self.rows[i]["date"],"amount_eur":external,"target":"CORE_OUT_CASH","executed_order":0})
                else:
                    sat.cash+=external
                    if sat_in:
                        self.buy_eq(sat,sat.cash,i,"PAC_ORDER","monthly PAC -> satellite")
                        self.pac_flows.append({"date":self.rows[i]["date"],"amount_eur":external,"target":"SATELLITE","executed_order":1})
                    else:self.pac_flows.append({"date":self.rows[i]["date"],"amount_eur":external,"target":"SATELLITE_OUT_CASH","executed_order":0})
                prev_month=month

            active=exit_count>0 or bear or not core_in
            reset_bull=(reset_bull+1 if self.bull[i] else 0) if active else 0
            reset_need=10 if (bear or exit_count>=2) else 5
            if self.dd[i]>=-1e-12 or (active and reset_bull>=reset_need):
                exit_count=0;bear=False;second_dd=None;reset_bull=0;core_zero=False;second_stage=False;warning=None;gate_active=False
                if not core_in:p_core_entry=True

            force=math.isfinite(self.r10[i]) and self.r10[i]<=-0.10
            if sat_in:
                best=max(best,self.dd[i]);threshold=0.075 if (exit_count==0 and self.bull[i]) else 0.05
                if self.dd[i]<=best-threshold or force:p_sat="EXIT"
            else:
                trough=min(trough,self.dd10[i])
                if exit_count<=1:
                    if self.dd10[i]>=trough+c["satellite_rebound"] and self.dd5[i]>self.dd10[i]:p_sat="ENTRY"
                elif self.bull[i]:p_sat="ENTRY"

            if core_in and gate_active:
                rg=self.stress_regime(i)
                if warning is not None:
                    age=i-warning["start"]
                    deteriorated=self.dd[i]<=warning["start_dd"]-c["warning_extra_drawdown"]
                    crash=math.isfinite(self.r10[i]) and self.r10[i]<=-0.10
                    if rg["score"]>=c["stress_immediate_score"]:
                        p_core_exit=i;second_stage=True;warning=None;gate_active=False
                        self.log_stress(i,"CORE_EXIT_WARNING_STRESS",rg)
                    elif deteriorated or crash:
                        p_core_exit=i;second_stage=True;warning=None;gate_active=False
                        self.log_stress(i,"CORE_EXIT_WARNING_PRICE",rg,"3pp deterioration or Fast10")
                    elif age>=c["warning_sessions"]:
                        if not sat_in:
                            p_core_exit=i;second_stage=True;warning=None;gate_active=False
                            self.log_stress(i,"CORE_EXIT_WARNING_TIMEOUT",rg)
                        else:
                            warning=None;gate_active=False;self.log_stress(i,"CORE_WARNING_CANCEL",rg,"satellite already in")
                else:
                    if rg["score"]>=c["stress_immediate_score"]:
                        p_core_exit=i;second_stage=True;gate_active=False
                        self.log_stress(i,"CORE_EXIT_LATE_STRESS",rg)
                    elif rg["score"]==1:
                        warning={"start":i,"start_dd":self.dd[i]};self.log_stress(i,"CORE_WARNING_LATE_START",rg)

            if bear and not core_zero and second_dd is not None and core_in and self.dd[i]<=second_dd-0.05:
                p_core_exit=i;warning=None;gate_active=False;second_stage=True
                self.log_stress(i,"CORE_EXIT_LEGACY_5PP",self.stress_regime(i),"legacy emergency override")
            if not core_in and self.bull[i]:p_core_entry=True

            nv=total_nav(i);daily_ret=0.0 if prev_nav is None else (nv-external)/prev_nav-1.0
            tw*=1+daily_ret;twr.append(tw);prev_nav=nv
            rg=self.stress_regime(i)
            nav_rows.append({"date":self.rows[i]["date"],"nav_eur":nv,"twr_index":tw,"market_proxy":self.price[i],"drawdown_twr":0.0,
                             "core_in":int(core_in),"satellite_in":int(sat_in),"stress_score":rg["score"],"gate_active":int(gate_active),"warning_active":int(warning is not None),
                             "core_value_eur":core.cash+core.units*self.price[i]+def_value(i),"satellite_value_eur":sat.cash+sat.units*self.price[i]})

        pk=-math.inf
        for r in nav_rows:
            pk=max(pk,r["twr_index"]);r["drawdown_twr"]=r["twr_index"]/pk-1.0

        last=self.n-1
        def liq_eq(sl):
            v=sl.cash
            if sl.units>0:
                fee=c["strategy_fixed_fee_eur"];raw=sl.units*self.price[last];slip=raw*c["slippage_each_side"];gross=raw-slip-fee;gain=gross-sl.basis
                v+=gross-max(gain,0)*c["tax_equity"]
            return v
        final=liq_eq(core)+liq_eq(sat)+def_cash-stamp
        if def_units>0:
            fee=c["strategy_fixed_fee_eur"];raw=def_units*self.bond[last];slip=raw*c["slippage_each_side"];gross=raw-slip-fee;gain=gross-def_basis
            final+=gross-max(gain,0)*c["tax_bond"]
        flows.append((self.rows[last]["date"],final));y=years_between(self.rows[0]["date"],self.rows[last]["date"])
        result={
            "strategy":c["strategy_name"],"start_date":self.rows[0]["date"],"end_date":self.rows[-1]["date"],"initial_eur":c["initial_eur"],
            "monthly_pac_eur":c["monthly_pac_eur"],"total_contributions_eur":contributions,"twr_cagr_net":tw**(1/y)-1,
            "max_drawdown":max_dd(twr),"xirr":xirr(flows),"final_liquidated_eur":final,"orders_total":sum(self.order_counts.values()),
            "orders_pac":self.order_counts["PAC_ORDER"],"orders_strategy":sum(v for k,v in self.order_counts.items() if k not in ("PAC_ORDER","INITIAL")),
            "orders_initial":self.order_counts["INITIAL"],"order_breakdown":self.order_counts,"fixed_fees_eur":self.fixed_fees,
            "slippage_proxy_eur":self.slippage_cost,"realized_taxes_eur":self.taxes,"stamp_proxy_eur":stamp,"stress_events":len(self.stress_events),
            "pac_months_total":len(self.pac_flows),"pac_months_with_immediate_order":sum(x["executed_order"] for x in self.pac_flows),
            "pac_months_held_as_cash_or_defensive":sum(1-x["executed_order"] for x in self.pac_flows),
        }
        return result,nav_rows,flows


def write_csv(path,rows,fieldnames=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not rows:path.write_text("",encoding="utf-8");return
    if fieldnames is None:fieldnames=list(rows[0].keys())
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fieldnames);w.writeheader();w.writerows(rows)


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--config",default=str(DEFAULT_CONFIG));ap.add_argument("--output",default=str(ROOT/"output"));args=ap.parse_args()
    cfg=json.loads(Path(args.config).read_text(encoding="utf-8"));out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    sim=Simulator(cfg,out);result,nav_rows,flows=sim.run()
    (out/"summary.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    write_csv(out/"nav_daily.csv",nav_rows);write_csv(out/"orders.csv",sim.events);write_csv(out/"stress_events.csv",sim.stress_events)
    write_csv(out/"pac_flows.csv",sim.pac_flows);write_csv(out/"cashflows_xirr.csv",[{"date":d,"cashflow_eur":v} for d,v in flows])
    write_csv(out/"summary.csv",[{k:v for k,v in result.items() if not isinstance(v,dict)}])
    print("\nATLAS V2.3-PAC Stress Filter Frozen")
    print(f"Period: {result['start_date']} -> {result['end_date']}")
    print(f"TWR CAGR net: {result['twr_cagr_net']*100:.4f}%")
    print(f"MaxDD:       {result['max_drawdown']*100:.4f}%")
    print(f"XIRR:        {result['xirr']*100:.4f}%")
    print(f"Final:       EUR {result['final_liquidated_eur']:,.2f}")
    print(f"Orders:      {result['orders_total']} total = {result['orders_pac']} PAC + {result['orders_strategy']} strategy + {result['orders_initial']} initial")
    print(f"Stress log:  {result['stress_events']} events")
    print(f"Output:      {out}")


if __name__=="__main__":
    main()