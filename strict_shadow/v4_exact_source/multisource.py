"""Optional independent EOD audit. Never mutates model frames or persists vendor prices.
Credentials are opt-in, private and only used for bounded verification requests.
"""
from __future__ import annotations
import csv,datetime as dt,json,math,os,re,time,urllib.request,urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

HERE=Path(__file__).resolve().parent
ECB=HERE.parent/'live_v4'/'dataset'/'fx_ecb'
KEYS={'massive':'MASSIVE_API_KEY','twelve':'TWELVE_DATA_API_KEY',
      'eodhd':'EODHD_API_KEY','fmp':'FMP_API_KEY'}
EOD_MAP={'.DE':'XETRA','.PA':'PA','.AS':'AS','.L':'LSE','.MI':'MI',
         '.MC':'MC','.ST':'ST','.CO':'CO','.HE':'HE','.OL':'OL','.TO':'TO'}
US=re.compile(r'^[A-Z][A-Z0-9-]{0,9}$')
FX=re.compile(r'^EUR([A-Z]{3})=X$')

def positive(v):
    try:x=float(v)
    except (TypeError,ValueError):return None
    return x if math.isfinite(x) and x>0 else None

def exact_close(frame,day,column='raw_close'):
    if frame is None or column not in frame:return None
    try:return positive(frame.loc[pd.Timestamp(day),column])
    except (KeyError,TypeError,ValueError):return None

def compare(primary,alternative,tolerance):
    a,b=positive(primary),positive(alternative)
    if b is None:return 'NO_ALTERNATIVE'
    if a is None:return 'ALTERNATIVE_AVAILABLE_PRIMARY_MISSING'
    return 'AGREES' if abs(a/b-1)<=tolerance else 'DIFFERENT_REVIEW'

def get_json(url,params):
    # Never include URL, key, response text or exception message in published audit.
    req=urllib.request.Request(url+'?'+urllib.parse.urlencode(params),
        headers={'Accept':'application/json','User-Agent':'AtlasPaperAudit/1.0'})
    with urllib.request.urlopen(req,timeout=10) as f:raw=f.read(8_000_001)
    if len(raw)>8_000_000:raise ValueError('oversize')
    return json.loads(raw)

def exact_row(rows,day,date_field='date',price_field='close'):
    if not isinstance(rows,list):return None
    matches=[r for r in rows if isinstance(r,dict) and str(r.get(date_field,''))==str(day)]
    return positive(matches[0].get(price_field)) if len(matches)==1 else None

def eod_symbol(symbol):
    for suffix,exchange in EOD_MAP.items():
        if symbol.endswith(suffix):return symbol[:-len(suffix)]+'.'+exchange
    return symbol+'.US' if US.fullmatch(symbol) else None

def ecb_reference(currency,day,folder=ECB):
    file=folder/(currency+'.csv')
    if not file.exists():return None
    with file.open(encoding='utf-8',newline='') as f:
        rows=list(csv.DictReader(f))
    return exact_row(rows,day,price_field='units_per_eur')

