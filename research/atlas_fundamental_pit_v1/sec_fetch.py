#!/usr/bin/env python3
"""Download SEC XBRL fact observations *with filed dates*; research branch only.
SEC source: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
Store all as-filed observations (no future restatement substitution). Do not fabricate
non-available analyst EPS consensus/revisions. Optional Yahoo splits for price/share
normalization; if missing, value yield remains unavailable for that symbol.
"""
import json,gzip,base64,urllib.request,urllib.error,time,datetime as dt,hashlib,random,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'
USERAGENT='AtlasQuantPITResearch research-team research-contact@example.org'
TAGS={'us-gaap':[
    'Assets','StockholdersEquity','StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',
    'NetIncomeLoss','ProfitLoss','NetIncomeLossAvailableToCommonStockholdersBasic',
    'Revenues','RevenueFromContractWithCustomerExcludingAssessedTax','SalesRevenueNet',
    'OperatingIncomeLoss','NetCashProvidedByUsedInOperatingActivities',
    'PaymentsToAcquirePropertyPlantAndEquipment','WeightedAverageNumberOfDilutedSharesOutstanding',
    'CommonStockSharesOutstanding','EarningsPerShareDiluted',
    'CashAndCashEquivalentsAtCarryingValue','LongTermDebt','LongTermDebtNoncurrent'],
    'dei':['EntityCommonStockSharesOutstanding']}

def loadmarket():
    inputs=([DATA/'market_input.b64'] if (DATA/'market_input.b64').exists() else sorted(DATA.glob('market_input_part_*.b64')))
    b64=''.join(''.join(p.read_text().splitlines()) for p in inputs)
    return json.loads(gzip.decompress(base64.b64decode(b64)))
def get(url,ntry=3):
    headers={'User-Agent':USERAGENT,'Accept':'application/json','Accept-Encoding':'gzip'}
    for i in range(ntry):
        try:
            req=urllib.request.Request(url,headers=headers)
            with urllib.request.urlopen(req,timeout=30) as rr:
                bb=rr.read()
                if rr.headers.get('Content-Encoding','').lower()=='gzip' or bb[:2]==b'\x1f\x8b':bb=gzip.decompress(bb)
                return json.loads(bb.decode('utf-8'))
        except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,ValueError) as exc:
            if i==ntry-1:raise
            time.sleep(1.5*(i+1))

def annual_facts(j):
    out=[]
    facts=j.get('facts',{})
    for ns,tags in TAGS.items():
        for tag in tags:
            d=facts.get(ns,{}).get(tag)
            if not d:continue
            for unit,vals in d.get('units',{}).items():
                if unit not in ('USD','shares','USD/shares'):continue
                for x in vals:
                    if x.get('form') not in ('10-K','10-K/A'):continue
                    filed=x.get('filed','');end=x.get('end','')
                    if not filed or not end or not ('2013-01-01'<=end<='2025-12-31') or not ('2013-01-01'<=filed<='2026-11-04'):continue
                    try:
                        age=(dt.date.fromisoformat(filed)-dt.date.fromisoformat(end)).days
                        if age<0 or age>250:continue
                        if x.get('start'):
                            duration=(dt.date.fromisoformat(end)-dt.date.fromisoformat(x['start'])).days
                            if duration<300 or duration>399:continue
                        elif unit in ('USD/shares',):continue
                        val=float(x['val'])
                        if not -1e25<val<1e25:continue
                        out.append([tag,unit,x.get('start',''),end,filed,x.get('accn',''),val])
                    except (ValueError,TypeError,OverflowError):continue
    # Preserve observations, first file available per metric+period picked at signal time.
    out.sort(key=lambda x:(x[0],x[3],x[4],x[5]))
    return out

def get_splits(sym):
    from urllib.parse import quote
    for host in ['query1.finance.yahoo.com','query2.finance.yahoo.com']:
        try:
            url='https://'+host+'/v8/finance/chart/'+quote(sym,safe='')+'?period1=1262304000&period2=1789344000&interval=1mo&events=splits'
            j=get(url,1)['chart']
            if j.get('error'):raise ValueError(str(j['error']))
            ev=j['result'][0].get('events',{}).get('splits',{})
            splits=[]
            for o in ev.values():
                yy=dt.datetime.fromtimestamp(o['date'],tz=dt.timezone.utc).date().isoformat()
                ratio=float(o.get('numerator',0))/float(o.get('denominator',1))
                if 0<ratio<1000:splits.append([yy,ratio])
            return sorted(splits)
        except Exception:time.sleep(.15)
    return None

def fetch_one(sym, cik):
    try:
        j=get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json')
        obs=annual_facts(j)
        if not obs:return sym,{'status':'NO_ANNUAL_FACTS','cik':int(cik),'name':j.get('entityName')}
        splits=get_splits(sym)
        return sym,dict(status='OK',cik=int(cik),name=j.get('entityName'),observations=obs,splits=splits)
    except Exception as e:return sym,dict(status='SEC_ERROR',cik=int(cik),error=str(e)[:220])

def main():
    market=loadmarket();symbols=sorted(market['symbols'])
    maps=get('https://www.sec.gov/files/company_tickers.json')
    tt={x['ticker']:x for x in maps.values()}
    out={};requests={};skip={}
    for sym in symbols:
        rec=tt.get(sym) or tt.get(sym.replace('-','.'))
        if not rec:skip[sym]='TICKER_NOT_FOUND_SEC';continue
        requests[sym]=rec['cik_str']
    with ThreadPoolExecutor(max_workers=4) as pool:
        fs={pool.submit(fetch_one,s,c):s for s,c in requests.items()}
        for fut in as_completed(fs):
            sym,rec=fut.result();out[sym]=rec
            if len(out)%20==0:print('processed',len(out),'/',len(requests),flush=True)
    data={'schema':1,'extracted_on':dt.datetime.now(dt.timezone.utc).isoformat(),
          'SEC_url':'https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json',
          'SEC_ticker_map_url':'https://www.sec.gov/files/company_tickers.json',
          'split_vendor':'Yahoo Finance events=splits, as downloaded (not an official execution source)',
          'entities':out,'missing_sec_map':skip}
    raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
    DATA.mkdir(exist_ok=True,parents=True)
    (DATA/'sec_pit_facts.json.gz').write_bytes(gzip.compress(raw,compresslevel=9,mtime=0))
    status={sym:x.get('status') for sym,x in out.items()}
    summary={'requested':len(symbols),'mapped_to_sec':len(requests),'facts_ok':sum(x=='OK' for x in status.values()),
      'split_history_ok':sum(v.get('splits') is not None for v in out.values()),'no_sec_ticker':len(skip),
      'failed_or_no_annual':{k:v for k,v in status.items() if v!='OK'},
      'raw_sha256':hashlib.sha256(raw).hexdigest(),'compressed_bytes':(DATA/'sec_pit_facts.json.gz').stat().st_size}
    (DATA/'sec_download_audit.json').write_text(json.dumps(summary,indent=2))
    print('SEC_DOWNLOAD_AUDIT',json.dumps(summary)[:4500],flush=True)
if __name__=='__main__':main()