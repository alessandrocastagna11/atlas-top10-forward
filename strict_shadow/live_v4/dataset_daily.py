#!/usr/bin/env python3
"""Daily rolling public-source research dataset for Atlas V4 PAPER.
Historical PIT snapshots must not be fabricated; Yahoo data are NOT proof of PIT.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,hashlib,io,json,os,re,time,urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo
from provider import YahooChart,DataUnavailable
from market_sessions import is_final_bar,release_time_utc,venue

ROOT=Path(__file__).resolve().parent
LIST=ROOT.parent/'acquisition/input/global200_symbols_recovered.csv'
DATA=ROOT/'dataset'
# Pair names match the valuation adapter: EUR -> local currency; SAR falls back through USDSAR.
FX=['EURUSD=X','EURJPY=X','EURGBP=X','EURCHF=X','EURCAD=X','EURAUD=X',
    'EURDKK=X','EURHKD=X','EURINR=X','EURKRW=X','USDSAR=X','EURSEK=X',
    'EURSGD=X','EURTWD=X','EURNOK=X','EURBRL=X','EURZAR=X']

ISHARES='https://www.ishares.com/us/products/239600/ishares-msci-acwi-etf/latest-holdings.csv'
NY=ZoneInfo('America/New_York')

def sha(data):return hashlib.sha256(data).hexdigest()
def symbol_filename(sym):
    return re.sub(r'[^A-Za-z0-9._-]+','_',sym)+'_'+sha(sym.encode())[:8]+'.csv'
def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.new')
    tmp.write_bytes(data)
    os.replace(tmp,path)
def json_save(path,obj):atomic(path,(json.dumps(obj,indent=2,sort_keys=True,ensure_ascii=False)+'\n').encode())
def rows_load(path):
    if not path.is_file():return []
    with path.open(newline='',encoding='utf-8') as f:return list(csv.DictReader(f))
def csv_write(path,rows,fields):
    f=io.StringIO();w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    atomic(path,f.getvalue().encode('utf-8'))
def terminal_symbols(today):
    """A verified delisting means no NEW session exists, not a made-up close."""
    path=DATA/'corporate_actions/known_events.json'
    if not path.exists():return set()
    raw=json.loads(path.read_text(encoding='utf-8'))
    if raw.get('schema_version')!=1:raise ValueError('unsupported corporate action registry')
    terminal=set()
    for item in raw.get('events',[]):
        if item.get('classification')!='TERMINAL_NO_FUTURE_DAILY_BARS':continue
        if not item.get('source_url') or not item.get('last_trading_date'):raise ValueError('missing corporate-action evidence')
        if dt.date.fromisoformat(item['market_effective_date'])<=today:
            terminal.add(item['symbol'])
    return terminal

def old_symbols():
    rows=rows_load(LIST);symbols=list(dict.fromkeys(str(x.get('symbol','')).strip() for x in rows))
    if len(symbols)!=288 or any(not x for x in symbols):raise ValueError('Original recovered 288-symbol inventory missing or modified')
    return symbols

def market_date():
    return dt.datetime.now(dt.timezone.utc).astimezone(NY).date()
def parse_num(v):
    try:return float(str(v).replace(',','').strip().replace('"',''))
    except (ValueError,TypeError):return 0.

def derive(t,exchange,country):
    t=str(t).strip().replace(' ','-').rstrip('.')
    ex=str(exchange).lower();country=str(country).lower()
    if not t or t=='-' or t.lower()=='nan':return ''
    if ('nasdaq' in ex or 'nyse' in ex) and ('united states' in country or country=='us'):return t
    routes=[(('tokyo',),'.T'),(('hong kong',),'.HK'),(('korea',),'.KS'),(('taiwan',),'.TW'),
            (('london',),'.L'),(('swiss','six'),'.SW'),(('toronto',),'.TO'),
            (('amsterdam',),'.AS'),(('paris',),'.PA'),(('xetra','frankfurt'),'.DE'),
            (('australia','asx'),'.AX'),(('bombay',),'.BO'),(('stockholm',),'.ST'),
            (('copenhagen',),'.CO'),(('oslo',),'.OL'),(('helsinki',),'.HE'),
            (('milan','borsa italiana'),'.MI'),(('madrid',),'.MC'),(('singapore',),'.SI')]
    if 'national stock exchange' in ex and 'india' in country:return t+'.NS'
    if 'saudi' in ex:return t+'.SR'
    for keys,suffix in routes:
        if any(x in ex for x in keys):
            if suffix=='.HK':t=''.join(c for c in t if c.isdigit()).zfill(4)
            if suffix=='.KS':t=t.zfill(6)
            return t+suffix
    return '' # Ambiguous mapping: fail closed instead of inventing a listing.

def universe_snapshot(today):
    """iShares ETF holdings are a dated cross-section, NOT market cap or USA PIT universe."""
    req=urllib.request.Request(ISHARES,headers={'User-Agent':'Mozilla/5.0','Accept':'text/csv'})
    with urllib.request.urlopen(req,timeout=25) as reply:raw=reply.read(5_000_000)
    text=raw.decode('utf-8-sig',errors='replace')
    lines=text.splitlines()
    h=next((i for i,line in enumerate(lines) if line.startswith('Ticker,Name,Sector,Asset Class')),-1)
    if h<0:raise DataUnavailable('iShares holdings CSV header not found')
    asof=''
    for line in lines[:h]:
        if line.startswith('Fund Holdings as of'):
            try:asof=next(csv.reader([line]))[1].strip()
            except (ValueError,IndexError):pass
    if not asof:raise DataUnavailable('iShares source effective date missing; refusing undated roster')
    try: source_date=dt.datetime.strptime(asof,'%b %d, %Y').date()
    except ValueError as exc:raise DataUnavailable('Unknown iShares date format: '+asof) from exc
    if source_date>today or (today-source_date).days>14:
        raise DataUnavailable(f'Stale/future iShares source snapshot as of {source_date}; today {today}')
    reader=csv.DictReader(io.StringIO('\n'.join(lines[h:])))
    required={'Ticker','Name','Sector','Asset Class','Market Value','Location','Exchange','Market Currency'}
    if not required.issubset(set(reader.fieldnames or [])):
        raise DataUnavailable('iShares holdings schema changed')
    ranked={}
    for row in reader:
        if row['Asset Class'].strip().lower()!='equity':continue
        value=parse_num(row['Market Value'])
        if value<=0:continue
        key=re.sub('[^A-Z0-9]+',' ',row['Name'].upper()).strip()
        if not key:continue
        ticker=derive(row['Ticker'],row['Exchange'],row['Location'])
        if not ticker:continue
        if key not in ranked:ranked[key]={'issuer_key':key,'company_name':row['Name'],
            'symbol':ticker,'sector':row['Sector'],'country':row['Location'],'currency':row['Market Currency'],
            'holding_market_value_usd':0.0,'primary_market_value_usd':value}
        ranked[key]['holding_market_value_usd']+=value
        if value>ranked[key]['primary_market_value_usd']:
            ranked[key]['symbol']=ticker;ranked[key]['primary_market_value_usd']=value
    allrows=sorted(ranked.values(),key=lambda r:r['holding_market_value_usd'],reverse=True)
    if len(allrows)<500:raise DataUnavailable('ETF constituent sample under 500: insufficient universe')
    allrows=allrows[:500]
    for i,row in enumerate(allrows,1):
        row['etf_holding_rank']=i
        del row['primary_market_value_usd']
        row['source_as_of']=asof
        row['first_recorded_at_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
        row['provider']='ISHARES_ACWI_HOLDINGS'
        row['ranking_type']='ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP'
    active=DATA/'universe'
    dated=active/'snapshots'/str(today)
    fields=['etf_holding_rank','issuer_key','company_name','symbol','sector','country','currency',
            'holding_market_value_usd','source_as_of','first_recorded_at_utc','provider','ranking_type']
    latest=active/'global500_latest.csv'
    first_capture=not (dated/'global500.csv').exists()
    meta={'source_as_of':asof,'recorded_on':str(today),'count':len(allrows),
          'type':'ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP','raw_sha256':sha(raw),
          'mapped_symbols':len(set(x['symbol'] for x in allrows))}
    if first_capture:
        csv_write(dated/'global500.csv',allrows,fields)
        json_save(dated/'source.json',meta)
    else:
        marker=dated/'source.json'
        if not marker.exists():raise DataUnavailable('Dated universe snapshot missing its source manifest')
        frozen=json.loads(marker.read_text(encoding='utf-8'))
        rows=rows_load(dated/'global500.csv')
        if not rows or rows[0].get('source_as_of')!=frozen.get('source_as_of'):
            raise DataUnavailable('Dated universe snapshot provenance mismatch; blocked')
    # Latest is overwriteable; dated daily captures and their manifests are immutable.
    csv_write(latest,allrows,fields)
    return allrows,meta

FIELDS=['date','symbol','open','close','volume','currency','unit_scale','source_sha256','first_seen_at_utc','last_verified_at_utc',
        'price_unit_is_normalized']
def update_history(provider,symbol,kind,today,base_date,overlap_days=10):
    folder=DATA/kind
    file=folder/symbol_filename(symbol)
    past=rows_load(file)
    # Older rows stored UK GBp *numbers* with a GBP currency label and scale
    # metadata. Convert them exactly once before merging new Yahoo candles.
    # All stored open/close values now have units of the declared currency.
    normalized_legacy=0
    for historic in past:
        if historic.get('price_unit_is_normalized')=='true':continue
        unit=float(historic.get('unit_scale') or 1.)
        if not 0.<unit<=1.:
            raise DataUnavailable('Invalid historic unit scale: '+symbol)
        if unit!=1.:
            historic['open']=format(float(historic['open'])*unit,'.10g')
            historic['close']=format(float(historic['close'])*unit,'.10g')
        historic['price_unit_is_normalized']='true'
        normalized_legacy+=1
    if past:
        last=max(dt.date.fromisoformat(row['date']) for row in past)
        first=min(dt.date.fromisoformat(row['date']) for row in past)
        # A backtest-grade warm-up must predate the latest 252-session signals.
        start=base_date if first>base_date+dt.timedelta(days=30) else max(base_date,last-dt.timedelta(days=overlap_days))
    else:start=base_date
    if start>today:
        if normalized_legacy:csv_write(file,past,FIELDS)
        return {'symbol':symbol,'kind':kind,'status':'UP_TO_DATE','records':len(past),
                'first':past[0]['date'],'last':past[-1]['date'],
                'normalized_legacy_rows':normalized_legacy}
    candles=provider.history(symbol,start,today)
    old={r['date']:r for r in past}
    inserted=0;revised=0;upgraded=0;provisional_blocked=0;provisional_purged=0
    observed_utc=dt.datetime.now(dt.timezone.utc)
    # A partial Tokyo/Hong Kong/London/Saudi quote is just as dangerous as a
    # partial US candle. Use the actual venue's DST-aware *local* close.
    for d,b in sorted(candles.items()):
        if d<base_date or d>today:continue
        if not is_final_bar(symbol,d,observed_utc):
            provisional_blocked+=1
            if d.isoformat() in old:
                # Older downloader versions could cache intraday local bars.
                # They must not be passed onward as certified EOD closes.
                del old[d.isoformat()]
                provisional_purged+=1
            continue
        observed=observed_utc.isoformat()
        # Yahoo GBp becomes GBP before persistence: no reader is required to
        # guess whether the numeric price must be multiplied by 0.01 again.
        unit=float(b.unit_scale)
        if not 0.<unit<=1.:raise DataUnavailable('Bad provider quote scale for '+symbol)
        row={'date':d.isoformat(),'symbol':symbol,'open':format(b.open*unit,'.10g'),
            'close':format(b.close*unit,'.10g'),'volume':format(b.volume,'.10g'),'currency':b.currency,
            'unit_scale':str(b.unit_scale),'source_sha256':b.source_sha256,
            'first_seen_at_utc':observed,'last_verified_at_utc':observed,
            'price_unit_is_normalized':'true'}
        prev=old.get(row['date'])
        if prev is None:old[row['date']]=row;inserted+=1
        elif any(prev.get(k)!=row[k] for k in ('open','close','currency','unit_scale')):
            # First observed timestamp survives provider revision. Missing
            # timestamps in older cached rows remain explicitly UNKNOWN.
            row['first_seen_at_utc']=prev.get('first_seen_at_utc','')
            revised+=1
            # Preserve the prior observation for audit, then replace revised provider bar.
            audit=DATA/'audit'/'revisions.csv'
            changes=rows_load(audit)
            changes.append({'observed_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                            'symbol':symbol,'date':row['date'],'previous_open':prev['open'],
                            'previous_close':prev['close'],'new_open':row['open'],'new_close':row['close'],
                            'previous_sha256':prev.get('source_sha256',''),'new_sha256':row['source_sha256']})
            csv_write(audit,changes,['observed_at','symbol','date','previous_open','previous_close','new_open','new_close','previous_sha256','new_sha256'])
            old[row['date']]=row
        elif prev.get('volume','')!=row['volume']:
            # The first backfill provides daily volume for liquidity filters.
            row['first_seen_at_utc']=prev.get('first_seen_at_utc','')
            row['last_verified_at_utc']=prev.get('last_verified_at_utc','')
            old[row['date']]=row;upgraded+=1
    ordered=[old[d] for d in sorted(old)]
    if inserted or revised or upgraded or provisional_purged or normalized_legacy or not file.exists():
        csv_write(file,ordered,FIELDS)
    if not ordered:raise DataUnavailable('provider returned no valid rows: '+symbol)
    return {'symbol':symbol,'kind':kind,'status':'OK','inserted':inserted,'revised':revised,'volume_rows_updated':upgraded,
            'provisional_bars_blocked':provisional_blocked,
            'prior_intraday_bars_removed':provisional_purged,
            'normalized_legacy_rows':normalized_legacy,
            'prices_in_declared_currency_units':True,
            'market_session_rule':venue(symbol)[0],
            'records':len(ordered),'first':ordered[0]['date'],'last':ordered[-1]['date'],
            'latest_file_sha256':sha(file.read_bytes())}

def choose_symbols(historical,universe,limit,status):
    # Revisit a few liquid symbols every day; rotate remaining set through every run.
    active=[r['symbol'] for r in universe] if universe else []
    combined=list(dict.fromkeys(['ACWI']+active+historical))
    pinned=list(dict.fromkeys(['ACWI','AAPL','MSFT','NVDA','XOM']+active[:20]))
    pinned=[s for s in pinned if s in combined]
    remaining=[s for s in combined if s not in pinned]
    cursor=int(status.get('round_robin_cursor',0))
    if remaining:
        offset=cursor%len(remaining)
        rotating=(remaining[offset:]+remaining[:offset])[:max(0,limit-len(pinned))]
        cursor=(offset+len(rotating))%len(remaining)
    else:rotating=[]
    return list(dict.fromkeys(pinned+rotating))[:limit],len(combined),cursor

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--limit',type=int,default=90)
    p.add_argument('--start',default='2024-01-01')
    p.add_argument('--no-universe',action='store_true')
    args=p.parse_args()
    if args.limit<25 or args.limit>650:raise ValueError('25..650 symbols per run')
    today=market_date()
    base=dt.date.fromisoformat(args.start)
    if base>today:raise ValueError('start is future')
    historic=old_symbols()
    terminal=terminal_symbols(today)
    old_status_file=DATA/'audit'/'latest.json'
    try:old_status=json.loads(old_status_file.read_text()) if old_status_file.exists() else {}
    except (ValueError, OSError):old_status={}
    universe=[];universe_error=None
    if not args.no_universe:
        try:universe,universe_meta=universe_snapshot(today)
        except Exception as e:universe_error=f'{type(e).__name__}: {str(e)[:160]}'
    if not universe:
        universe=rows_load(DATA/'universe/global500_latest.csv')
        universe_meta={'status':'LAST_CACHED_UNIVERSE_ONLY' if universe else 'MISSING','error':universe_error}
    # The instrument currency, not the ticker country, determines EUR conversion requirements.
    active_currencies={str(row.get('currency','')).upper().strip() for row in universe}
    active_currencies|={str(row.get('currency','')).upper().strip() for row in rows_load(DATA/'universe/global500_latest.csv')}
    required_fx=list(dict.fromkeys(FX+sorted('EUR'+curr+'=X' for curr in active_currencies
                              if len(curr)==3 and curr.isalpha() and curr not in ('EUR','SAR'))))
    active_universe=[r for r in universe if r['symbol'] not in terminal]
    symbols,total,cursor=choose_symbols([s for s in historic if s not in terminal],active_universe,args.limit,old_status)
    provider=YahooChart()
    metrics=[];errors=[]
    for fx in required_fx:
        try:metrics.append(update_history(provider,fx,'fx',today,base))
        except (DataUnavailable,OSError,ValueError) as e:errors.append({'kind':'fx','symbol':fx,'error':str(e)[:220]})
        time.sleep(0.15)
    for i,symbol in enumerate(symbols,1):
        try:metrics.append(update_history(provider,symbol,'prices',today,base))
        except (DataUnavailable,OSError,ValueError) as e:errors.append({'kind':'prices','symbol':symbol,'error':str(e)[:220]})
        if i%30==0:print(f'progress: {i}/{len(symbols)} symbols',flush=True)
        time.sleep(0.15)
    # Aggregate all cached symbols, not merely the processed batch.
    cached_price=[]
    for file in (DATA/'prices').glob('*.csv'):
        try:
            rows=rows_load(file)
            if not rows:continue
            cached_price.append({'symbol':rows[0]['symbol'],'first':rows[0]['date'],'last':rows[-1]['date'],'rows':len(rows)})
        except (OSError,KeyError):continue
    cached_fx=[]
    for file in (DATA/'fx').glob('*.csv'):
        try:
            rows=rows_load(file)
            if rows:cached_fx.append({'symbol':rows[0]['symbol'],'first':rows[0]['date'],'last':rows[-1]['date'],'rows':len(rows)})
        except (OSError,KeyError):continue
    required_fx_set=set(required_fx)
    cached_fx_set={r['symbol'] for r in cached_fx}
    missing_fx=sorted(required_fx_set-cached_fx_set)
    price_set={r['symbol'] for r in cached_price}
    missing=[s for s in historic if s not in price_set and s not in terminal]
    all_required=(set(historic)|{r['symbol'] for r in universe})-terminal
    complete=len(all_required-price_set)==0
    recent=sum(1 for r in cached_price if r['symbol'] in all_required and
               (today-dt.date.fromisoformat(r['last'])).days<=7)
    meta={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'source':'Yahoo Chart unofficial',
          'dataset_use':'FORWARD_RESEARCH_NOT_PIT_CERTIFIED','base_date':str(base),
          'historic_inventory_count':len(historic),'forward_universe':universe_meta,
          'verified_terminal_delisted_symbols':sorted(terminal),
          'historical_originals_accounted_for_including_verified_delistings':len(historic)-len(missing),
          'total_candidate_symbols':total,'round_robin_cursor':cursor,
          'cached_required_quotes_complete':complete,
          'quotes_cache_status':'ALL_ACTIVE_SYMBOLS_HAVE_SOME_DATA' if complete else 'PARTIAL_DATA',
          'required_symbols_fresh_7_calendar_days':recent,
          'processed_price_symbols':len(symbols),'price_symbols_cached':len(cached_price),
          'original_288_symbols_cached':sum(x in price_set for x in historic),
          'original_288_symbols_missing':missing,'fx_symbols_cached':len(required_fx_set & cached_fx_set),
          'fx_symbols_expected':len(required_fx_set),'fx_symbols_missing_required':missing_fx,
          'fx_validation':'EXCHANGE_LOCAL_CLOSE_PLUS_BUFFER_AND_NY_FX_ROLLOVER_NO_ANNUAL_INTERPOLATION_NOT_HISTORICAL_PIT',
          'local_exchange_eod_gate_enabled':True,
          'provisional_bars_blocked':sum(int(x.get('provisional_bars_blocked',0)) for x in metrics),
          'older_provisional_bars_removed':sum(int(x.get('prior_intraday_bars_removed',0)) for x in metrics),
          'legacy_price_rows_normalized_to_currency_units':sum(int(x.get('normalized_legacy_rows',0)) for x in metrics),
          'quote_prices_in_declared_currency_units':True,
          'fx_partial_daily_bar_blocked':True,
          'fx_observation_provenance':'first_seen_at_utc on newly inserted rows; legacy blanks not retroactively certified',
          'provider_errors':errors,
          'data_ready_for_confidence70_model':False,
          'reason_not_model_ready':'PIT validated forward USA100 rankings, RoW issuer scores, full technical features, independent daily FX provenance and true engine forward signal still missing'}
    json_save(old_status_file,meta)
    csv_write(DATA/'audit/coverage.csv',sorted(cached_price,key=lambda r:r['symbol']),['symbol','first','last','rows'])
    lines=['# Atlas V4 daily research dataset', '',
           f'Checked: {meta["checked_at_utc"]}',f'Original symbols cached: {meta["original_288_symbols_cached"]}/288',
           f'Originals accounted incl. delistings: {meta["historical_originals_accounted_for_including_verified_delistings"]}/288',
           f'Inactive verified delistings: {", ".join(sorted(terminal)) or "none"}',
           f'All ACTIVE cached prices: {len(price_set & all_required)} / {len(all_required)}',f'FX cached: {len(required_fx_set & cached_fx_set)} / {len(required_fx_set)}',
           f'Provider errors: {len(errors)}',
           f'Unique symbols with a quote dated within 7 days: {recent}/{total}',
           f'Cache status: {meta["quotes_cache_status"]}',
           f'Forward universe: {universe_meta.get("source_as_of",universe_meta.get("status","UNKNOWN"))}',
           'La qualita del dataset indipendente non sostituisce lo stato del motore ufficiale: consultare v4_exact_source/forward/latest_status.json.',
           'iShares universe ranks are holding-value proxies, not historical market cap ranks.',
           'Raw Yahoo Chart quotes are unofficial; adjusted/dividend total-return data are NOT claimed.',
           'Research paper data only; no real-money orders.','']
    atomic(DATA/'audit/latest.md',('\n'.join(lines)).encode())
    print(json.dumps({k:v for k,v in meta.items() if k not in ('original_288_symbols_missing','provider_errors')},ensure_ascii=False),flush=True)
    if errors:print('::warning::Dataset provider errors '+str(len(errors)),flush=True)
    # Expected partial coverage is not job failure; audit distinguishes incomplete from ready.
    return 0
if __name__=='__main__':raise SystemExit(main())