def audit(frames,active,fx_symbols,asof,expected,now,missing=(),getter=get_json,ecb_folder=ECB):
    day=str(asof)
    keys={p:os.environ.get(k,'').strip() for p,k in KEYS.items()}
    enabled=os.environ.get('ATLAS_MULTISOURCE_OPT_IN')=='1' and os.environ.get('ATLAS_EXACT_OFFLINE')!='1'
    # Optional provider access requires explicit license/usage opt-in as well as credentials.
    result={'schema':1,'decision_session':day,'recorded_at_utc':now.isoformat(),
            'policy':'AUDIT_ONLY_NO_SUBSTITUTION_NO_VENDOR_PRICE_PUBLICATION',
            'provider_opt_in':enabled,'providers':{},'primary_missing':sorted(set(missing))}
    def summary(provider,configured):
        s={'configured':configured,'requests':0,'counts':{},'review_symbols':[],
           'available_when_primary_missing':[],'errors':{}}
        result['providers'][provider]=s
        return s
    def add(s,symbol,primary,alternative,tol):
        state=compare(primary,alternative,tol)
        s['counts'][state]=s['counts'].get(state,0)+1
        if state=='DIFFERENT_REVIEW':s['review_symbols'].append(symbol)
        if state=='ALTERNATIVE_AVAILABLE_PRIMARY_MISSING':s['available_when_primary_missing'].append(symbol)
    def request(s,url,params):
        s['requests']+=1
        try:return getter(url,params)
        except Exception as exc:
            code=getattr(exc,'code',None)
            label='HTTP_'+str(code) if isinstance(code,int) else type(exc).__name__
            s['errors'][label]=s['errors'].get(label,0)+1
            return None
    # ECB fixing is an earlier reference, not the New York FX close.
    s=summary('ecb_reference',True)
    for symbol in sorted(set(fx_symbols)):
        m=FX.fullmatch(symbol)
        if not m:continue
        try:alt=ecb_reference(m.group(1),day,ecb_folder)
        except (OSError,ValueError):alt=None
        add(s,symbol,exact_close(frames.get(symbol),day,'adj_close'),alt,.02)
    s['basis']='SAME_DATE_ECB_FIXING_VS_NY_CLOSE_DIAGNOSTIC_2_PERCENT'
    missing=set(missing)
    active=sorted(set(active))
    # Missing primary first; deterministic rotating checks otherwise.
    def prioritize(symbols,cap):
        symbols=sorted(set(symbols))
        bad=[x for x in symbols if x in missing]
        rest=[x for x in symbols if x not in missing]
        offset=asof.toordinal()%len(rest) if rest else 0
        return (bad+rest[offset:]+rest[:offset])[:cap]
    us=[x for x in active if US.fullmatch(x) and str(expected.get(x,day))==day]
    s=summary('massive',bool(keys['massive']))
    s['basis']='US_GROUPED_SPLIT_UNADJUSTED_CLOSE_DIAGNOSTIC_1_PERCENT'
    if enabled and keys['massive'] and asof<now.astimezone(ZoneInfo('America/New_York')).date():
        data=request(s,'https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/'+day,
                     {'adjusted':'false','include_otc':'false','apiKey':keys['massive']})
        if isinstance(data,dict) and data.get('status') in ('OK','DELAYED'):
            rows={r.get('T'):r for r in data.get('results',[]) if isinstance(r,dict)}
            for symbol in us:
                # The returned aggregate timestamp must refer to the requested NY session.
                row=rows.get(symbol.replace('-','.'),rows.get(symbol,{}))
                stamp=row.get('t')
                try:valid=dt.datetime.fromtimestamp(float(stamp)/1000,dt.timezone.utc).astimezone(ZoneInfo('America/New_York')).date()==asof
                except (TypeError,ValueError,OverflowError,OSError):valid=False
                add(s,symbol,exact_close(frames.get(symbol),day),row.get('c') if valid else None,.01)
    s=summary('twelve',bool(keys['twelve']))
    s['basis']='US_RAW_DAILY_CLOSE_AND_FOREX_REFERENCE_DIAGNOSTIC'
    if enabled and keys['twelve']:
        symbols=prioritize(us+list(fx_symbols),5)
        for symbol in symbols:
            m=FX.fullmatch(symbol)
            vendor='EUR/'+m.group(1) if m else symbol
            data=request(s,'https://api.twelvedata.com/time_series',
                {'symbol':vendor,'interval':'1day','start_date':day,'end_date':day,
                 'outputsize':1,'adjust':'none','apikey':keys['twelve']})
            meta=data.get('meta',{}) if isinstance(data,dict) else {}
            valid=meta.get('symbol')==vendor and (bool(m) or meta.get('currency')=='USD')
            alt=exact_row(data.get('values'),day,'datetime') if valid else None
            add(s,symbol,exact_close(frames.get(symbol),day,'adj_close' if m else 'raw_close'),alt,.02 if m else .01)
    s=summary('eodhd',bool(keys['eodhd']))
    s['basis']='EXPLICIT_VENUE_MAPPING_RAW_EOD_DIAGNOSTIC_1_PERCENT'
    if enabled and keys['eodhd']:
        # 4 calls per run; 3 scheduled runs <=12/day, manual runs share provider quota.
        for symbol in prioritize([x for x in active if eod_symbol(x) and x not in us and x in expected],4):
            target=str(expected[symbol])
            data=request(s,'https://eodhd.com/api/eod/'+urllib.parse.quote(eod_symbol(symbol),safe=''),
                {'from':target,'to':target,'fmt':'json','api_token':keys['eodhd']})
            add(s,symbol,exact_close(frames.get(symbol),target),exact_row(data,target),.01)
    s=summary('fmp',bool(keys['fmp']))
    s['basis']='US_RAW_EOD_DIAGNOSTIC_1_PERCENT'
    if enabled and keys['fmp']:
        for symbol in prioritize(us,4):
            data=request(s,'https://financialmodelingprep.com/stable/historical-price-eod/full',
                {'symbol':symbol,'from':day,'to':day,'apikey':keys['fmp']})
            add(s,symbol,exact_close(frames.get(symbol),day),exact_row(data,day),.01)
    for name,s in result['providers'].items():
        s['status']=('NOT_ENABLED' if name!='ecb_reference' and not enabled else
                     'NOT_CONFIGURED' if not s['configured'] else
                     'CHECKED' if sum(s['counts'].values()) else 'NO_COMPARISONS')
    result['independent_agreements']=sum(s['counts'].get('AGREES',0) for s in result['providers'].values())
    result['independent_disagreements']=sum(s['counts'].get('DIFFERENT_REVIEW',0) for s in result['providers'].values())
    result['alternative_available_for_missing']=sorted({x for s in result['providers'].values()
                                                        for x in s['available_when_primary_missing']})
    result['status']='REVIEW_REQUIRED' if result['independent_disagreements'] else (
        'AUDITED' if result['independent_agreements'] else 'NO_INDEPENDENT_MATCH')
    return result
