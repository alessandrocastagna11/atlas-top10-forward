#!/usr/bin/env python3
"""Out-of-sample historical US-only Atlas MOM10 ADAPTER, not frozen global parity.
Uses dated S&P 500 constituents and real daily prices. No future membership,
no survivor-only filter. Market-only residual when historical sectors unavailable.
Liquidity-ranked Top100 proxy, USD accounting (no historical EUR), adjusted
total-return units, no separate dividends, original FIFO execution and tax code.
No real orders. Never writes a live branch.
"""
import gc, hashlib, json, math, sys
from pathlib import Path
from dataclasses import replace
from types import MethodType
import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'strict_shadow'/'v4_exact_source'
for p in [SRC,SRC/'code',SRC/'code'/'engine']:sys.path.insert(0,str(p))
import equity_long_backtest as v1
import equity_long_backtest_v2 as v2
import equity_long_bear_tax_comparison as t9
import equity_long_v4_research as v4
import incumbent_engine as inc
import frozen_region as frozen
import live_simulator as sim
OUT=ROOT/'research_long'/'results_daily_v2';OUT.mkdir(parents=True,exist_ok=True)
DATA=Path(sys.argv[1])
def dump(p,x):p.write_text(json.dumps(x,indent=2,default=str,allow_nan=False)+'\n')
def hashfile(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(2**20),b''):h.update(b)
    return h.hexdigest()
def finite(x):return float(x) if np.isfinite(x) else None
prices=pd.read_parquet(DATA/'prices.parquet',columns=['date','ticker','open','close','adj_close','volume','source'])
intervals=pd.read_parquet(DATA/'membership_intervals.parquet')
print('SCHEMA',intervals.columns.tolist(),prices.shape,flush=True)
assert {'ticker','start','end'}<=set(intervals)
prices['date']=pd.to_datetime(prices.date).dt.normalize()
prices['ticker']=prices.ticker.astype(str)
intervals['ticker']=intervals.ticker.astype(str)
intervals['start']=pd.to_datetime(intervals.start).dt.normalize()
intervals['end']=pd.to_datetime(intervals['end']).dt.normalize()
assert not prices.duplicated(['date','ticker']).any(), 'Duplicate ticker/date prices'
recycled=pd.read_csv(DATA/'recycled_tickers.csv')
assert 'ticker' in recycled
excluded=set(recycled.ticker.astype(str))
intervals=intervals[~intervals.ticker.isin(excluded)].dropna(subset=['start'])
names_all=sorted(intervals.ticker.unique())
prices=prices[prices.ticker.isin(names_all)&(prices.date>=pd.Timestamp('1970-01-01'))].copy()
fresh=~prices.source.astype(str).str.contains('carried|placeholder',case=False,regex=True)
hasclose=fresh&(prices.close>0)&(prices.adj_close>0)
hasopen=hasclose&(prices.open>0)
actual=hasclose&(prices.volume>0)
prices['valid']=actual
prices['open_verified']=hasopen
# A real next-session close is an explicitly approximate execution reference
# when the source has no actual opening price, never a backdated prior close.
prices['adj_open']=prices.adj_close.where(~hasopen,prices.open*prices.adj_close/prices.close).where(hasclose)
prices['tradable_close']=prices.adj_close.where(hasclose)
prices['dollar_volume']=(prices.close*prices.volume).where(actual)
prices['raw_trade_close']=prices.close.where(hasclose)
# Build actual session calendar from dated price observations, NOT sparse
# membership change events. Require at least 100 observed prices to define a
# market session (no empty weekends/holidays/artificial dates).
calendar=pd.DatetimeIndex(prices.loc[hasclose].groupby('date').size().loc[lambda x:x>=100].index).sort_values()
if calendar.empty:raise RuntimeError('No trading calendar')
events=np.zeros((len(calendar)+1,len(names_all)),dtype=np.int16)
name_ix={t:i for i,t in enumerate(names_all)}
for row in intervals.itertuples(index=False):
    lo=calendar.searchsorted(row.start,side='left')
    hi=calendar.searchsorted(row.end,side='left') if pd.notna(row.end) else len(calendar)
    if lo<hi:
        col=name_ix[row.ticker]
        events[lo,col]+=1;events[hi,col]-=1
member_arr=np.cumsum(events[:-1],axis=0,dtype=np.int16)>0
del events
daily_mem=pd.DataFrame(member_arr,index=calendar,columns=names_all)
# Coverage of price/volume and opening observations among actual members.
pday=calendar.get_indexer(prices.date)
pname=pd.Index(names_all).get_indexer(prices.ticker)
mask=(pday>=0)&(pname>=0)
ix=np.flatnonzero(mask)
ix=ix[member_arr[pday[ix],pname[ix]]]
def observed_count(m):
    return np.bincount(pday[ix[m.to_numpy()[ix]]],minlength=len(calendar))
