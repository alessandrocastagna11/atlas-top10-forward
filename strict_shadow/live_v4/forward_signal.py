#!/usr/bin/env python3
"""Forward research signals for Atlas V4-style Confidence70. NOT frozen engine parity.

No broker orders, no stale/undated rosters, no backfilled simulated decisions.
Uses current-known ACWI holdings as a cohort proxy, not PIT market-cap rankings.
"""
from __future__ import annotations
import bisect,csv,datetime as dt,hashlib,json,math,statistics
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from dataset_daily import DATA,rows_load,symbol_filename,atomic,json_save
from paper import ENGINE
ROOT=Path(__file__).resolve().parent
FORWARD=ROOT/'forward'
NY=ZoneInfo('America/New_York')
MIN_OBSERVATIONS=285
VERSION='V4_CONF70_FORWARD_COHORT_PROXY_V1'
FIELDS=['ticker','market_symbol','region','sector','score','raw12','res12','vol63','dollar_volume_20','weight','price_eur']

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def safe_float(x,default=math.nan):
    try:
        v=float(x);return v if math.isfinite(v) else default
    except (ValueError,TypeError):return default
def median(v):return statistics.median(v) if v else math.nan
def robust_z(values):
    if not values:return {}
    xs=list(values.values());m=median(xs);mad=median([abs(v-m) for v in xs]);den=max(1.4826*mad,0.05)
    return {k:max(-3.,min(3.,(v-m)/den)) for k,v in values.items()}
def next_weekday(session):
    x=session+dt.timedelta(days=1)
    while x.weekday()>=5:x+=dt.timedelta(days=1)
    return x
def latest_roster(decision):
    root=DATA/'universe/snapshots';found=[]
    for p in root.glob('*/source.json'):
        try:
            recorded=dt.date.fromisoformat(p.parent.name)
            meta=json.loads(p.read_text())
            source=dt.datetime.strptime(meta['source_as_of'],'%b %d, %Y').date()
            if recorded<=decision and source<=decision and (decision-source).days<=15:
                rows=rows_load(p.parent/'global500.csv')
                if len(rows)==500:found.append((recorded,source,p,rows,meta))
        except (ValueError,KeyError,OSError):continue
    if not found:raise ValueError('No 500-member roster recorded and available by decision session')
    rec,source,p,rows,meta=max(found,key=lambda x:(x[1],x[0]))
    if meta.get('type')!='ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP':
        raise ValueError('Unrecognized forward universe vintage')
    return rows,{'recorded_on':str(rec),'asof':str(source),'sha256':digest(p.parent/'global500.csv'),
                 'provider':'ISHARES_ACWI','type':meta['type']}
def prices(symbol,asof):
    p=DATA/'prices'/symbol_filename(symbol)
    if not p.exists():return [],None
    series=[]
    for r in rows_load(p):
        try:
            day=dt.date.fromisoformat(r['date'])
            v=safe_float(r['close']);op=safe_float(r['open']);vol=safe_float(r.get('volume'))
            scale=safe_float(r.get('unit_scale',1.0),1.)
            if day<=asof and v>0 and op>0 and scale>0:
                series.append((day,v*scale,op*scale,vol,str(r['currency']).upper()))
        except (KeyError,ValueError):continue
    series.sort(key=lambda x:x[0])
    return series,digest(p)
def moving_mean(values,n):return sum(values[-n:])/n
def log_return(a,b):return math.log(a/b) if a>0 and b>0 else math.nan
def annual_vol(prices,n=63):
    if len(prices)<n+1:return math.nan
    vals=[math.log(prices[i]/prices[i-1]) for i in range(len(prices)-n,len(prices))]
    return statistics.stdev(vals)*math.sqrt(252) if len(vals)>1 else math.nan
def fx_close(currency,day,cache):
    if currency=='EUR':return 1.
    if currency not in cache:return math.nan
    keys,values=cache[currency]
    i=bisect.bisect_right(keys,day)-1
    if i<0 or (day-keys[i]).days>5:return math.nan
    return values[i] if values[i]>0 else math.nan
def fx_cache():
    currencies=set()
    file=DATA/'universe/global500_latest.csv'
    for r in rows_load(file):currencies.add(r.get('currency',''))
    fx={};hashes={}
    for curr in currencies:
        if curr=='EUR':continue
        code='EUR'+curr+'=X'
        file=DATA/'fx'/symbol_filename(code)
        if not file.exists():continue
        pairs=[]
        for r in rows_load(file):
            try:
                d=dt.date.fromisoformat(r['date']);v=float(r['close'])
                if v>0:pairs.append((d,v))
            except (KeyError,ValueError):continue
        pairs.sort()
        fx[curr]=([p[0] for p in pairs],[p[1] for p in pairs])
        hashes[code]=digest(file)
    return fx,hashes
