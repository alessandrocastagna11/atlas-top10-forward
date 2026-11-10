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
    """Only cohorts actually recorded by this decision date; no backdating.

    From the first validated 100-USA/200-RoW market-cap observation onward,
    switch the candidate ordering to issuer-cap within the ACWI candidate pool.
    This is NOT the unrestricted historically certified Top200-by-market-cap.
    """
    folder=LIVE/'dataset'/'universe'/'snapshots'
    byday={}
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
            byday[recorded]=(recorded,observed,h,u,g)
        except (KeyError,ValueError,FileNotFoundError):continue
    # First verified vendor observation for a date is immutable once it is
    # accepted. Incomplete matches do not replace the existing ETF universe.
    capdir=LIVE/'dataset'/'marketcap_observed'/'observations'
    cap_kind='MARKET_CAP_USD_AS_RECORDED_WITHIN_ACWI_NOT_HISTORICAL_PIT'
    for source in sorted(capdir.glob('*/source.json')):
        try:
            meta=json.loads(source.read_text(encoding='utf8'))
            recorded=dt.date.fromisoformat(meta['capture_day'])
            observed=dt.datetime.fromisoformat(meta['observed_at_utc'])
            if observed.tzinfo is None:continue
            if observed.date()!=recorded or recorded>asof:continue
            if not meta.get('sufficient_for_100_usa_200_nonusa',False):continue
            if meta.get('historical_pit_market_cap_verified') is not False:continue
            datafile=source.parent/'ranking.csv'
            df=pd.read_csv(datafile)
            if not {'market_cap_usd','symbol','sector','country','currency','market_cap_vendor_rank'}.issubset(df.columns):continue
            if df.symbol.duplicated().any() or df.market_cap_vendor_rank.duplicated().any():continue
            df['market_cap_usd']=pd.to_numeric(df['market_cap_usd'],errors='coerce')
            if df.market_cap_usd.isna().any() or not (df.market_cap_usd>0).all():continue
            ordered=df.sort_values('market_cap_usd',ascending=False).to_dict('records')
            u=[r for r in ordered if r['country']=='United States'][:100]
            g=[r for r in ordered if r['country']!='United States'][:200]
            if len(u)!=100 or len(g)!=200:continue
            h=hashlib.sha256(datafile.read_bytes()).hexdigest()
            old=byday.get(recorded)
            # Prefer the FIRST qualifying market-cap observation of the day.
            if old is None or len(old)==5:
                byday[recorded]=(recorded,recorded,h,u,g,cap_kind)
        except (KeyError,ValueError,TypeError,FileNotFoundError):continue
    # Once the cap-ranked source has activated, use its most recent dated
    # observation during temporary vendor gaps. Never silently revert to the
    # ETF-weight ranking on an ordinary day just because today's cap page fails.
    cap_days=[date for date,item in byday.items() if len(item)>5]
    if cap_days:
        cap_start=min(cap_days)
        for date,item in list(byday.items()):
            if date>cap_start and len(item)==5:
                del byday[date]
    groups=sorted(byday.values(),key=lambda x:x[0])
    if not groups:raise ValueError('No as-recorded contemporary universe snapshots')
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


def asof_membership(index,snapshots,symbols_us,row_assets):
    """Point-in-time recorded roster, never replaced retroactively."""
    cols=list(symbols_us)+list(row_assets)
    out=pd.DataFrame(False,index=index,columns=cols,dtype=bool)
    for n,v in enumerate(snapshots):
        start=pd.Timestamp(v[0])
        stop=pd.Timestamp(snapshots[n+1][0]) if n+1<len(snapshots) else pd.Timestamp.max
        days=index[(index>=start)&(index<stop)]
        names=[str(r['symbol']).upper() for r in v[3]]
        names+=['G::'+str(r['symbol']).upper() for r in v[4]]
        if any(k not in cols for k in names):
            raise ValueError('Roster contains unmapped ticker')
        out.loc[days,names]=True
    return out


