"""V4 Confidence70 original-source forward data adapter (research PAPER only).

Frozen RegionSlotAtlas chooser/sizer, original H4 rolling two-factor OLS,
original V2 continuous regime, T8, panic/rebound and tax-aware simulator.
The *universe provider*, not the strategy, is a current timestamped ACWI
holding-value proxy until independently verifiable 2026 rank panels exist.
No source can be backdated earlier than its actual recording time.
"""
from __future__ import annotations
import concurrent.futures, datetime as dt, hashlib, io, json, math, os, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from scipy import optimize
HERE=Path(__file__).resolve().parent
LIVE=HERE.parent/'live_v4'
CODE=HERE/'code';E=CODE/'engine'
for directory in (str(E),str(CODE),str(HERE)):
    if directory not in sys.path:sys.path.insert(0,directory)
import equity_long_backtest as v1
import equity_long_backtest_v2 as v2
import equity_long_overlay_comparison as overlays
import equity_long_bear_tax_comparison as t9
import equity_long_v4_research as v4
import incumbent_engine as inc
import frozen_region as frozen
import live_simulator as live_sim
from dataclasses import replace

CACHE=HERE/'cache_adjusted'
FXC={'EUR':'EUR','USD':'EURUSD=X'}
SECTOR={
'Information Technology':'XLK','Financials':'XLF','Consumer Discretionary':'XLY',
'Communication':'XLC','Communication Services':'XLC','Health Care':'XLV',
'Industrials':'XLI','Consumer Staples':'XLP','Energy':'XLE','Utilities':'XLU',
'Materials':'XLB','Real Estate':'XLRE'}
ENGINE='ATLAS_UNIFIED_REGION_ROTATE_V4_CONFIDENCE_BETA070_2026-10-07'
INITIAL=10000.0
START_PAPER=pd.Timestamp('2026-11-10') # no retroactive paper fills


def latest_vintages(asof):
    """Every global500 snapshot usable ONLY from recorded_on; no hindsight."""
    folder=LIVE/'dataset'/'universe'/'snapshots'
    groups=[]
    for source in folder.glob('*/source.json'):
        try:
            meta=json.loads(source.read_text())
            recorded=dt.date.fromisoformat(meta['recorded_on'])
            observed=dt.datetime.strptime(meta['source_as_of'],'%b %d, %Y').date()
            if recorded>asof or observed>recorded or meta.get('type')!='ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP':continue
            df=pd.read_csv(source.parent/'global500.csv')
            if len(df)!=500:continue
            rows=[r for r in df.to_dict('records') if pd.notna(r['symbol']) and str(r['symbol']).strip()]
            if len(rows)<450:continue
            u=[r for r in rows if r['country']=='United States'][:100]
            g=[r for r in rows if r['country']!='United States'][:200]
            if len(u)!=100 or len(g)!=200:continue
            h=hashlib.sha256((source.parent/'global500.csv').read_bytes()).hexdigest()
            groups.append((recorded,observed,h,u,g))
        except (KeyError,ValueError,FileNotFoundError):continue
    if not groups:raise ValueError('No as-recorded contemporary universe snapshots')
    groups.sort(key=lambda x:x[0])
    if (asof-groups[-1][1]).days>10:raise ValueError('Universe vintage stale >10 days')
    return groups


def _cache_filename(symbol):
    return hashlib.sha1(symbol.encode()).hexdigest()[:12]+'.csv.gz'


