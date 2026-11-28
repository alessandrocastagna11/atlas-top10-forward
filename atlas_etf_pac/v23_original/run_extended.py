#!/usr/bin/env python3
"""Atlas V2.3 ORIGINAL engine, extended historical 1926--2026 proxy research.

Unmodified original state machine is imported verbatim from pinned reference ZIP.
Original Fama/French returns are retained EXACTLY through 2025-12-31.
The 2026 section is explicitly stitched, never relabeled as original PIT data.
This runner is not the forward 4-ETF paper trading ledger; never touches it.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, hashlib, importlib.util, io, json, math
import os, re, sys, urllib.parse, urllib.request, zipfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
REFERENCE=HERE/'pinned_reference.zip'
EXTRACT=HERE/'reference'
OUT=HERE/'reports'
UA='Mozilla/5.0 Atlas-V23-Original-Proxy-Research/1.0'
TARGET='2026-11-11'
ORIG_END='2025-12-31'
FF_BASE='https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip'

def fetch(url, timeout=35):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:return r.read()

def check_reference():
    with zipfile.ZipFile(REFERENCE) as z:
        z.extractall(EXTRACT)
    engine=EXTRACT/'engine/atlas_v23_pac_stress.py'
    h=hashlib.sha256(engine.read_bytes()).hexdigest()
    expected='aef2809b8a3bfe01e3b0fa6b3f0d564b8ad0db90340a04a7480d7266d9141c7a'
    if h != expected:raise RuntimeError('Original V2.3 engine hash mismatch! '+h)
    return engine,h

def load_original_engine(source):
    spec=importlib.util.spec_from_file_location('atlas_v23_original_frozen',source)
    v=importlib.util.module_from_spec(spec);sys.modules[spec.name]=v;spec.loader.exec_module(v)
    return v

def stable_xirr_factory(v):
    def solve(flows):
        if not flows:return math.nan
        t0=v.parse_date(flows[0][0]);arr=[((v.parse_date(t)-t0).days/365.25,float(x)) for t,x in flows]
        if not any(x>0 for _,x in arr) or not any(x<0 for _,x in arr):return math.nan
        lo=-.90;hi=1.
        def fun(r):
            if r<=-1:return math.inf
            z=math.log1p(r)
            if any(-t*z>690 for t,_ in arr):return math.inf
            return math.fsum(x*math.exp(-t*z) for t,x in arr)
        flo=fun(lo)
        for _ in range(30):
            if flo*fun(hi)<=0:break
            hi=hi*2+1
        else:return math.nan
        for _ in range(250):
            mid=(lo+hi)/2;fm=fun(mid)
            if not math.isfinite(fm):lo=mid;continue
            if flo*fm<=0:hi=mid
            else:lo=mid;flo=fm
        return (lo+hi)/2
    return solve

def load_ff_csv(contents):
    output={}
    if isinstance(contents,bytes):contents=contents.decode('utf-8-sig','replace')
    for row in contents.splitlines():
        m=re.match(r'^\s*(\d{8}),\s*([+\-\d.]+),\s*([+\-\d.]+),\s*([+\-\d.]+),\s*([+\-\d.]+)',row)
        if not m:continue
        key=m.group(1);day=f'{key[:4]}-{key[4:6]}-{key[6:8]}'
        try:rf=float(m.group(5))/100;market=float(m.group(2))/100+rf
        except (ValueError,TypeError):continue
        if math.isfinite(market) and math.isfinite(rf):output[day]={'date':day,'ret':market,'rf':rf}
    return output

def latest_daily_official():
    try:
        raw=fetch(FF_BASE)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name=next(n for n in z.namelist() if n.lower().endswith('.csv'))
            rows=load_ff_csv(z.read(name))
        if not rows or max(rows)<='2026-05-29':raise ValueError('Official feed not more recent than pinned data')
        return rows,{'source':'Ken French daily CSV ZIP','url':FF_BASE,'sha256':hashlib.sha256(raw).hexdigest(),'max_session':max(rows),'status':'OK'}
    except Exception as exc:
        return {},{'source':'Ken French daily CSV ZIP','status':'UNAVAILABLE','reason':str(exc)[:150]}

def yahoo_adj(ticker,from_day,to_day):
    s=int(dt.datetime.fromisoformat(from_day).replace(tzinfo=dt.timezone.utc).timestamp())
    t=int((dt.datetime.fromisoformat(to_day)+dt.timedelta(days=3)).replace(tzinfo=dt.timezone.utc).timestamp())
    url=f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker,safe="")}?period1={s}&period2={t}&interval=1d&includeAdjustedClose=true&events=div%2Csplits'
    raw=fetch(url)
    data=json.loads(raw);res=data['chart']['result'][0]
    timestamps=res.get('timestamp') or []
    indicator=res['indicators'];adjs=indicator.get('adjclose',[{}])[0].get('adjclose',[])
    if not adjs:adjs=indicator['quote'][0]['close']
    out={}
    for x,p in zip(timestamps,adjs):
        if p is None or p<=0:continue
        date=dt.datetime.fromtimestamp(x,dt.timezone.utc).strftime('%Y-%m-%d')
        if date not in out:out[date]=float(p)
    return out,{'ticker':ticker,'provider':'Yahoo adjusted daily close', 'sha256':hashlib.sha256(raw).hexdigest(),'max_session':max(out) if out else None}

def build_extension(v,cutoff,online,allow_proxy=True):
    pinned=load_ff_csv((EXTRACT/'data/F-F_Research_Data_Factors_daily.csv').read_bytes())
    frozen={k:r for k,r in pinned.items() if k<=ORIG_END}
    if len(frozen)<20000:raise RuntimeError('Frozen historical FF data insufficient')
    extended={k:r for k,r in pinned.items() if ORIG_END<k<=cutoff}
    source_counts={'pinned_frozen_through_2025':len(frozen),'pinned_2026':len(extended)}
    audit={'official':{'status':'NOT_REQUESTED'},'market_proxy':{'status':'NOT_REQUESTED'}}
    if online and cutoff>max(pinned):
        latest,meta=latest_daily_official();audit['official']=meta
        # Only append strictly newer observations, NEVER overwrite pinned data.
        for k,r in sorted(latest.items()):
            if ORIG_END<k<=cutoff and k not in extended:extended[k]=r
        source_counts['official_new_sessions']=len([k for k in extended if k not in pinned])
    max_day=max(list(frozen)+list(extended))
    if cutoff>max_day and allow_proxy and online:
        spy,audit_spy=yahoo_adj('SPY',str((dt.date.fromisoformat(max_day)-dt.timedelta(days=14))),cutoff)
        audit['market_proxy']=audit_spy
        # need preceding price even if provider has gaps; retain only days after last FF.
        known=sorted((k,v) for k,v in spy.items() if k<=max_day)
        before=known[-1] if known else None
        if before is None:raise RuntimeError('SPY has no anchor price before FF end')
        prev=before[1];rf=extended[max_day]['rf'] if max_day in extended else frozen[max_day]['rf']
        extrap=[]
        for d,p in sorted(spy.items()):
            if d<=max_day:continue
            if d>cutoff:break
            extended[d]={'date':d,'ret':p/prev-1.,'rf':rf}
            prev=p;extrap.append(d)
        source_counts['SPY_proxy_sessions']=len(extrap)
        audit['proxy_start']=extrap[0] if extrap else None
        audit['proxy_last']=extrap[-1] if extrap else None
        audit['rf_approximation']='last available Fama French daily RF repeated for SPY extension'
    days=sorted(dict(frozen,**extended))
    if days[-1]<cutoff:raise RuntimeError('Cannot reach requested end date with verified observations. Latest='+days[-1])
    rows=[(frozen if k in frozen else extended)[k] for k in days if k<=cutoff]
    if rows[0]['date']!='1926-07-01':raise RuntimeError('First date differs from frozen data')
    if len(rows)<25000:raise RuntimeError('Expected century of daily data')
    first_proxy=audit.get('proxy_start')
    audit.update({'source_counts':source_counts,'last_session':rows[-1]['date'],'total_sessions':len(rows),'first_proxy_session':first_proxy})
    return rows,audit

def passive_bh(v,rows,prices,cfg):
    units=0.;basis=0.;stamp=0.;twr=1.;previous=None;navs=[]
    flows=[(rows[0]['date'],-cfg['initial_eur'])]
    slip=cfg['slippage_each_side'];fee0=cfg['strategy_fixed_fee_eur']
    for i,row in enumerate(rows):
        external=0.;amount=0.;fee=0.
        if i==0:amount=cfg['initial_eur'];fee=fee0
        elif row['date'][:7]!=rows[i-1]['date'][:7]:
            amount=cfg['monthly_pac_eur'];external=amount;flows.append((row['date'],-amount))
        if i:stamp+=units*prices[i]*cfg['annual_stamp_proxy']/252.
        if amount:
            gross=(amount-fee)/(1+slip)
            units+=gross/prices[i];basis+=gross
        net=units*prices[i]-stamp
        if previous is not None:twr*=1+(net-external)/previous-1
        previous=net;navs.append(net)
    gross_sale=units*prices[-1]*(1-slip)-fee0
    final=gross_sale-max(0.,gross_sale-basis)*cfg['tax_equity']-stamp
    flows.append((rows[-1]['date'],final))
    period=v.years_between(rows[0]['date'],rows[-1]['date'])
    return {'twr_cagr_net':twr**(1/period)-1.,'max_drawdown':v.max_dd([q for q in rolling_twr_from_nav(rows,navs,cfg)]),'xirr':v.xirr(flows),
            'final_liquidated_eur':final,'total_contributions_eur':cfg['initial_eur']+(sum(1 for i in range(1,len(rows)) if rows[i]['date'][:7]!=rows[i-1]['date'][:7]))*cfg['monthly_pac_eur']}

def rolling_twr_from_nav(rows,navs,cfg):
    val=1.;previous=None
    for i,n in enumerate(navs):
        external=cfg['monthly_pac_eur'] if i>0 and rows[i]['date'][:7]!=rows[i-1]['date'][:7] else 0.
        if previous is not None:val*=1+(n-external)/previous-1
        previous=n;yield val

def stress_override(v,scenario,scoremap):
    v.load_fred_monthly=lambda _filename:{}
    v.load_six_portfolios_monthly=lambda :{}
    # No artificial stress=0 before 2026; replay exactly pinned monthly scores.
    known_max=max(scoremap)
    last_score=int(scoremap[known_max])
    missing=set()
    def regime(self,i):
        month=v.previous_month_key(self.rows[i]['date'])
        score=scoremap.get(month)
        if score is None:
            missing.add(month)
            score= 0 if scenario=='neutral' else 2 if scenario=='stressed' else last_score
        return {'month':month,'score':int(score),'credit':False,'breadth':False,'rates':False,
               'credit_spread':math.nan,'credit_p80':math.nan,'credit_change_3m':math.nan,
               'breadth_healthy':math.nan,'term_spread':math.nan,'term_p20':math.nan}
    v.Simulator.stress_regime=regime
    return missing

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--start',default='1926-07-01')
    ap.add_argument('--end',default=TARGET);ap.add_argument('--no-online',action='store_true')
    ap.add_argument('--scenario',choices=('neutral','carry','stressed','all'),default='all')
    ap.add_argument('--out',default=str(OUT))
    args=ap.parse_args()
    engine_path,engine_sha=check_reference()
    v=load_original_engine(engine_path);v.xirr=stable_xirr_factory(v)
    cfg=json.loads((EXTRACT/'config.json').read_text())
    cfg['start_date']=args.start
    cfg['end_date']=args.end
    pinned=json.loads((EXTRACT/'data/stress_scores_precomputed.json').read_text())
    y,m=map(int,pinned['start'].split('-'));scores={}
    for val in pinned['scores']:
        scores[f'{y:04}-{m:02}']=int(val);m+=1
        if m==13:m=1;y+=1
    rows,coverage=build_extension(v,args.end,not args.no_online)
    v.load_ff=lambda start,end:[dict(q) for q in rows if start<=q['date']<=end]
    output=Path(args.out);output.mkdir(parents=True,exist_ok=True)
    runs={}
    for scenario in (('neutral','carry','stressed') if args.scenario=='all' else (args.scenario,)):
        missing=stress_override(v,scenario,scores)
        sim=v.Simulator(cfg,output)
        result,nav,flows=sim.run()
        benchmark=passive_bh(v,sim.rows,sim.price,cfg)
        # Save full nav (optional, for audit), orders, monthly state events only for carry scenario.
        if scenario=='carry' or args.scenario!='all':
            v.write_csv(output/'atlas_original_v23_nav_daily.csv',nav)
            v.write_csv(output/'atlas_original_v23_orders.csv',sim.events)
            v.write_csv(output/'atlas_original_v23_stress_events.csv',sim.stress_events)
        runs[scenario]={'strategy':result,'passive_same_ff_proxy':benchmark,'unknown_macro_months':sorted(missing),
                        'stress_events_2026':[x for x in sim.stress_events if x['date']>ORIG_END],
                        'benchmark_definition':'Passive holds identical original FF equity proxy; both include cost/slippage/taxes and stamp convention; final amounts net liquidation'}
    summary={'model':'Atlas V2.3 ORIGINAL FROZEN STATE MACHINE (SHA256 verified)',
             'engine_sha256':engine_sha,'start_date':cfg['start_date'],'requested_end_date':args.end,
             'historical_frozen_through':ORIG_END,'macro_pinned_last_month':max(scores),
             '2026_extension_not_exact_frozen':args.end>ORIG_END,
             'coverage':coverage,'scenarios':runs,'audit':{'frozen':None}}
    (output/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False,allow_nan=False,default=str))
    print(json.dumps({'date':args.end,'coverage':coverage,'scenarios':{k:{'original':{'cagr_pct':round(z['strategy']['twr_cagr_net']*100,4),
      'max_dd_pct':round(z['strategy']['max_drawdown']*100,4),'xirr_pct':round(z['strategy']['xirr']*100,4),'final_eur':round(z['strategy']['final_liquidated_eur'],2),'orders':z['strategy']['orders_total']},
      'passive':{'cagr_pct':round(z['passive_same_ff_proxy']['twr_cagr_net']*100,4),'max_dd_pct':round(z['passive_same_ff_proxy']['max_drawdown']*100,4),
      'xirr_pct':round(z['passive_same_ff_proxy']['xirr']*100,4),'final_eur':round(z['passive_same_ff_proxy']['final_liquidated_eur'],2)},
      'unknown_months':len(z['unknown_macro_months'])} for k,z in runs.items()}},indent=2))


def audit_reentry_ath(output):
    """Research only: native frozen engine and immutable real-ETF snapshot."""
    import ast, copy, subprocess, types
    engine_path, digest=check_reference()
    src=engine_path.read_text(encoding='utf-8')
    needle='if self.dd10[i]>=trough+c["satellite_rebound"] and self.dd5[i]>self.dd10[i]:p_sat="ENTRY"'
    assert src.count(needle)==1
    patched=src.replace(needle,'if (self.dd10[i]>=trough+c["satellite_rebound"] and self.dd5[i]>self.dd10[i]) or self.dd[i]>=-1e-12:p_sat="ENTRY"',1)
    def mod(name,source,file):
        v=types.ModuleType(name);v.__file__=str(file);sys.modules[name]=v
        exec(compile(source,str(file),'exec'),v.__dict__)
        return v
    native=load_original_engine(engine_path)
    fix=mod('atlas_v231_ath_native',patched,engine_path)
    cfg=json.loads((EXTRACT/'config.json').read_text())
    expected=json.loads((EXTRACT/'meta/expected_frozen_results.json').read_text())['expected']
    pinned=json.loads((EXTRACT/'data/stress_scores_precomputed.json').read_text())
    y,m=map(int,pinned['start'].split('-'));scores={}
    for s in pinned['scores']:
        scores[f'{y:04d}-{m:02d}']=int(s);m+=1
        if m==13:m=1;y+=1
    def run(v,c,data=None,bond=None,stress=None):
        v.xirr=stable_xirr_factory(v)
        stress_override(v,'carry',scores)
        if data is not None:v.load_ff=lambda start,end:[dict(q) for q in data if start<=q['date']<=end]
        if stress is not None:v.Simulator.stress_regime=lambda self,i:stress.regime(self.rows[i]['date'])
        sim=v.Simulator(copy.deepcopy(c),output)
        if bond is not None:sim.bond=[bond[q['date']] for q in sim.rows]
        z,nav,flows=sim.run()
        assert z['orders_total']==sum(z['order_breakdown'].values()) and all(q['nav_eur']>0 for q in nav)
        result={k:z[k] for k in ('start_date','end_date','twr_cagr_net','max_drawdown','xirr','final_liquidated_eur','orders_total','orders_strategy','orders_pac','realized_taxes_eur')}
        result.update(sessions=len(nav),sales=sum(q['side']=='SELL' for q in sim.events),
                      core_exits=[q['date'] for q in sim.events if q['note']=='core exit'])
        return result,z
    hist=[]
    for start in ('1926-07-01','1950-01-01','1980-01-01','2000-01-01','2016-01-04','2020-01-02'):
        c=dict(cfg,start_date=start,end_date='2025-12-31')
        a,z=run(native,c)
        if start=='1926-07-01':
            for k,want in expected.items():assert math.isclose(float(z[k]),float(want),rel_tol=1e-10,abs_tol=1e-6),(k,z[k],want)
        b,_=run(fix,c);pair={'start':start,'base':a,'fix':b};hist.append(pair)
        print('ATH_NATIVE_PAIR',json.dumps(pair,allow_nan=False),flush=True)
    (output/'reentry_ath_native.json').write_text(json.dumps(hist,indent=2,allow_nan=False))
    live_commit='58a747385159cd2b984912a36469e72b340037f6'
    def gitfile(p):return subprocess.check_output(['git','show',live_commit+':atlas_etf_pac/'+p])
    snapshot=output/'live_snapshot';directory=snapshot/'v23_original';directory.mkdir(parents=True,exist_ok=True)
    live_path=directory/'atlas_v23_live.py';live_bytes=gitfile('v23_original/atlas_v23_live.py')
    assert hashlib.sha256(live_bytes).hexdigest()=='703e4ecc9509ec8278ecd39e008cdc12f53091cdf8c833e3f7aadc4032cb196e'
    live_path.write_bytes(live_bytes);(directory/'pinned_reference.zip').write_bytes(gitfile('v23_original/pinned_reference.zip'))
    s=live_bytes.decode('utf-8')
    live_orig=mod('atlas_live_original_research',s,live_path)
    n='if dd10[i] >= trough + float(cfg["satellite_rebound"]) and dd5[i] > dd10[i]:'
    assert s.count(n)==1
    live_fix=mod('atlas_live_ath_research',s.replace(n,'if (dd10[i] >= trough + float(cfg["satellite_rebound"]) and dd5[i] > dd10[i]) or dd[i] >= -1e-12:',1),live_path)
    cfg_live=json.loads(gitfile('config.json'));macro=json.loads(gitfile('live_cache/macro_v23.json'))
    tree=ast.parse(gitfile('engine.py').decode('utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CausalStress')
    ns={'v23':live_orig,'ROOT':snapshot,'json':json,'zipfile':zipfile}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),'CausalStress_snapshot','exec'),ns)
    stress=ns['CausalStress'](cfg_live,macro)
    def prices(p):
        a=[{'date':q['date'],'close':float(q['close'])} for q in csv.DictReader(io.StringIO(gitfile(p).decode('utf-8-sig')))]
        assert all(q['close']>0 for q in a) and [q['date'] for q in a]==sorted(set(q['date'] for q in a))
        return a
    eq=prices('live_cache/yahoo_EUNL_DE.csv');bd=prices('live_cache/yahoo_CSBGE3_MI.csv')
    end=min(eq[-1]['date'],bd[-1]['date'],'2026-11-27');eq=[q for q in eq if q['date']<=end]
    assert eq[-1]['date']=='2026-11-27'
    bond={};j=-1
    for q in eq:
        while j+1<len(bd) and bd[j+1]['date']<=q['date']:j+=1
        assert j>=0
        bond[q['date']]=bd[j]['close']
    a=live_orig.replay_strategy(eq,cfg_live,stress);b=live_fix.replay_strategy(eq,cfg_live,stress)
    signals={'last':end,'sessions':len(eq),'base':a.latest,'fix':b.latest,
             'different_sessions':sum((x['core_in'],x['satellite_in'])!=(y['core_in'],y['satellite_in']) for x,y in zip(a.history,b.history))}
    print('ATH_LIVE_SIGNAL',json.dumps(signals,allow_nan=False),flush=True)
    data=[{'date':q['date'],'ret':q['close']/eq[i-1]['close']-1 if i else 0.,'rf':0.} for i,q in enumerate(eq)]
    modern=[]
    for start in ('2010-01-04','2016-01-04','2020-01-02','2022-01-03'):
        c=dict(cfg,start_date=start,end_date=end)
        a,_=run(native,c,data,bond,stress);b,_=run(fix,c,data,bond,stress)
        pair={'start':start,'base':a,'fix':b};modern.append(pair)
        print('ATH_REAL_ETF_PAIR',json.dumps(pair,allow_nan=False),flush=True)
    report={'native_parity':True,'native_source_sha256':digest,'fix_source_sha256':hashlib.sha256(patched.encode()).hexdigest(),
            'native':hist,'real_etf':modern,'live_signal':signals,'live_commit':live_commit,
            'assumptions':'Native TWR before final liquidation tax; XIRR includes final tax. ETF adjusted close, zero interest on cash, causal previous-month macro with explicit carry; each financial window cold start. No paper writes.'}
    (output/'reentry_ath_audit.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    print('ATH_VALIDATION_COMPLETE',flush=True)

if __name__=='__main__':
    main()
    if '--no-online' in sys.argv and '--end' in sys.argv and sys.argv[sys.argv.index('--end')+1]=='2025-12-31' and '--out' in sys.argv:
        audit_reentry_ath(Path(sys.argv[sys.argv.index('--out')+1]))
