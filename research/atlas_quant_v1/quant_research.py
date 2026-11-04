#!/usr/bin/env python3
"""Atlas Quant Research v1.0: price-only PIT-style global Top200 experiment.

Standalone/offline. NEVER modifies live ledgers or workflows. Source data frozen from
H4 GLOBAL DATASET (2012-2026 annual rosters, downloaded OHLC) and Atlas FX/proxy.
Does not falsely label reconstructed history certified PIT. USD/EUR shares ONLY.
All trading instructions are created from information prior to the execution date.
"""
from __future__ import annotations
import argparse,csv,copy,datetime as dt,gzip,hashlib,io,json,math,re,sys,zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parent
LIVE_ZIP=(ROOT/'data'/'Top200_USD_EUR_Frozen_Prices.zip' if (ROOT/'data'/'Top200_USD_EUR_Frozen_Prices.zip').exists() else Path('/mnt/data/files/H4_GLOBAL_DATASET_FROZEN_2026-09-12.zip'))
ARCHIVED_FX=(ROOT/'data' if (ROOT/'data'/'dexuseu_usd_per_eur.csv').exists() else Path('/mnt/data/atlas_quant_dev/Atlas_Top10_Backtest_v08/historical_source/data'))
CAPITAL=10000.0
START=pd.Timestamp('2016-02-01'); END=pd.Timestamp('2025-12-10')
COMMISSION=1.; SLIPPAGE=.001; BOLLO=.002; GAIN_TAX=.26; DIV_TAX=.26
SELECTION_MONTHS={2,5,8,11}
MODES=['Top10_Eligible_BH','Top10_Eligible_MA6_12','Quant20_Momentum','Quant20_TrendVol']


def source_sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for piece in iter(lambda:f.read(1024*1024),b''):h.update(piece)
    return h.hexdigest()

def load_frozen(h4zip=LIVE_ZIP, fx_folder=ARCHIVED_FX):
    """Read archival prices in original currency, respecting per-security exchange dates.
    YF raw OHLC has been retrospectively split-adjusted; dividend events inferred
    from changing adj_close/close factor, not independently verified pay dates.
    """
    z=zipfile.ZipFile(h4zip)
    inner=(zipfile.ZipFile(io.BytesIO(z.read('sources/UPLOAD_TO_CHATGPT_GLOBAL_DATASET.zip')))
           if 'sources/UPLOAD_TO_CHATGPT_GLOBAL_DATASET.zip' in z.namelist() else z)
    roster=pd.read_csv(io.BytesIO(inner.read('global_top200_annual_pit.csv')))
    roster=roster.dropna(subset=['issuer_key']).copy()
    roster['issuer_key']=roster.issuer_key.astype(str)
    coverage=pd.read_csv(io.BytesIO(inner.read('price_coverage_summary.csv')))
    coverage=coverage.drop_duplicates('issuer_key').set_index('issuer_key')
    roster['currency']=roster.issuer_key.map(coverage.currency)
    filt=roster[roster.effective_year.between(2016,2025)&roster.currency.isin(['USD','EUR'])]
    keys=sorted(filt.issuer_key.unique())
    fx=pd.read_csv(fx_folder/'dexuseu_usd_per_eur.csv',parse_dates=['observation_date']).set_index('observation_date').DEXUSEU.sort_index().astype(float)
    fx=fx[fx.gt(0)].reindex(pd.date_range('2014-01-01','2025-12-31')).ffill(limit=7)
    raw_bm=pd.read_csv(fx_folder/'global_benchmark_proxy.csv',parse_dates=['date']).set_index('date').global_proxy.sort_index()
    market=raw_bm.reindex(fx.index)/fx
    market=market.dropna();market.index.name='date'
    records={};errors=[]
    for key in keys:
        fname='prices_by_issuer/'+re.sub('[^a-z0-9]+','_',key).strip('_')+'.csv.gz'
        if fname not in inner.namelist():errors.append(key+': MISSING_SOURCE');continue
        try:
            d=pd.read_csv(io.BytesIO(inner.read(fname)),compression='gzip',parse_dates=['date']).set_index('date').sort_index()
            d=d.loc['2014-01-01':str(END.date()),['open','close','adj_close','volume','currency']].copy()
            if d.empty:errors.append(key+': EMPTY');continue
            d=d[~d.index.duplicated(keep='last')]
            currency=str(d.currency.dropna().iloc[-1])
            if currency not in ('USD','EUR') or currency!=coverage.loc[key,'currency']:errors.append(key+': CURRENCY_MISMATCH');continue
            d['currency']=currency
            conv=1.0 if currency=='EUR' else (1/fx.reindex(d.index).ffill(limit=7))
            open_conv=1.0 if currency=='EUR' else (1/fx.shift(1).reindex(d.index).ffill(limit=7))
            d['open_eur']=d.open.astype(float)*open_conv
            d['close_eur']=d.close.astype(float)*conv
            d['adj_eur']=d.adj_close.astype(float)*conv
            d['volume_eur']=d.volume.astype(float)*d.close_eur
            if len(d.dropna(subset=['open_eur','close_eur','adj_eur']))<400:errors.append(key+': SHORT_HISTORY');continue
            # Approximate dividend events from YF's per-day dividend adjustment ratio.
            ratio=(d.adj_close/d.close).replace([np.inf,-np.inf],np.nan)
            factor=ratio/ratio.shift(1)
            div_local=d.close.shift(1)*(1-1/factor)
            div_local=div_local.where((factor>1.00015)&(factor<1.5)&(div_local>0),0.)
            d['div_eur_est']=div_local*conv
            records[key]=d
        except Exception as e:errors.append(key+': '+str(e)[:150])
    # O(1) daily quote grid for accounting; no stale mark older than 8 calendar days.
    grid_idx=pd.date_range('2014-01-01',str(END.date()),freq='D')
    close_grid=pd.DataFrame({k:d.close_eur for k,d in records.items()}).reindex(grid_idx).ffill(limit=8)
    open_grid=pd.DataFrame({k:d.open_eur for k,d in records.items()}).reindex(grid_idx)
    div_grid=pd.DataFrame({k:d.div_eur_est for k,d in records.items()}).reindex(grid_idx).fillna(0.)
    return dict(roster=roster,assets=records,fx=fx,market=market,errors=errors,coverage=coverage,
                close_grid=close_grid,open_grid=open_grid,div_grid=div_grid,factor_cache={},
                raw_source_sha256=source_sha256(h4zip),fx_source_sha256=source_sha256(fx_folder/'dexuseu_usd_per_eur.csv'))


