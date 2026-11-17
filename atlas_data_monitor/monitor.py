#!/usr/bin/env python3
"""Read-only shared Atlas quote monitor. Never imported by a decision engine."""
import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc

def load(path):
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def positive(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0

def stamp(x):
    return dt.datetime.fromtimestamp(x, UTC)

def decode_quote(symbol, raw, observed, sessions):
    m = raw["meta"]
    zone = ZoneInfo(m["exchangeTimezoneName"])
    ccy, scale = sessions.normalize_currency(m.get("currency"))
    clock = m.get("regularMarketTime")
    price = m.get("regularMarketPrice")
    period = (m.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = period.get("start"), period.get("end")
    during = isinstance(start, int) and isinstance(end, int) and start <= observed.timestamp() < end
    valid = positive(price) and isinstance(clock, int) and 0 < clock <= observed.timestamp()
    age = round((observed-stamp(clock)).total_seconds()) if valid else None
    if not valid:
        status = "PENDING_MISSING_QUOTE"
    elif during and (clock < start or age > 1800):
        status = "STALE_DURING_REPORTED_SESSION"
    elif during:
        status = "INDICATIVE_INTRADAY"
    else:
        status = "LAST_REPORTED_QUOTE_OUTSIDE_VENDOR_SESSION"
    dates = raw.get("timestamp") or []
    q = (raw.get("indicators", {}).get("quote") or [{}])[0]
    a = (raw.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    final = None
    for i, ts in enumerate(dates):
        day = stamp(ts).astimezone(zone).date()
        cl = (q.get("close") or [])[i]
        if not positive(cl) or not sessions.is_final_bar(symbol, day, observed):
            continue
        op = (q.get("open") or [None]*len(dates))[i]
        v = (q.get("volume") or [None]*len(dates))[i]
        final = {"session": str(day), "price": cl*scale,
                 "open": op*scale if positive(op) else None,
                 "adjusted_close": a[i]*scale if i<len(a) and positive(a[i]) else None,
                 "volume": v if isinstance(v, (int,float)) and math.isfinite(v) and v>=0 else None,
                 "release_after_utc": sessions.release_time_utc(symbol, day).isoformat()}
    venue, market = sessions.venue(symbol)
    # A weekday guess is only a diagnostic; no holiday or missing session fabricated.
    local_day = observed.astimezone(ZoneInfo(market.zone)).date()
    weekdays = {6,0,1,2,3} if venue==".SR" else {0,1,2,3,4}
    expected = local_day
    for _ in range(8):
        if expected.weekday() in weekdays and sessions.is_final_bar(symbol,expected,observed):
            break
        expected -= dt.timedelta(days=1)
    due = str(expected)
    eod_status = ("LATEST_EXPECTED_NORMAL_CLOSE_PRESENT" if final and final["session"]>=due
                  else "PENDING_CLOSE_OR_HOLIDAY_VERIFY_CALENDAR")
    return {"symbol":symbol,"currency":ccy,"exchange_timezone":str(zone),
            "quote_price":price*scale if valid else None,
            "quote_at_utc":stamp(clock).isoformat() if valid else None,
            "quote_age_seconds":age,"quote_status":status,
            "vendor_session_open":during,"last_confirmed_daily_bar":final,
            "expected_normal_session":due,"daily_status":eod_status,
            "observed_at_utc":observed.isoformat()}

def fetch(symbol, sessions):
    params = urllib.parse.urlencode({"range":"5d","interval":"1d","includeAdjustedClose":"true"})
    last = None
    for host in ("query1.finance.yahoo.com","query2.finance.yahoo.com"):
        try:
            url = "https://"+host+"/v8/finance/chart/"+urllib.parse.quote(symbol,safe="")+"?"+params
            request = urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"})
            with urllib.request.urlopen(request,timeout=8) as response:
                data=response.read(2_000_000)
            observed=dt.datetime.now(UTC)
            raw=json.loads(data)["chart"]["result"][0]
            row=decode_quote(symbol,raw,observed,sessions)
            row["source_sha256"]=hashlib.sha256(data).hexdigest()
            return row
        except Exception as exc:
            last=exc
            if isinstance(exc,urllib.error.HTTPError) and exc.code==429:
                break
    return {"symbol":symbol,"quote_status":"PENDING_PROVIDER_ERROR","daily_status":"PENDING_PROVIDER_ERROR",
            "error":str(last)[:180],"observed_at_utc":dt.datetime.now(UTC).isoformat(),
            "quote_price":None,"last_confirmed_daily_bar":None}

def indicative_nav(portfolio, quotes, symbol_map=None):
    if not portfolio:
        return {"status":"PENDING_LEDGER_UNAVAILABLE","nav_eur":None}
    cash=portfolio.get("cash_eur")
    holdings=portfolio.get("holdings",portfolio.get("positions"))
    if isinstance(holdings,dict):
        holdings=[dict(v,ticker=k) for k,v in holdings.items() if isinstance(v,dict)]
        if len(holdings)!=len(portfolio["positions"]):
            return {"status":"PENDING_UNRECOGNIZED_LEDGER","nav_eur":None}
    if not isinstance(cash,(int,float)) or not math.isfinite(cash) or not isinstance(holdings,list):
        return {"status":"PENDING_UNRECOGNIZED_LEDGER","nav_eur":None}
    total=cash
    missing=[]
    records=[]
    def usable(row):
        if not row:
            return False
        if row.get("vendor_session_open"):
            return row.get("quote_status")=="INDICATIVE_INTRADAY" and positive(row.get("quote_price"))
        return (row.get("daily_status")=="LATEST_EXPECTED_NORMAL_CLOSE_PRESENT" and
                positive((row.get("last_confirmed_daily_bar") or {}).get("price")))
    def mark(row):
        if row.get("vendor_session_open"):
            return row["quote_price"], row["quote_at_utc"]
        bar=row["last_confirmed_daily_bar"]
        return bar["price"], "DAILY_SESSION:"+bar["session"]
    for h in holdings:
        ticker=h.get("ticker")
        symbol=h.get("market_symbol") or (symbol_map or {}).get(ticker,ticker)
        qty=h.get("shares",h.get("quantity"))
        q=quotes.get(symbol)
        if not positive(qty) or not usable(q):
            missing.append(symbol or str(ticker));continue
        ccy=q["currency"]
        rates=[]
        if ccy=="EUR": rate=1.
        elif ccy=="SAR":
            rates=[quotes.get("EURUSD=X"),quotes.get("USDSAR=X")]
            rate=mark(rates[0])[0]*mark(rates[1])[0] if all(usable(x) for x in rates) else None
        else:
            rates=[quotes.get("EUR"+ccy+"=X")]
            rate=mark(rates[0])[0] if usable(rates[0]) else None
        if not positive(rate):
            missing.append(symbol+" FX");continue
        px,quote_at=mark(q)
        value=qty*px/rate
        records.append({"symbol":symbol,"shares":qty,"value_eur":round(value,4),
                        "quote_source_time":quote_at,
                        "fx_quote_source_times":[mark(x)[1] for x in rates]})
        total+=value
    return {"status":"PENDING_INCOMPLETE_QUOTES" if missing else "INDICATIVE_MIXED_TIMESTAMP_NOT_LEDGER",
            "nav_eur":None if missing else round(total,4),"cash_eur":cash,"missing":missing,"holdings":records}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source",type=Path,required=True)
    p.add_argument("--challenger",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    args=p.parse_args()
    source=args.source/"strict_shadow"
    live=source/"live_v4"
    sys.path.insert(0,str(live.resolve()))
    import market_sessions as sessions
    from dataset_daily import FX
    roster=live/"dataset/universe/global500_latest.csv"
    with roster.open(encoding="utf-8",newline="") as f: members=list(csv.DictReader(f))
    if len(members)<500: raise ValueError("Current ACWI roster incomplete")
    official=source/"v4_exact_source/forward"
    portfolio=load(official/"portfolio.json")
    status=load(official/"latest_status.json") or {}
    challenger=load(args.challenger/"challenger_top5/state.json")
    symbol_map=(status.get("source") or {}).get("ticker_to_market_symbol") or {}
    symbols={r["symbol"] for r in members if r.get("symbol")}
    symbols.update(FX)
    currencies={r.get("currency","").upper() for r in members}
    symbols.update("EUR"+c+"=X" for c in currencies if len(c)==3 and c.isalpha() and c not in ("EUR","SAR"))
    symbols.update(symbol_map.values())
    symbols.update(["ACWI","SPY","IUSQ.DE"])
    if portfolio:
        symbols.update(h["market_symbol"] for h in portfolio.get("holdings",[]) if h.get("market_symbol"))
    if challenger and isinstance(challenger.get("positions"),dict):
        for ticker,h in challenger["positions"].items():
            symbols.add(h.get("market_symbol") or symbol_map.get(ticker,ticker))
    before=dt.datetime.now(UTC)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        quotes=list(pool.map(lambda s:fetch(s,sessions),sorted(symbols)))
    by_symbol={r["symbol"]:r for r in quotes}
    caproot=live/"dataset/marketcap_observed"
    cap=load(caproot/"latest_status.json")
    out=args.out
    out.mkdir(parents=True,exist_ok=True)
    def save(name,obj):
        tmp=out/(name+".tmp")
        tmp.write_text(json.dumps(obj,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8")
        os.replace(tmp,out/name)
    save("quotes.json",quotes)
    # The half-hour pass must never overwrite a newer hourly capture with
    # an older copy from the original branch. Preserve the last valid timestamp.
    previous=load(out/"latest_status.json") or {}
    previous_cap=previous.get("capitalization")
    cap_attempt=cap
    cap_stamp=(cap or {}).get("observed_at_utc") or ""
    old_stamp=(previous_cap or {}).get("observed_at_utc") or ""
    published=False
    if cap_stamp and cap_stamp>=old_stamp:
        location=cap.get("latest_observation_path")
        if location:
            d=(live/"dataset"/location).resolve()
            if d.is_relative_to(caproot.resolve()) and (d/"ranking.csv").is_file():
                tmp=out/"capitalization_latest.csv.tmp"
                tmp.write_bytes((d/"ranking.csv").read_bytes())
                os.replace(tmp,out/"capitalization_latest.csv")
                published=True
    if not published:
        cap=previous_cap if old_stamp and (out/"capitalization_latest.csv").is_file() else None
    counts={}
    for r in quotes:
        key=r["quote_status"];counts[key]=counts.get(key,0)+1
    expected_pending=[r["symbol"] for r in quotes if r["daily_status"]!="LATEST_EXPECTED_NORMAL_CLOSE_PRESENT"]
    save("latest_status.json",{"monitor_only":True,"not_decision_input":True,
        "provider":"Yahoo Chart public unofficial; delay not guaranteed",
        "started_at_utc":before.isoformat(),"finished_at_utc":dt.datetime.now(UTC).isoformat(),
        "symbols_requested":len(symbols),"quote_counts":counts,"expected_close_pending":expected_pending,
        "normal_session_calendar_only":True,"acwi_roster_sha256":hashlib.sha256(roster.read_bytes()).hexdigest(),
        "acwi_members":len(members),"capitalization":cap,"capitalization_source_attempt":cap_attempt,
        "official_original":indicative_nav(portfolio,by_symbol,symbol_map),
        "challenger_top5":indicative_nav(challenger,by_symbol,symbol_map)})
    print("ATLAS MONITOR",counts,"expected close pending",len(expected_pending))

if __name__=="__main__":
    main()