def _chart(symbol,end_day,refresh=True):
    import gzip
    CACHE.mkdir(parents=True,exist_ok=True)
    path=CACHE/_cache_filename(symbol)
    existing=None
    if path.is_file():
        try:
            existing=pd.read_csv(path,parse_dates=['Date'],index_col='Date')
            if 'adj_close' not in existing.columns:existing=None
        except Exception: existing=None
    if os.environ.get('ATLAS_EXACT_OFFLINE')=='1':
        if existing is None:raise RuntimeError('Missing offline adjusted chart: '+symbol)
        return existing.loc[:str(end_day)].sort_index()
    # Refresh the entire calibration window: dividend-adjustment factors can
    # revise old rows after ex-dates. Do NOT mix changed bases silently.
    beg=int(dt.datetime(2023,1,1,tzinfo=dt.timezone.utc).timestamp())
    end=int((dt.datetime.combine(end_day,dt.time(0),dt.timezone.utc)+dt.timedelta(days=3)).timestamp())
    params=urllib.parse.urlencode({'period1':beg,'period2':end,'interval':'1d','events':'div,splits','includeAdjustedClose':'true'})
    last=None
    for attempt in range(3):
        host='query1.finance.yahoo.com' if attempt%2==0 else 'query2.finance.yahoo.com'
        url=f'https://{host}/v8/finance/chart/{urllib.parse.quote(symbol,safe="")}?{params}'
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; AtlasV4ExactPaper/1.0)','Accept':'application/json'})
            with urllib.request.urlopen(req,timeout=22) as f: payload=json.load(f)
            raw=payload['chart']['result'][0]
            if not raw:raise RuntimeError('empty result')
            timestamps=raw.get('timestamp') or []
            q=(raw['indicators'].get('quote') or [{}])[0]
            aj=(raw['indicators'].get('adjclose') or [{}])[0].get('adjclose') or q.get('close')
            if len(timestamps)<300 or not aj or len(aj)!=len(timestamps):raise RuntimeError('insufficient history')
            zone=raw.get('meta',{}).get('exchangeTimezoneName','UTC')
            try:timezone=ZoneInfo(zone)
            except Exception:raise RuntimeError('unknown exchange timezone '+str(zone))
            index=pd.DatetimeIndex([pd.Timestamp(x,unit='s',tz='UTC').tz_convert(timezone).date() for x in timestamps],name='Date')
            clos=pd.to_numeric(pd.Series(q.get('close')),errors='coerce')
            adj=pd.to_numeric(pd.Series(aj),errors='coerce')
            factor=adj/clos.replace(0,np.nan)
            opened=pd.to_numeric(pd.Series(q.get('open')),errors='coerce')
            vol=pd.to_numeric(pd.Series(q.get('volume')),errors='coerce')
            frame=pd.DataFrame({'adj_open':(opened*factor).to_numpy(),
                                'adj_close':adj.to_numpy(),
                                'raw_close':clos.to_numpy(),
                                'volume':vol.to_numpy(),
                                'dividend':np.zeros(len(index))},index=pd.to_datetime(index))
            for ev in (raw.get('events',{}).get('dividends') or {}).values():
                stamp=int(ev['date']);day=pd.Timestamp(stamp,unit='s',tz='UTC').tz_convert(timezone).tz_localize(None).normalize()
                if day in frame.index:
                    # Split-adjust historical dividend; source is explicit event.
                    frame.loc[day,'dividend']+=float(ev['amount'])
            frame=frame[~frame.index.duplicated(keep='last')].sort_index()
            frame=frame.loc[:pd.Timestamp(end_day)].dropna(subset=['adj_open','adj_close'])
            frame=frame[(frame.adj_open>0)&(frame.adj_close>0)]
            if len(frame)<300:raise RuntimeError('few clean adjusted closes')
            frame.to_csv(path,compression='gzip',float_format='%.10g')
            return frame
        except Exception as exc:
            last=exc
            time.sleep((attempt+1)*0.8)
    if existing is not None:
        old=existing.loc[:str(end_day)].sort_index()
        if (end_day-old.index[-1].date()).days<=4:return old
    raise RuntimeError(f'Yahoo adjusted chart unavailable: {symbol}: {last}')