def eligible_universe(source,asof,year):
    r=source['roster']
    candidates=r[(r.effective_year==year)&r.currency.isin(['USD','EUR'])].sort_values('effective_rank')
    out=[];reject=defaultdict(int)
    for _,item in candidates.iterrows():
        k=item.issuer_key
        if k not in source['assets']:reject['price_archive_missing']+=1;continue
        p=source['assets'][k].loc[:asof]
        past=p[p.index<=asof].dropna(subset=['adj_eur','close_eur','open_eur'])
        if len(past)<253:reject['insufficient_12m']+=1;continue
        if (asof-past.index[-1]).days>7:reject['stale_at_decision']+=1;continue
        if not past.iloc[-1]['close_eur']>0:reject['bad_price']+=1;continue
        vol_eur=float(past.volume_eur.tail(40).median())
        if pd.isna(vol_eur) or vol_eur<1e6:reject['below_1m_median_volume']+=1;continue
        out.append((k,item, past))
    return out,dict(reject)


def generate_factors(source,asof,year):
    key=(asof.year,asof.month,year)
    if key in source.get('factor_cache',{}):return source['factor_cache'][key]
    elig,reject=eligible_universe(source,asof,year)
    rec=[]
    for k,item,d in elig:
        adj=d.adj_eur.astype(float).dropna().tail(253)
        if len(adj)<253:continue
        ret=adj.pct_change(fill_method=None).dropna()
        if len(ret)<252:continue
        mom12_1=float(adj.iloc[-22]/adj.iloc[0]-1)
        mom6_1=float(adj.iloc[-22]/adj.iloc[-127]-1)
        vol126=float(ret.tail(126).std(ddof=1)*np.sqrt(252))
        down126=float(np.sqrt((ret.tail(126).clip(upper=0)**2).mean())*np.sqrt(252))
        if not all(np.isfinite([mom12_1,mom6_1,vol126,down126])):continue
        sector=str(item.marketcap_text) if pd.notna(item.marketcap_text) else 'Unknown'
        rec.append(dict(issuer=k,effective_rank=int(item.effective_rank),sector=sector,
            mom12_1=mom12_1,mom6_1=mom6_1,vol126=vol126,down126=down126,median_volume_eur=float(d.volume_eur.tail(40).median())))
    df=pd.DataFrame(rec)
    if len(df)<30:raise RuntimeError('Insufficient selection universe: '+str((asof,year,len(df),reject)))
    for field,sign in [('mom12_1',1),('mom6_1',1),('vol126',-1),('down126',-1)]:
        df[field+'_rank']=df[field].rank(pct=True,ascending=sign>0)
    # Note: ascending=sign>0 means high value -> high rank for momentum,
    # low value -> high rank for volatility and downside.
    df['score']=(.45*df.mom12_1_rank + .25*df.mom6_1_rank+
                 .20*df.vol126_rank+.10*df.down126_rank)
    df=df.sort_values(['score','issuer'],ascending=[False,True]).reset_index(drop=True)
    source.setdefault('factor_cache',{})[key]=(df,reject)
    return df,reject