def build_model(snapshot,asof,frames):
    """Construct the exact replay's H4 features/regime/RegionSlotAtlas callbacks.
    The 2026 membership datasource is explicitly NOT the historical PIT market-cap roster.
    """
    # A roster is effective from its actual recorded date, never earlier.
    # Preserve earlier memberships while new daily snapshots are appended.
    snapshots=([snapshot] if isinstance(snapshot,tuple) else list(snapshot))
    if not snapshots:raise ValueError('No recorded roster vintage')
    snapshots.sort(key=lambda v:v[0])
    if len({v[0] for v in snapshots})!=len(snapshots):
        raise ValueError('Duplicate roster date would make the forward ledger non-deterministic')
    last,observed,vintage_hash,us,global_non=snapshots[-1][:5]
    latest_kind=(snapshots[-1][5] if len(snapshots[-1])>5 else 'ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP')
    symbols_us=list(dict.fromkeys(str(x['symbol']).upper() for v in snapshots for x in v[3]))
    symbols_row=list(dict.fromkeys(str(x['symbol']).upper() for v in snapshots for x in v[4]))
    if set(symbols_us)&set(symbols_row):raise ValueError('Regional symbol overlap')
    # Asset identities, sector mappings and currencies must not mutate silently.
    seen={}
    for vintage in snapshots:
        for row in vintage[3]+vintage[4]:
            symbol=str(row['symbol']).upper()
            ident=(str(row['currency']).upper(),SECTOR.get(str(row['sector'])))
            if symbol in seen and seen[symbol]!=ident:
                raise ValueError('Issuer symbol mapping changed; manual corporate-action review: '+symbol)
            seen[symbol]=ident
    sp=frames['SPY'];index=sp.index[(sp.index>=pd.Timestamp('2023-01-01'))&(sp.index<=pd.Timestamp(asof))]
    if len(index)<500:raise ValueError('Benchmark calibration shorter than 500 sessions')
    # Do not substitute present constituents for actual historical investible membership.
    # Only recorded forward vintages are tradable; backwards prices are for factors.
    row_assets=['G::'+s for s in symbols_row]
    cols=symbols_us+row_assets
    currency={symbol:ident[0] for symbol,ident in seen.items()}
    sector={symbol:ident[1] for symbol,ident in seen.items()}
    if any(not sector[s] for s in symbols_us+symbols_row):raise ValueError('Unknown sector in roster')
    for s in symbols_us+symbols_row:
        if s not in frames:raise ValueError('Missing adjusted price '+s)
    fx={c:_fx(frames,c,index) for c in set(currency.values())|{'USD'}}
    fx_prev={c:series.shift(1) for c,series in fx.items()}
    # Pre-close data: model is computed only after complete US close.
    close_usd={};close_eur={};open_eur={};local_close={};vol={};div={}
    execution_close_eur={};execution_open_eur={}
    for symbol in symbols_us+symbols_row:
        a=frames[symbol].reindex(index)
        # Different exchanges have different holidays. Carry the LAST known
        # close for marking and rolling signals (never a fabricated open).
        # A missed trading session is not a zero-value position.
        usable_adj_close=a.adj_close.ffill(limit=5)
        usable_raw_close=a.raw_close.ffill(limit=5)
        eur_close=usable_adj_close/fx[currency[symbol]]
        usd_close=eur_close*fx['USD']
        eur_open=a.adj_open/fx_prev[currency[symbol]]
        key=symbol if symbol in symbols_us else 'G::'+symbol
        # Signal prices use dividend-adjusted total-return history.
        # PAPER executions and cash-dividend accounting MUST instead use
        # actual split-adjusted exchange prices. Combining adjusted close
        # with cash dividends would count the same dividend twice.
        dividend_factor=usable_adj_close/usable_raw_close.replace(0,np.nan)
        execution_close_eur[key]=usable_raw_close/fx[currency[symbol]]
        execution_open_eur[key]=(a.adj_open/dividend_factor)/fx_prev[currency[symbol]]
        close_usd[key]=usd_close
        close_eur[key]=eur_close
        open_eur[key]=eur_open
        local_close[key]=a.adj_close
        vol[key]=usable_raw_close*a.volume.fillna(0.)*fx['USD']/fx[currency[symbol]]
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
    # Membership changes only on each snapshot's RECORDED day. Historical
    # calibration bars are usable before first capture, but never tradeable.
    membership=asof_membership(index,snapshots,symbols_us,row_assets)
    factor_mem=membership[row_assets].copy()
    historical=index<pd.Timestamp(snapshots[0][0])
    if historical.any():
        factor_mem.loc[historical,['G::'+str(r['symbol']).upper() for r in snapshots[0][4]]]=True
    us_factor_mem=membership[symbols_us].copy()
    if historical.any():
        us_factor_mem.loc[historical,[str(r['symbol']).upper() for r in snapshots[0][3]]]=True
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
    row_score=(.35*rz(res12.where(factor_mem))+.20*rz(res6.where(factor_mem))
               +.15*rz(raw12.where(factor_mem))+.10*rz(raw6.where(factor_mem))
               +.05*rz(raw3.where(factor_mem))+.075*rz(trend.where(factor_mem))
               +.075*rz((-vol63).where(factor_mem)))
    row_elig=(roclose>=cfg.minimum_price_usd)&(mdv>=cfg.minimum_median_dollar_volume)&(roclose>sma200)&(sma50>sma200)&(raw12>0)&(ext<=cfg.maximum_extension_z)&(ext>=cfg.minimum_extension_z)&(res12>0)&row_score.notna()
    # Recalculate ONLY the US cross-sectional z-scores against each day's
    # historically observed roster. Otherwise adding a new symbol today
    # would silently re-rank last week's holdings and corrupt the ledger.
    def uz(name):return rz(usf.feature_tables[name][symbols_us].where(us_factor_mem))
    # The original low-volatility signal z-scores the NEGATIVE volatility.
    us_score=(cfg.score_residual_12_1*uz('residual_12_1')
              +cfg.score_residual_6_1*uz('residual_6_1')
              +cfg.score_raw_12_1*uz('raw_12_1')
              +cfg.score_trend*uz('trend_strength')
              +cfg.score_low_volatility*rz((-usf.feature_tables['annual_vol_63'][symbols_us]).where(us_factor_mem)))
    raw_close=pd.DataFrame(execution_close_eur,index=index)
    raw_open=pd.DataFrame(execution_open_eur,index=index)
    dividends=pd.DataFrame(div,index=index).fillna(0)
    score=pd.concat([us_score,row_score],axis=1)
    elig=pd.concat([usf.eligible[symbols_us] & membership[symbols_us],
                    row_elig & membership[row_assets]],axis=1)
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
    def eq_index(series,allowed):
        # A new vintage must not alter an earlier regime classification.
        logrets=np.log(series.where(series>0)).diff().where(allowed)
        return 100*np.exp(logrets.mean(axis=1,skipna=True).fillna(0).cumsum())
    usa_index=eq_index(close_eur_all[symbols_us],us_factor_mem)
    row_index=eq_index(close_eur_all[row_assets],factor_mem)
    logrel=np.log(usa_index/row_index)
    relvol=logrel.diff().rolling(252,min_periods=126).std()
    region_strength=(.6*(logrel.shift(21)-logrel.shift(252))/(relvol*np.sqrt(231))+.4*(logrel.shift(21)-logrel.shift(126))/(relvol*np.sqrt(105))).clip(-2.5,2.5).fillna(0)
    frozen.usa_cols=symbols_us
    frozen.region_strength=region_strength
    # Exactly 100 US + 200 rest-of-world are eligible per vintage.
    # This threshold cannot depend on the growing number of historic symbols.
    frozen.SCALED_EXIT=120
    frozen.SCALED_POOL=120
    inc.STRENGTH_BETA=.70
    m=object.__new__(frozen.RegionSlotAtlas)
    m.schedule='ROTATE';m.region_slot_audit=[]
    m.v1=v1;m.v2=v2;m.v4=v4;m.t9=t9
    m.bundle={'cfg':replace(v2.V2Config(),maximum_position_weight=.15,maximum_sector_weight=.30,
                 decision_interval_weeks=5,minimum_holding_calendar_days=35,
                 max_positions=10,rebalance_band_weight=.06,minimum_trade_eur=500),
              'start':str(START_PAPER.date())}
    m.features=features;m.base_score=score;m.eligible=elig;m.membership=membership
    raw_close['SPY']=sp.raw_close.reindex(index)/fx['USD']
    sp_factor=sp.adj_close.reindex(index)/sp.raw_close.reindex(index).replace(0,np.nan)
    raw_open['SPY']=(sp.adj_open.reindex(index)/sp_factor)/fx_prev['USD']
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
    # Historical calibration uses the first prospectively observed cohort;
    # each following day uses ONLY that day's as-recorded cohort. Otherwise
    # cross-sectional path quality / breadth / alpha regressions would
    # retroactively change when a future constituent entered the universe.
    calibration_mask=membership.copy()
    if historical.any():
        first_names=[str(r['symbol']).upper() for r in snapshots[0][3]]
        first_names+=['G::'+str(r['symbol']).upper() for r in snapshots[0][4]]
        calibration_mask.loc[historical,first_names]=True
    log_price=np.log(m.features.close_eur.where(m.features.close_eur>0))
    absret=log_price.diff().abs()
    denominator=absret.shift(21).rolling(231,min_periods=180).sum()
    efficiency=((log_price.shift(21)-log_price.shift(252)).abs()/denominator.replace(0,np.nan)).clip(0,1)
    jump_share=(absret.shift(21).rolling(231,min_periods=180).max()/denominator.replace(0,np.nan)).clip(0,1)
    quality=(efficiency.where(calibration_mask).rank(axis=1,pct=True)
             -jump_share.where(calibration_mask).rank(axis=1,pct=True)).clip(-1,1)
    m.path_score=(m.base_score+0.20*quality).clip(-3,3)
    spy=m.raw.close_eur['SPY'];return20=spy.pct_change(20);return10=spy.pct_change(10)
    drawdown63=spy/spy.rolling(63,min_periods=42).max()-1.0
    sma50=m.features.close_eur.rolling(50,min_periods=40).mean()
    breadth=((m.features.close_eur>sma50)&calibration_mask).sum(axis=1)/calibration_mask.sum(axis=1).replace(0,np.nan)
    breadth_change10=breadth-breadth.shift(10)
    m.panic=((drawdown63<=-.10)&(return20<=-.05)&(breadth<.35)).fillna(False)
    recent=m.panic.shift(1).rolling(42,min_periods=1).max().fillna(0).astype(bool)
    m.rebound=(recent&(return10>=.05)&(breadth>=.50)&(breadth_change10>=.15)).fillna(False)
    m.pr_t8=m.base_t8.copy();m.pr_t8.loc[m.panic]=np.minimum(m.pr_t8.loc[m.panic],.50)
    m.pr_regime=m.base_regime.copy();m.pr_regime.loc[m.rebound]=np.maximum(m.pr_regime.loc[m.rebound],.65)
    m.pr_t8.loc[m.rebound]=np.maximum(m.pr_t8.loc[m.rebound],.90)
    m._returns=m.features.close_eur.pct_change(fill_method=None);m._corr_cache={}
    # First fill on START_PAPER uses the PREVIOUS complete market session's
    # decision. The 63-session PIT alpha calibration must already exist then.
    decision_history=index[index<START_PAPER]
    signal_start=decision_history[-1] if len(decision_history) else START_PAPER
    m.expected_alpha,_=v4.build_expected_alpha(
        m.path_score.where(calibration_mask),
        m.features.close_eur.where(calibration_mask),
        str(signal_start.date()))
    return m,{'roster_recorded_date':str(last),'roster_source_asof':str(observed),'roster_sha256':vintage_hash,
               'universe_kind':latest_kind,
               'ticker_to_market_symbol':{x:x for x in symbols_us}|{'G::'+x:x for x in symbols_row},
               'active_roster_names':300,'known_historical_symbols':len(cols),
               'dated_vintages_applied':len(snapshots),
               'first_roster_recorded_date':str(snapshots[0][0]),
               'last_benchmark_date':str(index[-1].date())}


def evaluate_model(m,provenance):
    if m.features.close_eur.index[-1]<START_PAPER:
        return None,{'status':'WAITING_FIRST_EXECUTION_QUOTES',
                     'reason':'First paper session 2026-11-10: no verified actual execution-session market bar yet; the preceding close signal is recorded',
                     'first_execution_session':str(START_PAPER.date())}
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
