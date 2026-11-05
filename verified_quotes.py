"""Verified, documented historical quote supplements.  Never fabricates data.
Yahoo quote has priority when both providers have a same-day bar.  Data added
only if independently sourced EOD for the SAME venue and currency.
"""
from __future__ import annotations
import json, math
from pathlib import Path
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parent
DEFAULT_FILE=ROOT/'market_data'/'verified_quotes.json'

def apply_verified(data,asof,shape='close',supplements=None):
    if shape not in ('close','ohlc'):raise ValueError('Unsupported quote shape')
    if supplements is None:
        if not DEFAULT_FILE.exists():return data
        supplements=json.loads(DEFAULT_FILE.read_text(encoding='utf-8'))
    if supplements.get('schema')!=1:raise ValueError('Unknown verified quote schema')
    applied=[]
    for item in supplements.get('quotes',[]):
        sym,day=item['symbol'],item['date']
        if day>asof or sym not in data:continue
        rec=data[sym]
        if item.get('currency')!=rec.get('currency'):
            raise ValueError('Supplement currency mismatch '+sym)
        sources=item.get('source_urls',[])
        hosts={urlsplit(str(x)).hostname for x in sources if
               urlsplit(str(x)).scheme=='https' and urlsplit(str(x)).hostname}
        if len(hosts)<2:
            raise ValueError('At least two distinct HTTPS domains required '+sym)
        verified_on=item.get('verified_on')
        try:
            vdate=date.fromisoformat(verified_on)
            session=date.fromisoformat(day)
        except (TypeError,ValueError):
            raise ValueError('Invalid supplement dates '+sym) from None
        if vdate<session or vdate>datetime.now(timezone.utc).date():
            raise ValueError('Quote verification not yet available '+sym)
        close=float(item['close'])
        op=float(item['open']) if item.get('open') is not None else None
        if not math.isfinite(close) or close<=0 or (op is not None and (not math.isfinite(op) or op<=0)):
            raise ValueError('Invalid EOD price '+sym)
        bars=rec.get('bars',{})
        if day in bars:
            actual=bars[day]['close'] if shape=='ohlc' else bars[day]
            if abs(float(actual)-close)/close>0.005:
                rec.setdefault('supplement_discrepancies',[]).append({'date':day,'source_price':close,'provider_price':actual})
            continue
        if shape=='ohlc':
            # Never use CLOSE as the day's OPEN, even in a virtual backtest.
            if op is None:continue
            bars[day]={'open':op,'close':close,'volume':None}
        else:
            bars[day]=close
        rec.setdefault('verified_supplements',{})[day]={
          'kind':'POST_SESSION_VERIFIED_EOD','verified_on':item['verified_on'],
          'sources':sources,'venue':item['venue']}
        rec['feed']=str(rec.get('feed','Yahoo'))+' + separately verified EOD supplement'
        applied.append((sym,day))
    return data