def select_names(factors,n=20,limit_sector=5):
    out=[];sec=defaultdict(int)
    for _,r in factors.iterrows():
        if sec[r.sector]>=limit_sector:continue
        out.append(r.issuer);sec[r.sector]+=1
        if len(out)>=n:break
    return out


def bench_regime(market,asof,prev):
    monthly=market.loc[:asof].resample('ME').last().dropna()
    if len(monthly)<12:raise ValueError('Insufficient global proxy MA history')
    slow=float(monthly.iloc[-12:].mean());fast=float(monthly.iloc[-6:].mean())
    risk=prev
    if fast>slow*1.01:risk=True
    elif fast<slow*.99:risk=False
    vol=float(market.loc[:asof].pct_change(fill_method=None).dropna().tail(60).std()*np.sqrt(252))
    if not np.isfinite(vol):raise ValueError('Missing 60d vol of benchmark')
    return risk,vol,fast,slow


def project_weights(proposed,exposure,sectors,name_cap=.09,sector_cap=.30):
    """Deterministic capped water-fill projection, finite iterations, no external optimizer.

    Sector limits expressed as fraction of total equity sleeve, not the cash budget.
    It is not an exact Euclidean projection; explicit feasibility is checked.
    """
    n=len(proposed)
    if not n:return np.zeros(0)
    names=np.asarray(sectors,dtype=str);groups={k:np.flatnonzero(names==k) for k in sorted(set(names))}
    cap=name_cap*exposure;scap=sector_cap*exposure
    if sum(min(len(ii)*cap,scap) for ii in groups.values())+1e-8<exposure:
        raise ValueError('Infeasible sector/name diversification constraints')
    x=np.clip(np.asarray(proposed,dtype=float),0,None)
    if x.sum()<1e-14:x=np.ones(n)
    x=x/x.sum()*exposure
    for it in range(250):
        x=np.minimum(np.maximum(x,0),cap)
        for ii in groups.values():
            sm=x[ii].sum()
            if sm>scap:x[ii]*=scap/sm
        missing=exposure-x.sum()
        if missing<1e-8:break
        room=np.maximum(cap-x,0)
        for ii in groups.values():
            rem=max(0.,scap-x[ii].sum())
            ss=room[ii].sum()
            if ss>rem and ss>0:room[ii]*=rem/ss
        total=room.sum()
        if total<1e-11:raise ValueError('Cannot fill risk budget under constraints')
        x+=room*(min(1.,missing/total))
    if abs(x.sum()-exposure)>5e-6:raise ValueError('Projection failed to conserve exposure')
    if np.any(x>cap+1e-8) or any(x[ii].sum()>scap+1e-8 for ii in groups.values()):
        raise AssertionError('Projection violated diversification constraints')
    return x


class TaxLedger:
    def __init__(self):self.loss=[]
    def apply(self,profit,year):
        self.loss=[x for x in self.loss if x['expires']>=year and x['amount']>1e-8]
        if profit<0:
            self.loss.append(dict(expires=year+4,amount=-profit));return 0.
        taxable=float(profit)
        for item in sorted(self.loss,key=lambda x:x['expires']):
            use=min(taxable,item['amount']);taxable-=use;item['amount']-=use
        self.loss=[x for x in self.loss if x['amount']>1e-8]
        return GAIN_TAX*taxable