coverage=pd.DataFrame({'count':observed_count(actual),'size':member_arr.sum(axis=1),
                       'close_count':observed_count(hasclose),'open_count':observed_count(hasopen)},index=calendar)
coverage.index.name='date'
coverage['fraction']=coverage['count']/coverage['size']
coverage['close_fraction']=coverage['close_count']/coverage['size']
coverage['open_fraction']=coverage['open_count']/coverage['size']
coverage.to_csv(OUT/'coverage_daily.csv')
annual_cov=coverage.groupby(coverage.index.year).agg(sessions=('size','size'),mean=('fraction','mean'),min=('fraction','min'),close_mean=('close_fraction','mean'),open_mean=('open_fraction','mean'),members_mean=('size','mean'))
annual_cov.to_csv(OUT/'coverage_annual.csv')
# 95% primary coverage threshold; 40% exploratory threshold deliberately
# acknowledged as incomplete, never considered an unbiased PIT validation.
good=coverage.index[(coverage.fraction>=.95)&(coverage['size']>=450)]
exploratory=coverage.index[(coverage.fraction>=.40)&(coverage['size']>=450)]
if len(good)==0 or len(exploratory)==0:raise RuntimeError('No adequately covered membership dates')
first=good.min();early=exploratory.min()
print('calendar',calendar.min(),calendar.max(),'sessions',len(calendar),'first >=95%',first,'first >=40%',early,flush=True)
manifest={'dataset':'Johnbrick123/sp500-data release data','method_version':'DAILY_INTERVALS_V2_SUPERSEDES_INVALID_SPARSE_CALENDAR_RUN','sha256':{p.name:hashfile(p) for p in DATA.glob('*') if p.is_file()},'calendar_start':str(calendar.min().date()),'calendar_end':str(calendar.max().date()),'calendar_sessions':len(calendar),'first_adequate_date':str(first.date()),'first_exploratory_date':str(early.date()),'last_adequate_date':str(good.max().date()),'recycled_excluded':len(excluded),'limitations':['USA only, historical S&P500 intervals not world market-cap universe','Top100 by lagged trailing dollar volume, not issuer market cap','Market-only residual; dated sector classifications unavailable, no genuine sector weight caps','USD nominal account, no historical EUR conversion; zero cash yield','Adjusted total return units, tax on simulated gains with historical engine tax proxy, no separate dividend withholding','Nominal $50m daily liquidity filter preserved; weak coverage/exposure before the modern era','Execution at observed next-session opening or next-session closing when actual open unavailable','No unobserved fills; original simulator can mark missing holdings at zero after 5 bars and may retain locked slots; delisting proceeds not separately modeled','Exploratory 40% coverage is incomplete and can have severe selection bias','Not exact Atlas replay; no parameter tuning']}
dump(OUT/'manifest.json',manifest)
del member_arr,ix,pday,pname,mask
gc.collect()
summary=[]
def test_window(label,begin,end,first_coverage):
    begin=pd.Timestamp(begin);end=pd.Timestamp(end)
    start=max(begin,first_coverage+pd.DateOffset(years=2))
    if start>=end:return
    st=max(first_coverage,start-pd.DateOffset(years=3))
    dates=coverage.index[(coverage.index>=st)&(coverage.index<=end)].sort_values()
    full_mem=daily_mem.loc[dates]
    names=full_mem.columns[full_mem.any(axis=0)].tolist()
    if len(names)<100:raise RuntimeError('Insufficient symbols')
    pp=prices[(prices.date>=st)&(prices.date<=end)&prices.ticker.isin(names)]
    def panel(key):return pp.pivot(index='date',columns='ticker',values=key).reindex(index=dates,columns=names).astype('float32')
    mem=full_mem[names].copy()
    close=panel('tradable_close');op=panel('adj_open');rawprice=panel('raw_trade_close');dvol=panel('dollar_volume')
    adv=dvol.rolling(20,min_periods=15).median();del dvol
    # At each month's first trading day use the previous session's observed
    # dollar volume and today's known membership, never next month's values.
    roster=pd.DataFrame(False,index=dates,columns=names)
    for _,g in pd.Series(dates,index=dates).groupby(dates.to_period('M')):
        day=g.index[0];pos=dates.get_loc(day)
        if pos<1:continue
        selected=adv.iloc[pos-1].where(mem.loc[day]).dropna().nlargest(100).index
        roster.loc[g.index,selected]=True
    roster &= mem
    logp=np.log(close.where(close>0))
    logret=logp.diff()
    # The point-in-time member basket is the market factor. Trade names are a
    # smaller liquidity proxy; historical sector labels are not invented.
    market=logret.where(mem.shift(1,fill_value=False)).mean(axis=1)
    resid=pd.DataFrame(index=dates,columns=names,dtype=np.float32)
    variance=market.rolling(252,min_periods=160).var().replace(0,np.nan)
    for t in names:
        beta=(logret[t].rolling(252,min_periods=160).cov(market)/variance).shift(1)
        resid[t]=(logret[t]-beta*market).astype('float32')
    r12=resid.shift(21).rolling(231,min_periods=160).sum().astype('float32')
    r6=resid.shift(21).rolling(105,min_periods=80).sum().astype('float32');del resid
    raw12=logp.shift(21)-logp.shift(252)
    fast=close.rolling(50,min_periods=40).mean();slow=close.rolling(200,min_periods=160).mean()
    vol=logret.rolling(63,min_periods=42).std()*math.sqrt(252)
    ext=(logp-logp.shift(5))/(logret.rolling(20,min_periods=15).std().replace(0,np.nan)*math.sqrt(5))
    trend=np.log(close/slow)/vol.replace(0,np.nan)
    def z(x):return v1.robust_cross_sectional_z(x.where(roster)).astype('float32')
    base=.40*z(r12)+.25*z(r6)+.15*z(raw12)+.10*z(trend)+.10*z(-vol)
    eligible=(rawprice>=5)&(adv>=50_000_000)&(close>slow)&(fast>slow)&(raw12>0)&(ext<=2.5)&(ext>=-2)&(r12>0)&roster&base.notna()
    del rawprice,adv,r12,r6,raw12,fast,slow,ext,trend,vol
    # Fixed path penalty and MOM10, with PIT roster for cross-sectional ranks.
    absret=logret.abs();denom=absret.shift(21).rolling(231,min_periods=180).sum().replace(0,np.nan)
    eff=((logp.shift(21)-logp.shift(252)).abs()/denom).clip(0,1)
    jump=(absret.shift(21).rolling(231,min_periods=180).max()/denom).clip(0,1)
    quality=(eff.where(roster).rank(axis=1,pct=True)-jump.where(roster).rank(axis=1,pct=True)).clip(-1,1)
    path=(base+.20*quality).clip(-3,3)
    quality=((path-base)/.20).clip(-1,1)
    mom=close.pct_change(63,fill_method=None).shift(21).where(eligible).rank(axis=1,pct=True)
    score=(base+.20*quality+.10*(2*mom-1)).clip(-3,3).astype('float32')
    del absret,denom,eff,jump,quality,mom,path,logp,logret
    counts=pd.DataFrame({'roster':roster.sum(axis=1),'eligible':eligible.sum(axis=1)})
    counts.to_csv(OUT/f'{label}_counts.csv')
    eligible_counts=counts.loc[start:,'eligible']
    bench=(1+close.pct_change(fill_method=None).where(roster.shift(1,fill_value=False)).mean(axis=1).fillna(0)).loc[start:].cumprod()*10000
    active=eligible.loc[start:].any(axis=0)
    active_names=active[active].index.tolist()
    print(label,'start',start,'end',end,'eligible names',len(active_names),'first eligible',counts.index[counts.eligible>0].min(),flush=True)
    if not active_names: return
    # Keep only names which have an eligible observation for execution. All
    # historical cross-sectional factor and ranking calculations precede this.
    close=close[active_names];op=op[active_names];roster=roster[active_names];eligible=eligible[active_names];score=score[active_names]
    rank=score.where(eligible).rank(axis=1,ascending=False,method='min')
    features=v1.FeatureBundle(close,op,close,score,rank,eligible,score,rank,eligible,pd.Series(True,index=dates),{})
    # Source supplies total return series. Use synthetic adjusted-price units,
    # no additional cash dividend to avoid double counting.
    raw=t9.RawMarketData(op,close,pd.DataFrame(0.,index=dates,columns=active_names,dtype=np.float32),
             pd.DataFrame(False,index=dates,columns=active_names),pd.DataFrame(columns=['date','ticker','classification']))
    v1.TICKER_TO_SECTOR.update({t:'UNCLASSIFIED_'+t for t in active_names})
    frozen.usa_cols=active_names;frozen.region_strength=pd.Series(0.,index=dates)
    frozen.SCALED_EXIT=120;frozen.SCALED_POOL=120;inc.STRENGTH_BETA=.70;inc.CASH_RESERVE=.01
    model=object.__new__(frozen.RegionSlotAtlas)
    model.schedule='ROTATE';model.region_slot_audit=[];model.v1=v1;model.v2=v2;model.v4=v4;model.t9=t9
    model.features=features;model.base_score=base;model.path_score=score;model.eligible=eligible;model.membership=roster
    model.pr_regime=pd.Series(1.,index=dates);model.pr_t8=model.pr_regime.copy()
    model.expected_alpha,_=v4.build_expected_alpha(score,close,str(start.date()))
    model._returns=close.pct_change(fill_method=None)
    def corr(self,date,chosen,candidates):
        ix=dates.get_loc(date);r=self._returns.iloc[max(0,ix-125):ix+1]
        if not chosen:return dict.fromkeys(candidates,0.)
        values=pd.concat([r[candidates].corrwith(r[t]).where(r[candidates].notna().mul(r[t].notna(),axis=0).sum(axis=0)>=63) for t in chosen],axis=1).max(axis=1).fillna(0).clip(lower=0)
        return values.to_dict()
    model.correlations=MethodType(corr,model)
    def slots(self,date,n):return 0,0.,0.
    model.row_slots_at=MethodType(slots,model)
    rate=pd.Series(0.,index=dates);tax=t9.TaxConfig('italian_tax',True)
    policy=t9.ExecutionPolicy(name='coreband0.05',core_risk_band_weight=.05)
    # Matched equal-weight market proxy, gross as a reference (not after-tax).
    # Broader S&P500 equal-weight log-return reference constructed above.
    broad=np.exp(market.loc[start:].fillna(0).cumsum())*10000
    pd.DataFrame({'market_equal_weight_log_proxy_gross':broad,'liquidity_basket_gross':bench}).to_csv(OUT/f'{label}_benchmark.csv')
    for n in (1,3,5,6,7,10):
        name=f'{label}_TOP{n:02d}'
        cfg=replace(v2.V2Config(),max_positions=n,target_volatility=5.,maximum_position_weight=1./n,
                 maximum_sector_weight=max(.40,1./n),minimum_holding_calendar_days=35,
                 decision_interval_weeks=5,minimum_trade_eur=500,rebalance_band_weight=.06,default_slippage_bps_per_side=10.)
        model.bundle={'cfg':cfg,'start':str(start.date())}
        sel,alloc,_,_=model.callbacks(None)
        result=sim.simulate_tax_aware(features,score,eligible,roster,model.pr_regime,rate,None,cfg,raw,str(start.date()),10000.,name,tax,
                  weekly_risk_overlay=None,bear_floor=None,execution_policy=policy,cash_reserve_fraction=.01,
                  name_selector=sel,target_weight_builder=alloc,liquidate_on_final_session=False)
        equity=result.equity.dropna();dd=equity/equity.cummax()-1
        years=(equity.index[-1]-equity.index[0]).days/365.25
        r=equity.pct_change().dropna()
        trades=result.orders
        result.equity.rename('net_liquidation_nav_usd_proxy').to_csv(OUT/f'{name}_nav.csv')
        result.exposure.rename('exposure').to_csv(OUT/f'{name}_exposure.csv')
        trades.to_csv(OUT/f'{name}_orders.csv',index=False)
        # Audit start of actual exposure, since the nominal USD gate may
        # legitimately leave earlier years entirely in cash.
        exposed=result.exposure[result.exposure>0]
        first_trade=str(exposed.index[0].date()) if len(exposed) else None
        active_cagr=((equity.iloc[-1]/10000)**(365.25/max(1,(equity.index[-1]-exposed.index[0]).days))-1)*100 if len(exposed) else None
        row={'window':label,'n':n,'start':str(equity.index[0].date()),'end':str(equity.index[-1].date()),'net_cagr_pct':finite(((equity.iloc[-1]/10000)**(1/years)-1)*100),'max_drawdown_pct':finite(dd.min()*100),'sharpe_zero_cash':finite(r.mean()/r.std()*math.sqrt(252)),'final_nav_usd_proxy':finite(equity.iloc[-1]),'orders':len(trades),'average_exposure_pct':finite(result.exposure.mean()*100),'first_exposure':first_trade,'active_period_cagr_pct':finite(active_cagr) if active_cagr is not None else None,'total_tax_usd_proxy':finite(result.total_tax_eur),'mean_coverage_pct':finite(100*coverage.loc[equity.index,'fraction'].mean()),'mean_eligible_names':finite(eligible_counts.mean()),'trading_sessions':len(equity)}
        summary.append(row)
        pd.DataFrame(summary).to_csv(OUT/'comparison.csv',index=False)
        dump(OUT/'comparison.json',summary)
        print('RESULT',json.dumps(row),flush=True)
        del result
        gc.collect()
    del model,features,close,op,raw,base
    gc.collect()
test_window('pre2012_primary95','1972-01-01','2011-12-31',first)
if early<first: test_window('pre2012_exploratory40','1972-01-01','2011-12-31',early)
test_window('modern','2012-01-01','2025-12-10',first)
if not summary:raise RuntimeError('No backtest results')
print('COMPLETED',len(summary),'configurations',flush=True)
