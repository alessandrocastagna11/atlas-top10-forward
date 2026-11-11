"""Yahoo Chart EOD quotes with exchange-local sessions and lookahead-safe FX. Research only."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from market_sessions import normalize_currency

class DataUnavailable(ValueError):
    pass

def _date(v):
    return dt.date.fromisoformat(str(v))

@dataclass(frozen=True)
class Bar:
    session: dt.date
    open: float
    close: float
    currency: str
    symbol: str
    source_sha256: str
    unit_scale: float = 1.0
    volume: float = 0.0

class YahooChart:
    """No guessed candles. An exact exchange-local session is mandatory."""
    def __init__(self, timeout=22):
        self.timeout=timeout
        self.cache={}
    def history(self,symbol,start,end):
        key=(symbol,str(start),str(end))
        if key in self.cache: return self.cache[key]
        since=int(dt.datetime.combine(_date(start),dt.time.min,dt.timezone.utc).timestamp())-86400
        until=int(dt.datetime.combine(_date(end)+dt.timedelta(days=3),dt.time.min,dt.timezone.utc).timestamp())
        url='https://query1.finance.yahoo.com/v8/finance/chart/'+urllib.parse.quote(symbol,safe='')+'?'+urllib.parse.urlencode({
            'period1':since,'period2':until,'interval':'1d','events':'div,splits','includeAdjustedClose':'false'})
        data=None
        for attempt in range(3):
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (AtlasV4 research-paper/1.0)','Accept':'application/json'})
                with urllib.request.urlopen(req,timeout=self.timeout) as res: data=res.read(10_000_000)
                break
            except (urllib.error.URLError,TimeoutError,OSError) as exc:
                if attempt==2: raise DataUnavailable(f'provider unavailable: {symbol}: {type(exc).__name__}') from exc
                time.sleep(2*(attempt+1))
        try:
            payload=json.loads(data); result=(payload['chart'].get('result') or [None])[0]
            if not result: raise DataUnavailable(f'provider returned no series for {symbol}')
            meta=result['meta']; tz=ZoneInfo(meta['exchangeTimezoneName'])
            raw=result['indicators']['quote'][0]
            raw_currency=str(meta.get('currency',''))
            if not raw_currency: raise DataUnavailable(f'currency missing: {symbol}')
            try:currency,unit_scale=normalize_currency(raw_currency)
            except ValueError as exc:raise DataUnavailable(f'Unsupported Yahoo quote currency for {symbol}: {raw_currency}') from exc
            candles={}
            digest=hashlib.sha256(data).hexdigest()
            for i,timestamp in enumerate(result.get('timestamp') or []):
                sess=dt.datetime.fromtimestamp(timestamp,dt.timezone.utc).astimezone(tz).date()
                op,cl=raw['open'][i],raw['close'][i]
                if op is None or cl is None or not math.isfinite(float(op)) or not math.isfinite(float(cl)) or op<=0 or cl<=0: continue
                candles[sess]=Bar(sess,float(op),float(cl),currency,symbol,digest,unit_scale,float(raw.get('volume',[None]*len(result.get('timestamp') or []))[i] or 0))
            if not candles: raise DataUnavailable(f'no valid bars: {symbol}')
            self.cache[key]=candles
            return candles
        except (KeyError,IndexError,TypeError,ValueError) as exc:
            if isinstance(exc,DataUnavailable): raise
            raise DataUnavailable(f'bad provider payload: {symbol}: {type(exc).__name__}') from exc

    def bar(self,symbol,session):
        target=_date(session)
        candles=self.history(symbol,target-dt.timedelta(days=10),target)
        if target not in candles: raise DataUnavailable(f'no exact session {target} for {symbol}')
        return candles[target]

    def fx_units_per_eur(self,currency,session,for_open=False):
        curr=str(currency).upper()
        if curr=='EUR':return 1.0,{'fx_symbol':'EUR','fx_session':str(session),'fx_sha256':'EUR'}
        # GBp means pence Sterling; Yahoo historical prices normally use GBp for UK equities.
        if curr in ('GBX','GBPENCE','GBPENNY') : curr='GBP'
        target=_date(session)
        # A Yahoo daily FX bar is not an intraday executable fixing. The
        # same-day FX close may be published AFTER a Tokyo, London or US
        # stock close. Always use the last completed *prior weekday* FX day.
        # Weekend/Sunday FX bars are excluded (some are Monday rollover bars).
        end=target-dt.timedelta(days=1)
        start=end-dt.timedelta(days=10)
        pair='EUR'+curr+'=X'
        def last_close(pair_name):
            candles=self.history(pair_name,start,end)
            eligible=[x for d,x in candles.items() if d<=end and d.weekday()<5]
            if not eligible: raise DataUnavailable(f'FX missing {pair_name} as of {end}')
            b=max(eligible,key=lambda x:x.session)
            if (end-b.session).days>4: raise DataUnavailable(f'stale FX {pair_name}: {b.session}')
            return b
        try:
            b=last_close(pair)
            if not math.isfinite(b.close) or b.close<=0: raise DataUnavailable('invalid FX rate')
            return b.close,{'fx_symbol':pair,'fx_session':str(b.session),'fx_sha256':b.source_sha256}
        except DataUnavailable:
            if curr!='SAR':raise
            a=last_close('EURUSD=X'); b=last_close('USDSAR=X')
            if abs((a.session-b.session).days)>2:
                raise DataUnavailable('Incoherent SAR cross-rate session dates')
            return a.close*b.close,{'fx_symbol':'EURUSD=X*USDSAR=X','fx_session':str(min(a.session,b.session)),'fx_sha256':a.source_sha256+':'+b.source_sha256}

    def latest_published_eur_quote(self,symbol,asof,max_age_days=5):
        """Mark a mixed-exchange holding at its last *published* closing bar.

        A market holiday or different venue weekend cannot be treated as an
        execution. This function is valuation-only; execution still requires
        an EXACT exchange-local session and checks the precommitted signal.
        """
        cutoff=_date(asof)
        bars=self.history(symbol,cutoff-dt.timedelta(days=max_age_days+5),cutoff)
        valid=[day for day in bars if day<=cutoff]
        if not valid:raise DataUnavailable('No previous published close for '+symbol)
        day=max(valid)
        age=(cutoff-day).days
        if age>max_age_days:
            raise DataUnavailable(f'Stale published close for {symbol}: {day} vs {cutoff}')
        result=self.eur_quote(symbol,day,'close')
        result.update({'valuation_cutoff':str(cutoff),'valuation_quote_session':str(day),
                       'valuation_staleness_days':age,'is_exact_valuation_session':day==cutoff})
        return result

    def eur_quote(self,symbol,session,field):
        if field not in ('open','close'):raise ValueError('only open or close')
        bar=self.bar(symbol,session)
        curr=bar.currency
        unit_factor=bar.unit_scale
        rate,fx=self.fx_units_per_eur(curr,bar.session,for_open=(field=='open'))
        quote=getattr(bar,field)*unit_factor/rate
        if not math.isfinite(quote) or quote<=0:raise DataUnavailable(f'invalid EUR quote: {symbol}')
        return {'symbol':symbol,'session':str(bar.session),'field':field,'price_eur':round(quote,10),
                'local_price':getattr(bar,field),'local_unit_scale':unit_factor,'currency':curr,'fx_units_per_eur':rate,
                'conversion_convention':'LAST_PREVIOUS_COMPLETED_WEEKDAY_FX_CLOSE_NOT_SAME_DAY',
                'market_sha256':bar.source_sha256,**fx}