@dataclass
class Position:
    shares:float
    basis:float

class Account:
    def __init__(self,mode,source):
        self.mode=mode;self.source=source;self.cash=CAPITAL;self.positions={};self.loss=TaxLedger()
        self.orders=[];self.nav=[];self.signals=[];self.factor_snapshots=[];self.errors=[]
        self.pending={};self.selection=[];self.target_exposure=0.;self.risk_on=False
        self.bollo_liability=0.;self.dividend_taxes=0.;self.fees=0.;self.slippage=0.;self.gain_taxes=0.
        self.peak=CAPITAL;self.max_top3=0.;self.max_name=0.;self.last_decision=None
    def old_price(self,k,t):
        return float(self.source['close_grid'].at[t,k])
    def equity(self,t):
        total=0.
        for k,pos in self.positions.items():
            p=self.old_price(k,t)
            if not np.isfinite(p):raise ValueError(f'Unavailable valuation {k} on {str(t)[:10]}')
            total+=p*pos.shares
        return total
    def target_task(self,k,side,value,reason,decision):
        if value>5.:self.pending[k]=dict(key=k,side=side,budget_eur=float(value),reason=reason,decision=decision)
    def decision(self,t,asof,roster_year,firstmonth):
        # Selection and regime always use information available at PRIOR MONTH END.
        prevsel=list(self.selection)
        prev_expo=self.target_exposure
        risk,vol,fast,slow=bench_regime(self.source['market'],asof,self.risk_on)
        self.risk_on=risk
        quarterly=t.month in SELECTION_MONTHS
        annual=t.month==2
        if self.mode.startswith('Top10'):
            if annual or not self.selection:
                f,_=generate_factors(self.source,asof,roster_year)
                self.selection=list(f.sort_values('effective_rank').issuer[:10]);self.factor_snapshots.append(f.assign(date=str(t.date()),strategy=self.mode).head(10))
            expo=1. if self.mode.endswith('_BH') else float(risk)
            weights=None
        else:
            if quarterly or not self.selection:
                f,reject=generate_factors(self.source,asof,roster_year)
                self.selection=select_names(f,20,5)
                self.factor_snapshots.append(f.assign(date=str(t.date()),strategy=self.mode).head(60))
            else:f=(generate_factors(self.source,asof,roster_year)[0] if self.mode=='Quant20_TrendVol' and not self.positions else None)
            expo=1.
            if self.mode=='Quant20_TrendVol':expo=min(1.,max(.5,.14/max(vol,1e-6))) if risk else 0.
            weights=None
            if self.mode=='Quant20_TrendVol' and expo>0:
                oldnav=self.cash+self.equity(asof)-self.bollo_liability
                # Reselection quarterly; otherwise just scale existing portfolio
                # to the new risk budget, without tax-creating equal rebalancing.
                if quarterly or not self.positions:
                    if f is None:f,_=generate_factors(self.source,asof,roster_year)
                    fi=f.set_index('issuer').reindex(self.selection)
                    raw=(1/fi.vol126.clip(lower=.05).to_numpy())*np.exp(fi.score.to_numpy()*.4)
                    sectors=fi.sector.tolist()
                    w=project_weights(raw,expo,sectors)
                    weights=dict(zip(self.selection,w))
                else:
                    values={k:self.old_price(k,asof)*v.shares for k,v in self.positions.items() if k in self.selection}
                    invested=sum(values.values())
                    if invested>0:weights={k:(values.get(k,0.)/invested)*expo for k in self.selection}
                    else:weights={k:expo/len(self.selection) for k in self.selection}
        self.target_exposure=expo
        active=self.selection if expo>0 else []
        changed=(set(active)!=set(self.positions) or abs(expo-prev_expo)>1e-5)
        if self.mode=='Quant20_TrendVol':changed=True # monthly risk budget; quarter sector-capped inverse-vol weights
        if self.mode=='Quant20_Momentum' and not quarterly and prevsel==self.selection:changed=False
        if self.mode.startswith('Top10') and not annual and abs(expo-prev_expo)<1e-5:changed=False
        self.signals.append(dict(date=str(t.date()),decision_asof=str(asof.date()),roster_year=roster_year,
           mode=self.mode,regime_risk_on=risk,market_fast=fast,market_slow=slow,market_vol60=vol,
           target_exposure=expo,selected=';'.join(active),selected_count=len(active),changed=changed))
        if not changed:return
        if self.pending:
            self.errors.append(dict(date=str(t.date()),error='CANCELED_PENDING_ON_NEW_SIGNAL',count=len(self.pending)))
            self.pending={}
        # Decision sizing from preceding closing prices, no same-day OPEN peeking.
        prices={k:self.old_price(k,asof) for k in self.positions}
        capital=self.cash+sum(prices[k]*p.shares for k,p in self.positions.items())-self.bollo_liability
        if not np.isfinite(capital) or capital<=0:raise RuntimeError('Invalid NAV at decision')
        target=set(active)
        exiting=[k for k in self.positions if k not in target]
        for k in exiting:self.target_task(k,'SELL',prices[k]*self.positions[k].shares,'membership_or_cash_exit',str(asof.date()))
        new=[k for k in active if k not in self.positions]
        if weights is None:
            # Never rebalance winners if risk state/membership does not change.
            retained_value=sum(prices[k]*p.shares for k,p in self.positions.items() if k in target)
            available_budget=max(0.,capital*expo-retained_value)
            for k in new:self.target_task(k,'BUY',available_budget/max(len(new),1)*.996,'new_member',str(asof.date()))
        else:
            # At monthly optimizer rebalance only above 1.5% NAV changes.
            for k in active:
                ideal=capital*weights[k];actual=prices[k]*self.positions[k].shares if k in self.positions else 0.
                delta=ideal-actual
                if abs(delta)/capital<.015:continue
                self.target_task(k,'BUY' if delta>0 else 'SELL',abs(delta)*(.996 if delta>0 else 1.),'risk_optimization',str(asof.date()))
    def sell(self,t,k,budget,reason,decision):
        px=self.source['open_grid'].at[t,k]
        if not np.isfinite(px):return False
        pos=self.positions.get(k)
        if not pos:return True
        px=float(px);qty=pos.shares if reason=='membership_or_cash_exit' else min(pos.shares,budget/max(px,1e-9))
        if qty*px<COMMISSION+5:return True
        gross=qty*px;sl=SLIPPAGE*gross;net=gross-sl-COMMISSION
        basis=pos.basis*(qty/pos.shares);gain=net-basis;tax=self.loss.apply(gain,t.year)
        self.cash+=net-tax;pos.shares-=qty;pos.basis-=basis
        if pos.shares<1e-8:del self.positions[k]
        self.fees+=COMMISSION;self.slippage+=sl;self.gain_taxes+=tax
        self.orders.append(dict(date=str(t.date()),mode=self.mode,side='SELL',issuer=k,decision_asof=decision,
          open_eur=px,shares=qty,gross_eur=gross,fee_eur=COMMISSION,slippage_eur=sl,
          realized_gain_eur=gain,tax_eur=tax,cash_after=self.cash,reason=reason))
        return True
    def buy(self,t,k,budget,reason,decision):
        px=self.source['open_grid'].at[t,k]
        if not np.isfinite(px):return False
        budget=min(budget,max(0.,self.cash))
        if budget<15:return False
        px=float(px);gross=(budget-COMMISSION)/(1+SLIPPAGE)
        qty=gross/px
        if qty<1e-8:return True
        spent=gross*(1+SLIPPAGE)+COMMISSION
        if spent>self.cash+1e-4:raise ValueError('Overdraft')
        pos=self.positions.setdefault(k,Position(0.,0.));pos.shares+=qty;pos.basis+=spent;self.cash-=spent
        self.fees+=COMMISSION;self.slippage+=gross*SLIPPAGE
        self.orders.append(dict(date=str(t.date()),mode=self.mode,side='BUY',issuer=k,decision_asof=decision,
          open_eur=px,shares=qty,gross_eur=gross,fee_eur=COMMISSION,slippage_eur=gross*SLIPPAGE,
          realized_gain_eur=0.,tax_eur=0.,cash_after=self.cash,reason=reason))
        return True
    def step(self,t,prior,monthly_decision=False,asof=None,roster_year=None):
        # Ex-date entitlement arises from holdings before today's opening.
        for k,pos in list(self.positions.items()):
            dividend=float(self.source['div_grid'].at[t,k])
            if dividend>0:
                gross=dividend*pos.shares;tax=DIV_TAX*gross;self.cash+=gross-tax;self.dividend_taxes+=tax
        if prior is not None:
            self.bollo_liability+=self.equity(prior)*BOLLO*(t-prior).days/365.25
        if monthly_decision:self.decision(t,asof,roster_year,firstmonth=True)
        # Execute delayed selling and buying separately on each security's OPEN.
        for k,task in list(self.pending.items()):
            if task['side']=='SELL' and self.sell(t,k,task['budget_eur'],task['reason'],task['decision']):del self.pending[k]
        for k,task in list(self.pending.items()):
            if task['side']=='BUY' and self.buy(t,k,task['budget_eur'],task['reason'],task['decision']):del self.pending[k]
        real=self.cash+self.equity(t)-self.bollo_liability
        hypo=self.hypo_liquidated(t)
        if not np.isfinite(hypo):raise RuntimeError('Invalid liquidated NAV')
        holdings=sorted([p.shares*self.old_price(k,t) for k,p in self.positions.items()],reverse=True)
        invested=sum(holdings);total=max(invested+self.cash,1e-8)
        top3=sum(holdings[:3])/total;top1=(holdings[0]/total if holdings else 0)
        self.max_top3=max(self.max_top3,top3);self.max_name=max(self.max_name,top1)
        self.nav.append(dict(date=str(t.date()),mode=self.mode,nav_eur=real,nav_liquidatable_eur=hypo,
            cash_eur=self.cash,invested_eur=invested,positions=len(self.positions),
            tax_paid_eur=self.gain_taxes,bollo_liability_eur=self.bollo_liability,
            dividend_taxes_eur=self.dividend_taxes,orders=len(self.orders),risk_on=self.risk_on,
            target_exposure=self.target_exposure,top1_weight=top1,top3_weight=top3,
            pending_count=len(self.pending)))
    def hypo_liquidated(self,t):
        ledger=copy.deepcopy(self.loss);total=self.cash-self.bollo_liability
        for k,pos in self.positions.items():
            px=self.old_price(k,t);gross=px*pos.shares
            net=max(0.,gross*(1-SLIPPAGE)-COMMISSION)
            total+=net-ledger.apply(net-pos.basis,t.year)
        return total