def feature_pack(sym,row,decision,fx):
    series,price_sha=prices(sym,decision)
    if len(series)<MIN_OBSERVATIONS:return None
    # Avoid delayed/stale symbols and lookahead.
    if (decision-series[-1][0]).days>5:return None
    last=series[-MIN_OBSERVATIONS:]
    close=[x[1] for x in last]; dates=[x[0] for x in last]
    if any(not math.isfinite(x) or x<=0 for x in close):return None
    if not (close[-1]>moving_mean(close,200) and moving_mean(close,50)>moving_mean(close,200)):
        return None
    raw12=log_return(close[-22],close[-253])
    raw6=log_return(close[-22],close[-127])
    raw3=log_return(close[-22],close[-64])
    if not all(math.isfinite(x) for x in [raw12,raw6,raw3]) or raw12<=0:return None
    vol=annual_vol(close)
    if not math.isfinite(vol) or vol<0.02:return None
    trend=math.log(close[-1]/moving_mean(close,200))/vol
    r5=math.log(close[-1]/close[-6])
    changes=[math.log(close[i]/close[i-1]) for i in range(len(close)-21,len(close))]
    v20=statistics.stdev(changes)
    extension=r5/(v20*math.sqrt(5)) if v20>0 else math.inf
    if extension>3.0 or extension< -3.0:return None
    # Rolling 20-session liquidity: median local price * volume, converted to EUR at each close.
    eurovals=[]
    currency=last[-1][4]
    for day,p,_,v,c in last[-20:]:
        fxrate=fx_close(c,day,fx)
        if math.isfinite(v) and v>0 and math.isfinite(fxrate):eurovals.append(p*v/fxrate)
    mdv=median(eurovals)
    if len(eurovals)<15 or not math.isfinite(mdv) or mdv<1_000_000:return None
    eur=close[-1]/fx_close(currency,dates[-1],fx)
    if not math.isfinite(eur) or eur<=1:return None
    returns={dates[i]:math.log(close[i]/close[i-1]) for i in range(1,len(close))}
    return {'ticker':sym,'market_symbol':sym,'country':row['country'],
            'region':'USA' if row['country']=='United States' else 'ROW','sector':row['sector'],
            'currency':currency,'raw12':raw12,'raw6':raw6,'raw3':raw3,
            'vol63':vol,'trend':trend,'extension':extension,'dollar_volume_20':mdv,
            'price_eur':eur,'date':str(dates[-1]),'returns':returns,'price_sha256':price_sha}
def residual_momentum(asset,sector_daily,market_daily,lag=21,window=231):
    dates=sorted(asset['returns'])
    if len(dates)<lag+window:return math.nan
    selected=dates[-(lag+window):-lag]
    vals=[]
    for day in selected:
        m=market_daily.get(day);s=sector_daily.get(day)
        if m is None or s is None:continue
        # Explicit factor-residual approximation (NOT original two-factor OLS).
        vals.append(asset['returns'][day]-0.5*m-0.5*s)
    if len(vals)<int(window*.70):return math.nan
    return sum(vals)
def group_score(features):
    if len(features)<6:return []
    daily_m=defaultdict(list);daily_sector=defaultdict(list)
    for f in features:
        for d,r in f['returns'].items():
            daily_m[d].append(r);daily_sector[(d,f['sector'])].append(r)
    mm={k:sum(v)/len(v) for k,v in daily_m.items()}
    ss={k:sum(v)/len(v) for k,v in daily_sector.items()}
    for f in features:
        sec={d:ss[(d,f['sector'])] for d in f['returns'] if (d,f['sector']) in ss}
        f['res12']=residual_momentum(f,sec,mm,21,231)
        f['res6']=residual_momentum(f,sec,mm,21,105)
    names=['res12','res6','raw12','raw6','raw3','trend','vol63']
    zs={k:robust_z({f['ticker']:(-f[k] if k=='vol63' else f[k]) for f in features if math.isfinite(f[k])}) for k in names}
    weights={'res12':.35,'res6':.20,'raw12':.15,'raw6':.10,'raw3':.05,'trend':.075,'vol63':.075}
    out=[]
    for f in features:
        if not all(f['ticker'] in zs[k] for k in weights):continue
        f['score']=sum(weights[k]*zs[k][f['ticker']] for k in weights)
        out.append(f)
    return sorted(out,key=lambda f:f['score'],reverse=True)