def get_adjusted(symbols,asof,max_workers=8):
    unique=sorted(set(symbols));d={};failed={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        jobs={pool.submit(_chart,s,asof):s for s in unique}
        for future in concurrent.futures.as_completed(jobs):
            symbol=jobs[future]
            try:d[symbol]=future.result()
            except Exception as exc:failed[symbol]=str(exc)
    return d,failed


def _fx(frames,code,index):
    if code=='EUR':return pd.Series(1.0,index=index)
    key='EUR'+code+'=X'
    if key not in frames:raise ValueError('Missing FX: '+key)
    vals=frames[key]['adj_close'].reindex(index).ffill(limit=5)
    if vals.loc[index[-1]]<=0 or not np.isfinite(vals.loc[index[-1]]):raise ValueError('Stale FX '+key)
    return vals


def build_model(snapshot,asof,frames):
    """Construct the exact replay's H4 features/regime/RegionSlotAtlas callbacks.
    The 2026 membership datasource is explicitly NOT the historical PIT market-cap roster.
    """
    last,observed,vintage_hash,us,global_non= snapshot
    symbols_us=[str(x['symbol']).upper() for x in us]
    symbols_row=[str(x['symbol']).upper() for x in global_non]
    if set(symbols_us)&set(symbols_row):raise ValueError('Regional symbol overlap')
    sp=frames['SPY'];index=sp.index[(sp.index>=pd.Timestamp('2023-01-01'))&(sp.index<=pd.Timestamp(asof))]
    if len(index)<500:raise ValueError('Benchmark calibration shorter than 500 sessions')
    # Do not substitute present constituents for actual historical investible membership.
    # Only recorded forward vintages are tradable; backwards prices are for factors.
    row_assets=['G::'+s for s in symbols_row]
    cols=symbols_us+row_assets
    currency={str(x['symbol']).upper():str(x['currency']).upper() for x in us+global_non}
    sector={str(x['symbol']).upper():SECTOR.get(str(x['sector'])) for x in us+global_non}
    if any(not sector[s] for s in symbols_us+symbols_row):raise ValueError('Unknown sector in roster')
    for s in symbols_us+symbols_row:
        if s not in frames:raise ValueError('Missing adjusted price '+s)
    fx={c:_fx(frames,c,index) for c in set(currency.values())|{'USD'}}
    fx_prev={c:series.shift(1) for c,series in fx.items()}
    # Pre-close data: model is computed only after complete US close.
    close_usd={};close_eur={};open_eur={};local_close={};vol={};div={}
    for symbol in symbols_us+symbols_row:
        a=frames[symbol].reindex(index)
        eur_close=a.adj_close/fx[currency[symbol]]
        usd_close=eur_close*fx['USD']
        eur_open=a.adj_open/fx_prev[currency[symbol]]
        key=symbol if symbol in symbols_us else 'G::'+symbol
        close_usd[key]=usd_close
        close_eur[key]=eur_close
        open_eur[key]=eur_open
        local_close[key]=a.adj_close
        vol[key]=a.raw_close*a.volume*fx['USD']/fx[currency[symbol]]
        div[key]=a.dividend/fx[currency[symbol]]
    stock_us=pd.DataFrame({s:close_usd[s] for s in symbols_us},index=index)
    dollar_vol=pd.DataFrame({s:vol[s] for s in cols},index=index)
    # Original US sector factors: real SPY & original sector ETFs in adjusted USD.
    v1.TICKER_TO_SECTOR.update({s:sector[s] for s in symbols_us})
    v1.TICKER_TO_SECTOR.update({'G::'+s:sector[s] for s in symbols_row})
    d={'SPY':pd.DataFrame({'Open':sp.adj_open.reindex(index),'Close':sp.adj_close.reindex(index),
                           'RawClose':sp.raw_close.reindex(index),'Volume':sp.volume.reindex(index)},index=index)}
    usd_raw=frames['EURUSD=X']
    d['EURUSD=X']=pd.DataFrame({'Open':usd_raw.adj_open.reindex(index),'Close':usd_raw.adj_close.reindex(index)},index=index)
    for factor in set(sector.values()):
        if factor not in frames:raise ValueError('Missing original sector benchmark '+factor)
        q=frames[factor].reindex(index)
        d[factor]=pd.DataFrame({'Open':q.adj_open,'Close':q.adj_close},index=index)
    for s in symbols_us:
        q=frames[s].reindex(index)
        d[s]=pd.DataFrame({'Open':q.adj_open,'Close':q.adj_close,'RawClose':q.raw_close,'Volume':q.volume},index=index)
    cfg=v1.StrategyConfig()
    usf=v1.build_features(d,cfg)
    # The archived replay's exact RoW feature mathematics, using OLS residuals
    # against the equal-weight RoW market and sector panels (not proxy half-return).
    roclose=pd.DataFrame({a:close_usd[a] for a in row_assets},index=index)
    roeur=pd.DataFrame({a:close_eur[a] for a in row_assets},index=index)
    roopen=pd.DataFrame({a:open_eur[a] for a in row_assets},index=index)
    factor_mem=pd.DataFrame(True,index=index,columns=row_assets)
    logp=np.log(roclose.where(roclose>0));ret=logp.diff();masked=ret.where(factor_mem)
    market=masked.mean(axis=1,skipna=True)
    secs={a:sector[a[3:]] for a in row_assets}
    sector_factor={name:masked[[a for a in row_assets if secs[a]==name]].mean(axis=1,skipna=True)
                   for name in sorted(set(secs.values()))}
    residual=pd.DataFrame({a:v1.rolling_two_factor_residual(ret[a],market,sector_factor[secs[a]]) for a in row_assets},index=index)
    res12=residual.shift(21).rolling(231,min_periods=160).sum()
    res6=residual.shift(21).rolling(105,min_periods=80).sum()
    raw12=logp.shift(21)-logp.shift(252);raw6=logp.shift(21)-logp.shift(126);raw3=logp.shift(21)-logp.shift(63)
    sma50=roclose.rolling(50,min_periods=40).mean();sma200=roclose.rolling(200,min_periods=160).mean()
    vol63=ret.rolling(63,min_periods=42).std()*math.sqrt(252)
    dv20=ret.rolling(20,min_periods=15).std()
    trend=np.log(roclose/sma200)/vol63.replace(0,np.nan)
    ext=(logp-logp.shift(5))/(dv20*math.sqrt(5)).replace(0,np.nan)
    mdv=dollar_vol[row_assets].rolling(20,min_periods=15).median()
    rz=v1.robust_cross_sectional_z
    row_score=(.35*rz(res12)+.20*rz(res6)+.15*rz(raw12)+.10*rz(raw6)+.05*rz(raw3)+.075*rz(trend)+.075*rz(-vol63))
    row_elig=(roclose>=cfg.minimum_price_usd)&(mdv>=cfg.minimum_median_dollar_volume)&(roclose>sma200)&(sma50>sma200)&(raw12>0)&(ext<=cfg.maximum_extension_z)&(ext>=cfg.minimum_extension_z)&(res12>0)&row_score.notna()
    # The same source class RegionSlotAtlas is invoked, with initial membership
    # effective *only* from its first actually recorded 2026 snapshot.
    raw_close=pd.DataFrame(close_eur,index=index)
    raw_open=pd.DataFrame(open_eur,index=index)
    dividends=pd.DataFrame(div,index=index).fillna(0)
    # 2026 membership starts when we actually observed it. No artificial
    # historical fills from the 2026 cohort.
    membership=pd.DataFrame(False,index=index,columns=cols)
    if pd.Timestamp(last) in index:
        membership.loc[index>=pd.Timestamp(last),cols]=True
    elif index[-1]>=pd.Timestamp(last):
        membership.loc[index>=pd.Timestamp(last),cols]=True
    score=pd.concat([usf.score[symbols_us],row_score],axis=1)
    elig=pd.concat([usf.eligible[symbols_us],row_elig],axis=1)
    close_eur_all=pd.DataFrame(close_eur,index=index)
    open_eur_all=pd.DataFrame(open_eur,index=index)
    close_usd_all=pd.DataFrame(close_usd,index=index)
    ft={}
    for key in set(usf.feature_tables)|{'residual_12_1','residual_6_1','raw_12_1','raw_6_1','trend_strength','extension_5','annual_vol_63','median_dollar_volume'}:
        left=usf.feature_tables.get(key,pd.DataFrame(index=index,columns=symbols_us)).reindex(index).reindex(columns=symbols_us)
        right={'residual_12_1':res12,'residual_6_1':res6,'raw_12_1':raw12,'raw_6_1':raw6,'trend_strength':trend,'extension_5':ext,'annual_vol_63':vol63,'median_dollar_volume':mdv}.get(key,pd.DataFrame(index=index,columns=row_assets))
        ft[key]=pd.concat([left,right],axis=1)
    features=v1.FeatureBundle(close_usd_all,open_eur_all,close_eur_all,score,
             score.where(elig).rank(axis=1,ascending=False,method='min'),elig,
             score,score.where(elig).rank(axis=1,ascending=False,method='min'),elig,
             pd.Series(True,index=index),ft)
    def eq_index(series):
        return 100*np.exp(np.log(series.where(series>0)).diff().mean(axis=1,skipna=True).fillna(0).cumsum())
    usa_index=eq_index(close_eur_all[symbols_us]);row_index=eq_index(close_eur_all[row_assets]);logrel=np.log(usa_index/row_index)
    relvol=logrel.diff().rolling(252,min_periods=126).std()
    region_strength=(.6*(logrel.shift(21)-logrel.shift(252))/(relvol*np.sqrt(231))+.4*(logrel.shift(21)-logrel.shift(126))/(relvol*np.sqrt(105))).clip(-2.5,2.5).fillna(0)
    frozen.usa_cols=symbols_us
    frozen.region_strength=region_strength
    frozen.SCALED_EXIT=max(40,int(round(.40*len(cols))))
    frozen.SCALED_POOL=frozen.SCALED_EXIT
    inc.STRENGTH_BETA=.70
    m=object.__new__(frozen.RegionSlotAtlas)
    m.schedule='ROTATE';m.region_slot_audit=[]
    m.v1=v1;m.v2=v2;m.v4=v4;m.t9=t9
    m.bundle={'cfg':replace(v2.V2Config(),maximum_position_weight=.15,maximum_sector_weight=.30,
                 decision_interval_weeks=5,minimum_holding_calendar_days=35,
                 max_positions=10,rebalance_band_weight=.06,minimum_trade_eur=500),
              'start':str(START_PAPER.date())}
    m.features=features;m.base_score=score;m.eligible=elig;m.membership=membership
    raw_close['SPY']=sp.adj_close.reindex(index)/fx['USD']
    raw_open['SPY']=sp.adj_open.reindex(index)/fx_prev['USD']
    dividends['SPY']=sp.dividend.reindex(index).fillna(0)/fx['USD']
    taxable=dividends.ge(0)
    m.raw=t9.RawMarketData(raw_open,raw_close,dividends,taxable,pd.DataFrame(columns=['date','ticker','classification']))
    m.base_regime=v2.continuous_regime(usf,d)
    m.base_t8=overlays.t8_volatility_overlay(d,index)[0]
    bear=t9.bear_exposure_signals(d,usf,m.base_t8)
    m.bear_floor=bear['t9a_exposure_floor']
    m.cash_rate,ecb_source=v2.fetch_ecb_cash_rate(index,CACHE,refresh=False,haircut_bps=50)
    if ecb_source.startswith('cash yield disabled'):
        raise ValueError('Cannot replicate frozen EUR cash model without ECB: '+ecb_source)
    m.tax=t9.TaxConfig('italian_tax',True)
    m.net_cash=t9.net_cash_rate(m.cash_rate,m.tax)
    m._build_path_score();m._build_panic_rebound()
    m._returns=m.features.close_eur.pct_change(fill_method=None);m._corr_cache={}
    m.expected_alpha,_=v4.build_expected_alpha(m.path_score,m.features.close_eur,str(START_PAPER.date()))
    return m,{'roster_recorded_date':str(last),'roster_source_asof':str(observed),'roster_sha256':vintage_hash,
               'universe_kind':'ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP',
               'ticker_to_market_symbol':{x:x for x in symbols_us}|{'G::'+x:x for x in symbols_row},
               'active_roster_names':len(cols),'last_benchmark_date':str(index[-1].date())}


def evaluate_model(m,provenance):
    if m.features.close_eur.index[-1]<START_PAPER:
        return None,{'status':'PENDING_US_CLOSE','reason':'paper start session unavailable'}
    selector,weight_builder,_,_=m.callbacks(None)
    inc.CAPITAL=INITIAL
    cfg=replace(m.bundle['cfg'],target_volatility=inc.TARGET_VOL,
                max_positions=10,default_slippage_bps_per_side=inc.SLIPPAGE_BPS)
    policy=t9.ExecutionPolicy(name=f'corrected_coreband{inc.CORE_BAND}',core_risk_band_weight=inc.CORE_BAND)
    result=live_sim.simulate_tax_aware(m.features,m.path_score,m.eligible,m.membership,
             m.pr_regime,m.cash_rate,None,cfg,m.raw,str(START_PAPER.date()),INITIAL,
             'UNIFIED_REGION_THRESHOLD_ROTATE',m.tax,weekly_risk_overlay=m.pr_t8,
             bear_floor=m.bear_floor,execution_policy=policy,cash_reserve_fraction=inc.CASH_RESERVE,
             name_selector=selector,target_weight_builder=weight_builder,liquidate_on_final_session=False)
    return result,{'status':'PAPER_REPLAYED_EXACT_CODE_PATH','source':'frozen RegionSlotAtlas + t9 tax engine no terminal liquidation',
                   **provenance}