def benchmark_report(market,dates):
    # Synthetic proxy global 60/30/10 EUR, total-return index;
    # only realized gain taxed on hypothetical full ETF liquidation.
    first=dates[0];p0=float(market.asof(first));q=(CAPITAL-COMMISSION)/(p0*(1+SLIPPAGE))
    points=[];bollo=0.
    for i,t in enumerate(dates):
        p=float(market.asof(t));val=p*q
        if i>0:bollo+=(t-dates[i-1]).days/365.25*val*BOLLO
        gross=val*(1-SLIPPAGE)-COMMISSION
        hypo=gross-bollo-max(0.,gross-CAPITAL)*GAIN_TAX
        points.append((t,hypo))
    return pd.Series(dict(points),name='Global_proxy_60_30_10')


def run(source,mode,dates):
    acc=Account(mode,source)
    cal=list(dates)
    prev=None;decisions=[];lastmonth=None
    for t in cal:
        thismonth=(t.year,t.month)
        decision=thismonth!=lastmonth
        if decision:
            year=t.year if t.month>=2 else t.year-1
            asof=t.to_period('M').start_time-pd.Timedelta(days=1)
        acc.step(t,prev,monthly_decision=decision,asof=asof if decision else None,roster_year=year if decision else None)
        prev=t;lastmonth=thismonth
    return acc