def cohort_strength(usa,row):
    """Current-known cohort momentum: no claim of a PIT historical index."""
    if len(usa)<5 or len(row)<5:return 0.
    def average(f,key):return statistics.mean(x[key] for x in f if math.isfinite(x[key]))
    u12=average(usa,'raw12');r12=average(row,'raw12')
    u6=average(usa,'raw6');r6=average(row,'raw6')
    delta=.6*(u12-r12)+.4*(u6-r6)
    divisor=max(.05,(statistics.median(x['vol63'] for x in usa+row)/math.sqrt(252))*math.sqrt(126))
    return max(-2.5,min(2.5,delta/divisor))
def weighted_allocation(chosen,exposure,beta=.70):
    if not chosen or exposure<=0:return {}
    names=[f['ticker'] for f in chosen]
    order=sorted(range(len(chosen)),key=lambda i:chosen[i]['score'],reverse=True)
    rank_strength={names[i]:1.-(n/max(1,len(chosen)-1)) for n,i in enumerate(order)}
    raw={f['ticker']:(1/max(.12,f['vol63']))*math.exp(beta*(2*rank_strength[f['ticker']]-1)) for f in chosen}
    targets={x:0. for x in names};sects={}
    # Iterative water-filling respecting 15% issuer and 30% sector limits.
    remaining=exposure
    for _ in range(30):
        eligible=[f for f in chosen if targets[f['ticker']]<.15-1e-9 and sects.get(f['sector'],0.)<.30-1e-9]
        if not eligible or remaining<1e-7:break
        denom=sum(raw[f['ticker']] for f in eligible)
        if denom<=0:break
        applied=0.
        for f in eligible:
            t=f['ticker'];sector=f['sector'];want=remaining*raw[t]/denom
            # Sector cap may be filled by a peer in this very pass; update as we go.
            add=max(0.,min(want,.15-targets[t],.30-sects.get(sector,0.)))
            targets[t]+=add;sects[sector]=sects.get(sector,0.)+add;applied+=add
        remaining-=applied
        if applied<1e-9:break
    return {k:math.floor(v*1e8)/1e8 for k,v in targets.items() if v>=.005}
