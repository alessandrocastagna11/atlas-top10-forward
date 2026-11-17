#!/usr/bin/env python3
"""1972–2025 close-only Atlas MOM10 / Confidence70 / passive diagnostic, not exact global parity.
Uses dated S&P 500 constituents and real daily prices. No future membership,
no survivor-only filter. Market-only residual when historical sectors unavailable.
Liquidity-ranked Top100 proxy, USD accounting (no historical EUR), adjusted
total-return units, no separate dividends, original FIFO execution and tax code.
No real orders. Never writes a live branch.
"""
import gc, hashlib, json, math, sys, zipfile, re, io
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
import equity_long_overlay_comparison as overlays
import incumbent_engine as inc
import frozen_region as frozen
import live_simulator as sim
OUT=ROOT/'research_long'/'results_price_1972';OUT.mkdir(parents=True,exist_ok=True)
DATA=Path(sys.argv[1])
def dump(p,x):p.write_text(json.dumps(x,indent=2,default=str,allow_nan=False)+'\n')
def hashfile(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(2**20),b''):h.update(b)
    return h.hexdigest()
def finite(x):return float(x) if np.isfinite(x) else None
# Independent, public capitalization-weighted US market total return
# (Mkt-RF + RF; not S&P 500 exact and not actual historical ETF).
with zipfile.ZipFile(DATA/'ff_daily.zip') as z:
    rawtext=z.read(z.namelist()[0]).decode('latin1')