def metrics(nav):
    s=nav.set_index(pd.to_datetime(nav.date)).nav_liquidatable_eur.astype(float)
    years=(s.index[-1]-s.index[0]).days/365.25
    cagr=(s.iloc[-1]/CAPITAL)**(1/years)-1
    dd=s/s.cummax()-1
    r=s.pct_change(fill_method=None).dropna()
    sharpe=r.mean()/r.std(ddof=1)*np.sqrt(252) if r.std(ddof=1)>0 else np.nan
    return dict(cagr_pct=100*cagr,maxdd_pct=100*dd.min(),sharpe=sharpe,final_nav_eur=s.iloc[-1],
       total_return_pct=(s.iloc[-1]/CAPITAL-1)*100,volatility_pct=100*r.std(ddof=1)*np.sqrt(252))


def research(h4zip=LIVE_ZIP,fxdir=ARCHIVED_FX,out=ROOT/'reports'):
    out.mkdir(parents=True,exist_ok=True)
    source=load_frozen(h4zip,fxdir)
    # UNION of EUR/USD exchange sessions, restricted to the valid benchmark period.
    dates=pd.date_range(START,END)
    actual=set(source['market'].loc[START:END].index)
    for p in source['assets'].values():actual.update(p.loc[START:END].index)
    dates=pd.DatetimeIndex(sorted(t for t in actual if START<=t<=END and t.weekday()<5))
    result=[]; accounts={}
    for mode in MODES:
        a=run(source,mode,dates);accounts[mode]=a
        n=pd.DataFrame(a.nav); pd.DataFrame(a.orders).to_csv(out/(mode+'_orders.csv'),index=False)
        n.to_csv(out/(mode+'_nav.csv'),index=False)
        pd.DataFrame(a.signals).to_csv(out/(mode+'_signals.csv'),index=False)
        pd.concat(a.factor_snapshots,ignore_index=True).to_csv(out/(mode+'_factor_audit.csv'),index=False)
        result.append(dict(strategy=mode,**metrics(n),orders=len(a.orders),buys=sum(z['side']=='BUY' for z in a.orders),
            sells=sum(z['side']=='SELL' for z in a.orders),gain_tax_paid_eur=a.gain_taxes,dividend_tax_paid_eur=a.dividend_taxes,
            bollo_liability_eur=a.bollo_liability,fees_eur=a.fees,slippage_eur=a.slippage,
            max_top1_pct=100*a.max_name,max_top3_pct=100*a.max_top3,signals=len(a.signals),execution_warnings=len(a.errors)))
        if a.errors:pd.DataFrame(a.errors).to_csv(out/(mode+'_warnings.csv'),index=False)
    benchmark=benchmark_report(source['market'],dates)
    b=pd.DataFrame([{'date':str(t.date()),'nav_liquidatable_eur':v} for t,v in benchmark.items()]);b.to_csv(out/'benchmark_nav.csv',index=False)
    result.append(dict(strategy='Global_proxy_60_30_10',**metrics(b),orders=1,buys=1,sells=0,
                       gain_tax_paid_eur=0.,dividend_tax_paid_eur=0.,bollo_liability_eur=np.nan,
                       fees_eur=1.,slippage_eur=10.,max_top1_pct=np.nan,max_top3_pct=np.nan,signals=0,execution_warnings=0))
    pd.DataFrame(result).to_csv(out/'comparison.csv',index=False)
    samples={mode:pd.DataFrame(accounts[mode].nav).set_index('date').nav_liquidatable_eur for mode in MODES}
    pd.DataFrame([dict(date=str(t.date()),**{mode:float(samples[mode].loc[str(t.date())]) for mode in MODES},benchmark=float(benchmark.loc[t])) for t in dates[::21]]).to_csv(out/'monthly_sample_nav.csv',index=False)
    metadata=dict(start=str(START.date()),end=str(END.date()),currency_scope=['USD','EUR'],
         input_issuers=len(source['assets']),selected_roster_issuers=source['roster'].issuer_key.nunique(),
         effective_years=[2016,2025],simulated_sessions=len(dates),failed_price_inputs=source['errors'][:30],
         source_sha256=source['raw_source_sha256'],fx_sha256=source['fx_source_sha256'],
         code_model='annual reconstructed ranking; cross-sectional 12-1 and 6-1 momentum with low-vol and downside; actual archived OHLC OPEN fills',
         no_trade_in_live=True, pit_ranking_certified=False, fundamentals_quality_value_available=False)
    (out/'manifest.json').write_text(json.dumps(metadata,indent=2,ensure_ascii=False),encoding='utf8')
    return pd.DataFrame(result),accounts,metadata

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--source-zip',type=Path,default=LIVE_ZIP);ap.add_argument('--fx-dir',type=Path,default=ARCHIVED_FX)
    ap.add_argument('--output',type=Path,default=ROOT/'reports');args=ap.parse_args()
    df,accounts,meta=research(args.source_zip,args.fx_dir,args.output)
    print(df.to_string(index=False,float_format=lambda x:f'{x:.3f}'))
    print('Manifest:',meta)