def generate(now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:raise ValueError('timezone required')
    ny=now.astimezone(NY)
    status={'checked_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
            'model_variant':VERSION,'mode':'RESEARCH_PROXY_PAPER_ONLY',
            'historical_model_parity':'NOT_CLAIMED','real_broker_orders':0}
    try:
        acwi,bench_sha=prices('ACWI',ny.date())
        if not acwi:raise ValueError('ACWI benchmark history missing')
        decision=max(x[0] for x in acwi)
        if decision==ny.date() and (ny.hour,ny.minute)<(17,30):
            raise ValueError('US session not closed yet; no forward signal')
        if (ny.date()-decision).days>5:raise ValueError('Stale benchmark close')
        status['decision_session']=str(decision)
        if (decision-acwi[0][0]).days<365:raise ValueError('Insufficient benchmark warm-up')
        roster,rm=latest_roster(decision)
        fx,fxhash=fx_cache()
        if len(fx)<10:raise ValueError('FX panel incomplete for forward universe')
        us_rows=[r for r in roster if r['country']=='United States'][:100]
        rw_rows=[r for r in roster if r['country']!='United States'][:200]
        if len(us_rows)<90 or len(rw_rows)<90:raise ValueError('Forward universe missing USA/ROW breadth')
        by_symbol={}
        pricehashes={}
        for r in us_rows+rw_rows:
            symbol=r['symbol']
            if symbol in by_symbol:continue
            f=feature_pack(symbol,r,decision,fx)
            if f:
                by_symbol[symbol]=f;pricehashes[symbol]=f['price_sha256']
        fs=list(by_symbol.values());usa=[f for f in fs if f['region']=='USA'];row=[f for f in fs if f['region']=='ROW']
        status['eligible_feature_history']={'usa':len(usa),'row':len(row),'universe_candidates':len(us_rows)+len(rw_rows)}
        if len(usa)<60 or len(row)<40:raise ValueError('Insufficient verified 252-session+volume history: USA '+str(len(usa))+' / ROW '+str(len(row)))
        scored=group_score(usa)+group_score(row)
        top_usa=sorted((f for f in scored if f['region']=='USA'),key=lambda x:x['score'],reverse=True)
        top_row=sorted((f for f in scored if f['region']=='ROW'),key=lambda x:x['score'],reverse=True)
        strength=cohort_strength(usa,row)
        row_slots=0 if strength>=0 else 2 if strength>=-.5 else 4 if strength>=-1 else 6
        desired={'USA':10-row_slots,'ROW':row_slots}
        picks=[];sec={}
        def add(f):
            if f['ticker'] in [p['ticker'] for p in picks] or len(picks)>=10 or sec.get(f['sector'],0)>=3:return False
            picks.append(f);sec[f['sector']]=sec.get(f['sector'],0)+1;return True
        for region,pool in [('USA',top_usa),('ROW',top_row)]:
            count=0
            for f in pool:
                if count>=desired[region]:break
                if add(f):count+=1
        if len(picks)<10:
            for f in sorted(top_usa+top_row,key=lambda x:x['score'],reverse=True):
                if len(picks)>=10:break
                add(f)
        if len(picks)<5:raise ValueError('Too few eligible selections after sector and region checks')
        ben_closes=[x[1] for x in acwi]
        if len(ben_closes)<201:raise ValueError('ACWI 200-day regime unavailable')
        risk_on=ben_closes[-1]>moving_mean(ben_closes,200) and moving_mean(ben_closes,50)>moving_mean(ben_closes,200)
        exposure=.80 if risk_on else .30
        # This exposure schedule is an explicit RESEARCH proxy, not the frozen path simulator.
        weights=weighted_allocation(picks,exposure)
        if not weights:raise ValueError('Optimizer returned no weights')
        targets=[{'ticker':f['ticker'],'market_symbol':f['market_symbol'],
                  'region':f['region'],'sector':f['sector'],'weight':weights[f['ticker']],
                  'price_eur':round(f['price_eur'],6),'score':round(f['score'],8)}
                 for f in picks if f['ticker'] in weights]
        # Check all source vintage and hashes before publishing any candidate.
        sources={'price_files_sha256':hashlib.sha256(json.dumps(pricehashes,sort_keys=True).encode()).hexdigest(),
                 'fx_files_sha256':hashlib.sha256(json.dumps(fxhash,sort_keys=True).encode()).hexdigest(),
                 'roster_sha256':rm['sha256'],'benchmark_price_file_sha256':bench_sha}
        signal={'schema_version':'research-forward-1','engine_id':ENGINE,
                'model_variant':VERSION,'historical_engine_equivalent':False,
                'mode':'SHADOW','decision_session':str(decision),'execution_session':str(next_weekday(decision)),
                'risk_regime':'RISK_ON' if risk_on else 'DEFENSIVE_PROXY',
                'target_exposure':sum(x['weight'] for x in targets),
                'region_strength_proxy':round(strength,6),'row_slots':row_slots,
                'universe_source':rm,'feature_source':'CURRENT_COHORT_H4_APPROX_NOT_ORIGINAL_OLS',
                'provenance_sha256':sources,'authorization':{'real_money_authorized':False},
                'targets':targets}
        status.update({'status':'RESEARCH_SIGNAL_READY','signal_session':str(decision),
                       'num_targets':len(targets),'target_exposure_pct':round(signal['target_exposure']*100,2),
                       'roster_date':rm['asof'],'notes':'Research proxy; not frozen H4 V4 Confidence70 exact engine'})
        dest=FORWARD/'latest_signal.json';json_save(dest,signal)
        # Preserve all daily signal versions; never rewrite a previous date.
        dated=FORWARD/'history'/(str(decision)+'.json')
        if not dated.exists():json_save(dated,signal)
        # Paper execution reads this pending signal; don't overwrite an unexecuted order.
        pending=FORWARD/'pending_signal.json'
        if not pending.exists():json_save(pending,signal)
        else:
            prior=json.loads(pending.read_text())
            status['pending_execution_session']=prior.get('execution_session')
        scorepath=FORWARD/'scores_latest.csv'
        fields=['ticker','market_symbol','region','sector','score','raw12','res12','vol63','dollar_volume_20','price_eur']
        with __import__('io').StringIO() as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
            for x in sorted(scored,key=lambda a:a['score'],reverse=True):
                w.writerow({k:(round(x[k],8) if isinstance(x[k],float) else x[k]) for k in fields})
            atomic(scorepath,f.getvalue().encode())
    except (ValueError,OSError,KeyError,IndexError,ZeroDivisionError,OverflowError) as exc:
        status.update({'status':'PENDING_FORWARD_DATA','reason':str(exc)[:350]})
    json_save(FORWARD/'latest_status.json',status)
    lines=['# Atlas V4 Confidence70 — Forward research proxy','',
           '**Status: '+status['status']+'**','',
           'Historical frozen V4 engine parity: NOT CLAIMED.','',
           'This is a current-cohort research selection, never a certified PIT global market-cap ranking.',
           'No broker access. Original exact V4 paper account is unchanged.','',
           'Latest decision session: '+str(status.get('decision_session','n/a')),
           'Targets: '+str(status.get('num_targets',0)),
           'Exposure: '+str(status.get('target_exposure_pct',0))+'%',
           'Issue: '+str(status.get('reason','none'))]
    atomic(FORWARD/'latest.md',('\n'.join(lines)+'\n').encode())
    print(json.dumps(status,ensure_ascii=False))
    return status

if __name__=='__main__':generate()