lines=[l for l in rawtext.splitlines() if re.match(r'^\s*\d{8},',l)]
ff=pd.read_csv(io.StringIO('\n'.join(lines)),header=None,names=['date','Mkt-RF','SMB','HML','RF'])
ff.index=pd.to_datetime(ff.date.astype(str),format='%Y%m%d')
ff_market=(ff['Mkt-RF']+ff['RF'])/100.
assert ff_market.index.is_unique and ff_market.notna().all() and (ff_market>-.95).all()
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
calendar=pd.DatetimeIndex(prices.loc[hasclose].groupby('date').size().loc[lambda x:x>=100].index).sort_values().intersection(ff_market.index)
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
good=coverage.index[(coverage.close_fraction>=.95)&(coverage['size']>=450)]
if len(good)==0:raise RuntimeError('No adequately covered member close dates')
first=good.min()
print('calendar',calendar.min(),calendar.max(),'sessions',len(calendar),'first >=95% close coverage',first,flush=True)
manifest={'dataset':'Johnbrick123/sp500-data release data + Kenneth French daily US market research factors','method_version':'CLOSE_ONLY_1972_MATCHED_COMPARISON_V1','sha256':{p.name:hashfile(p) for p in DATA.glob('*') if p.is_file()},'calendar_start':str(calendar.min().date()),'calendar_end':str(calendar.max().date()),'calendar_sessions':len(calendar),'first_adequate_date':str(first.date()),'recycled_excluded':len(excluded),'limitations':['USA only; dated S&P500 membership instead of global market-cap universe; all contemporaneous members rather than Top100','No historical liquidity or sector constraints; retain price/trend/momentum screens','Market-only OLS residual with independent Fama-French broad-US total return','USD nominal account, zero cash yield; no historical EUR','Adjusted total-return units, no separate dividend withholding, historical engine tax schedule extrapolated before 2012','Execution at observed next weekly session close across entire sample; no carried-patch trading','Absent prices valued using original five-session carry; missing delisting proceeds not independently audited','Passive ETF proxy is Fama-French cap-weighted US market, synthetic 0.07% TER, 10bps each side, 1 currency-unit each side and same stamp/tax schedule; no fund-level withholding','Algos original risk sizing and tax simulator reused; not exact global Atlas replay; no tuning']}
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
    close=panel('tradable_close');op=close
    rawprice=panel('raw_trade_close')
    roster=mem
    del full_mem,pp
    logp=np.log(close.where(close>0))
    logret=logp.diff()
    market=np.log1p(ff_market.reindex(dates))
    assert market.notna().all()
    index_tr=100*np.exp(market.cumsum())
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
    eligible=(rawprice>=5)&(close>slow)&(fast>slow)&(raw12>0)&(ext<=2.5)&(ext>=-2)&(r12>0)&roster&base.notna()
    del rawprice,r12,r6,raw12,fast,slow,ext,trend,vol
    # Fixed path penalty and MOM10, with PIT roster for cross-sectional ranks.
    absret=logret.abs();denom=absret.shift(21).rolling(231,min_periods=180).sum().replace(0,np.nan)
    eff=((logp.shift(21)-logp.shift(252)).abs()/denom).clip(0,1)
    jump=(absret.shift(21).rolling(231,min_periods=180).max()/denom).clip(0,1)
    quality=(eff.where(roster).rank(axis=1,pct=True)-jump.where(roster).rank(axis=1,pct=True)).clip(-1,1)
    path=(base+.20*quality).clip(-3,3).astype('float32')
    quality=((path-base)/.20).clip(-1,1)
    mom=close.pct_change(63,fill_method=None).shift(21).where(eligible).rank(axis=1,pct=True)
    score=(base+.20*quality+.10*(2*mom-1)).clip(-3,3).astype('float32')
    del absret,denom,eff,jump,quality,mom,logp,logret
    counts=pd.DataFrame({'roster':roster.sum(axis=1),'eligible':eligible.sum(axis=1)})
    counts.to_csv(OUT/f'{label}_counts.csv')
    eligible_counts=counts.loc[start:,'eligible']
    active=eligible.loc[start:].any(axis=0)
    active_names=active[active].index.tolist()
    print(label,'start',start,'end',end,'eligible names',len(active_names),'first eligible',counts.index[counts.eligible>0].min(),flush=True)
    if not active_names:return
    # Use membership-normalized breadth before limiting names to historical
    # eligible candidates (a future eligibility test must not change breadth).
    def breadth_for(w,minimum):
        sm=close.rolling(w,min_periods=minimum).mean()
        return ((close>sm)&roster).sum(axis=1)/roster.sum(axis=1).replace(0,np.nan)
    breadth50=breadth_for(50,40)
    breadth80=breadth_for(80,64)
    breadth200=breadth_for(200,160)
    ret20=index_tr.pct_change(20);ret10=index_tr.pct_change(10)
    dd63=index_tr/index_tr.rolling(63,min_periods=42).max()-1
    panic=((dd63<=-.10)&(ret20<=-.05)&(breadth50<.35)).fillna(False)
    recent=panic.shift(1).rolling(42,min_periods=1).max().fillna(0).astype(bool)
    rebound=(recent&(ret10>=.05)&(breadth50>=.50)&((breadth50-breadth50.shift(10))>=.15)).fillna(False)
    sma50=index_tr.rolling(50,min_periods=40).mean()
    sma200=index_tr.rolling(200,min_periods=160).mean()
    annual_vol=market.rolling(63,min_periods=42).std()*math.sqrt(252)
    trend_score=(.65+1.5*np.log(index_tr/sma200)/annual_vol.replace(0,np.nan)).clip(0,1)
    slope_score=(.5+10*(sma50/sma200-1)).clip(0,1)
    risk=(.5*trend_score+.25*slope_score+.25*((breadth200-.35)/.30).clip(0,1)).fillna(0).clip(0,1)
    structural=(index_tr<sma200)&(sma50<sma200)
    risk.loc[structural]=risk.loc[structural].clip(upper=.25)
    rr=market.rolling(63,min_periods=42).std()/market.rolling(20,min_periods=15).std().replace(0,np.nan)
    t8=rr.clip(.5,1).fillna(1)
    t8.loc[panic]=np.minimum(t8.loc[panic],.50)
    risk.loc[rebound]=np.maximum(risk.loc[rebound],.65)
    t8.loc[rebound]=np.maximum(t8.loc[rebound],.90)
    dd=index_tr/index_tr.cummax()-1
    floor=pd.Series(0.,index=dates);floor.loc[structural]=.25;floor.loc[structural&(dd<=-.2)]=.35;floor.loc[structural&(dd<=-.3)]=.45
    close=close[active_names];op=close;roster=roster[active_names];eligible=eligible[active_names];score=score[active_names];path=path[active_names];base=base[active_names]
    del mem
    gc.collect()
    rank=score.where(eligible).rank(axis=1,ascending=False,method='min')
    features=v1.FeatureBundle(close,op,close,score,rank,eligible,score,rank,eligible,pd.Series(True,index=dates),{})
    # Source supplies total return series. Use synthetic adjusted-price units,
    # no additional cash dividend to avoid double counting.
    exec_prices=close.assign(SPY=index_tr)
    raw=t9.RawMarketData(exec_prices,exec_prices,pd.DataFrame(0.,index=dates,columns=exec_prices.columns,dtype=np.float32),
             pd.DataFrame(False,index=dates,columns=exec_prices.columns),pd.DataFrame(columns=['date','ticker','classification']))
    v1.TICKER_TO_SECTOR.update({t:'UNCLASSIFIED_'+t for t in active_names})
    frozen.usa_cols=active_names;frozen.region_strength=pd.Series(0.,index=dates)
    frozen.SCALED_EXIT=120;frozen.SCALED_POOL=120;inc.STRENGTH_BETA=.70;inc.CASH_RESERVE=.01
    model=object.__new__(frozen.RegionSlotAtlas)
    model.schedule='ROTATE';model.region_slot_audit=[];model.v1=v1;model.v2=v2;model.v4=v4;model.t9=t9
    model.features=features;model.base_score=base;model.path_score=score;model.eligible=eligible;model.membership=roster
    model.pr_regime=pd.Series(1.,index=dates);model.pr_t8=model.pr_regime.copy()
    alpha_mom,_=v4.build_expected_alpha(score.where(roster),close.where(roster),str(start.date()))
    alpha_atlas,_=v4.build_expected_alpha(path.where(roster),close.where(roster),str(start.date()))
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
    # Passive ETF proxy: reinvested US market, consistent cost model,
    # 7bp annual TER plus original same stamp schedule. Terminal-only tax
    # is reflected every day for comparison with hypothetical liquidation NAV.
    ix=dates[dates>=start]
    gross=10000*index_tr.loc[ix]/index_tr.loc[ix[0]]
    portfolio=(10000-1)/1.001
    nav=[];equity_gross=[];prev=None
    for day in ix:
        if prev is not None:
            elapsed=(day-prev).days/365.25
            portfolio*=float(index_tr.at[day]/index_tr.at[prev])*math.exp(-(.0007+t9.historical_stamp_rate(prev))*elapsed)
        gross_sale=portfolio*.999-1
        terminal_tax=max(0.,gross_sale-10000)*t9.italian_financial_rate(day)
        nav.append(gross_sale-terminal_tax)
        equity_gross.append(portfolio)
        prev=day
    passive=pd.Series(nav,index=ix,name='net_liquidation_nav_usd_proxy')
    passive.to_csv(OUT/'PASSIVE_US_ETF_PROXY_nav.csv')
    gross.rename('gross_market_index').to_csv(OUT/'PASSIVE_US_market_gross_nav.csv')
    pd.DataFrame({'regime':risk,'T8':t8,'bear_floor':floor,'breadth50':breadth50,'breadth200':breadth200}).to_csv(OUT/'confidence70_regime.csv')
    def metrics(equity,exposure,name,n,orders,total_tax):
        equity=equity.dropna()
        dd=equity/equity.cummax()-1
        years=(equity.index[-1]-equity.index[0]).days/365.25
        rr=equity.pct_change().dropna()
        row={'strategy':name,'n':n,'start':str(equity.index[0].date()),'end':str(equity.index[-1].date()),'net_cagr_pct':finite(((equity.iloc[-1]/10000)**(1/years)-1)*100),'max_drawdown_pct':finite(dd.min()*100),'sharpe_zero_cash':finite(rr.mean()/rr.std()*math.sqrt(252)),'final_nav_usd_proxy':finite(equity.iloc[-1]),'orders':orders,'average_exposure_pct':finite(exposure),'total_tax_usd_proxy':finite(total_tax),'mean_close_coverage_pct':finite(100*coverage.loc[equity.index,'close_fraction'].mean()),'trading_sessions':len(equity)}
        for sub,lo,hi in [('pre2012','1972-01-01','2011-12-31'),('modern','2012-01-01','2025-12-10'),('dotcom','2000-01-01','2002-12-31'),('gfc','2007-01-01','2009-12-31'),('oil','1973-01-01','1974-12-31'),('2020','2020-01-01','2020-12-31')]:
            q=equity.loc[lo:hi]
            if len(q)>1:
                pre=equity.loc[equity.index<q.index[0]]
                initial=float(pre.iloc[-1]) if len(pre) else 10000
                yr=(q.index[-1]-q.index[0]).days/365.25
                row[sub+'_cagr_pct']=finite(((q.iloc[-1]/initial)**(1/yr)-1)*100) if yr>0 else None
                arr=np.r_[initial,q.to_numpy()];row[sub+'_max_drawdown_pct']=finite((arr/np.maximum.accumulate(arr)-1).min()*100)
        summary.append(row)
        pd.DataFrame(summary).to_csv(OUT/'comparison.csv',index=False);dump(OUT/'comparison.json',summary)
        print('RESULT',json.dumps(row),flush=True)
    metrics(passive,100.,'PASSIVE_US_ETF_PROXY',None,2,max(0.,portfolio*.999-1-10000)*t9.italian_financial_rate(ix[-1]))
    for tag in ['TOP01','TOP03','TOP05','TOP06','TOP07','TOP10','CONFIDENCE70']:
        is_atlas=tag=='CONFIDENCE70'
        n=10 if is_atlas else int(tag[-2:])
        name=tag
        cfg=replace(v2.V2Config(),max_positions=n,target_volatility=inc.TARGET_VOL if is_atlas else 5.,maximum_position_weight=.15 if is_atlas else 1./n,
                 maximum_sector_weight=.30 if is_atlas else max(.40,1./n),minimum_holding_calendar_days=35,
                 decision_interval_weeks=5,minimum_trade_eur=500,rebalance_band_weight=.06,default_slippage_bps_per_side=10.)
        model.bundle={'cfg':cfg,'start':str(start.date())}
        model.expected_alpha=alpha_atlas if is_atlas else alpha_mom
        model.pr_regime=risk if is_atlas else pd.Series(1.,index=dates)
        model.pr_t8=t8 if is_atlas else pd.Series(1.,index=dates)
        sel,alloc,_,_=model.callbacks(None)
        result=sim.simulate_tax_aware(features,path if is_atlas else score,eligible,roster,model.pr_regime,rate,None,cfg,raw,str(start.date()),10000.,name,tax,
                  weekly_risk_overlay=t8 if is_atlas else None,bear_floor=floor if is_atlas else None,execution_policy=policy,cash_reserve_fraction=.01,
                  name_selector=sel,target_weight_builder=alloc,liquidate_on_final_session=False)
        result.equity.rename('net_liquidation_nav_usd_proxy').to_csv(OUT/f'{name}_nav.csv')
        result.exposure.rename('exposure').to_csv(OUT/f'{name}_exposure.csv')
        result.orders.to_csv(OUT/f'{name}_orders.csv',index=False)
        metrics(result.equity,100*result.exposure.mean(),name,n,len(result.orders),result.total_tax_eur)
        del result
        gc.collect()
    del model,features,close,op,raw,base
    gc.collect()
test_window('full','1972-01-01','2025-12-10',first)
if not summary:raise RuntimeError('No backtest results')
print('COMPLETED',len(summary),'configurations',flush=True)
