#!/usr/bin/env python3
"""As-recorded global market-cap research feed; NEVER redate a vendor's data.

Separate from official live cohort until mapping and PIT coverage are proven.
Source is a third-party contemporaneous public ranking, not filing-grade shares.
"""
from __future__ import annotations
import csv,datetime as dt,hashlib,html,io,json,re,urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]/"live_v4"/"dataset"
OUT=ROOT/"marketcap_observed"
URL="https://companiesmarketcap.com/"
UA="Mozilla/5.0 (compatible; AtlasV4PITResearch/1.0; research paper trading)"
RANKING_TYPE="CONTEMPORANEOUS_VENDOR_MARKET_CAP_USD_AS_RECORDED_ACWI_CANDIDATE_MATCH"
CAPTURED_TYPE="MARKET_CAP_USD_OBSERVED_ONLY_NOT_HISTORICAL_PIT_CERTIFIED"
ALIASES={"BRK.B":"BRK-B","GOOG":"GOOG","ROG.SW":"ROP.SW"}

class _Table(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows=[];self.row=None;self.cell=None;self.depth=0
        self.sym_depth=None;self.symbol_fragments=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);classes=a.get("class","")
        if tag=="tr":
            self.row=[];self.depth=0;self.sym_depth=None;self.symbol_fragments=[]
        elif self.row is not None:
            self.depth+=1
            if tag=="td":self.cell={"text":[],"sort":a.get("data-sort"),"symbol":[]}
            if "company-code" in classes or "company-ticker" in classes:
                self.sym_depth=self.depth
    def handle_data(self,data):
        if self.row is None:return
        if self.cell is not None:
            self.cell["text"].append(data)
            if self.sym_depth is not None:self.cell["symbol"].append(data)
    def handle_endtag(self,tag):
        if self.row is None:return
        if tag=="td" and self.cell is not None:
            self.row.append(self.cell);self.cell=None
        elif tag=="tr":
            if len(self.row)>=4:self.rows.append(self.row)
            self.row=None;self.cell=None;self.sym_depth=None;return
        if self.sym_depth is not None and self.depth<=self.sym_depth:
            self.sym_depth=None
        if self.depth>0:self.depth-=1

def _plain(s):return re.sub(r"\s+"," ",str(s)).strip()
def _number(raw):
    raw=_plain(raw).replace(",","")
    m=re.search(r"([$€£])?\s*([0-9]+(?:\.[0-9]+)?)\s*([TtBbMmKk])\b",raw)
    if m:return float(m.group(2))*{"T":1e12,"B":1e9,"M":1e6,"K":1e3}[m.group(3).upper()]
    try:
        x=float(raw)
        return x if x>1e7 else None
    except (ValueError,TypeError):return None

def parse_ranking(source):
    p=_Table();p.feed(source)
    out=[]
    for row in p.rows:
        try:
            rank=int(re.search(r"\d+",_plain("".join(row[0]["text"]))).group())
            name=_plain("".join(row[1]["text"]))
            symbols=row[1]["symbol"]
            ticker=_plain("".join(symbols)).upper()
            # Some layouts omit the separate company-code div.
            if not ticker:
                m=re.search(r"([A-Z0-9]{1,9}(?:[.-][A-Z0-9]{1,5})?)$",name)
                ticker=m.group(1) if m else ""
            if not re.fullmatch(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*",ticker):continue
            cap=_number("".join(row[2]["text"]))
            if not cap and row[2]["sort"]:cap=_number(row[2]["sort"])
            if not cap or cap<1e7:continue
            country=_plain("".join(row[-1]["text"]))
            if not country:continue
            out.append({"vendor_rank":rank,"vendor_ticker":ticker,"market_cap_usd":round(cap,2),
                        "vendor_company_name":name,"vendor_country":country})
        except (ValueError,AttributeError,IndexError,TypeError):
            continue
    return out

def fetch_pages(count=9,fetch=None):
    fetch=fetch or _fetch
    retrieved=dt.datetime.now(dt.timezone.utc)
    result=[]
    page_hashes=[]
    for page in range(1,count+1):
        url=URL if page==1 else URL+f"page/{page}/"
        raw=fetch(url)
        result.extend(parse_ranking(raw.decode("utf-8","replace")))
        page_hashes.append({"page":page,"url":url,"sha256":hashlib.sha256(raw).hexdigest()})
    ranks={x["vendor_rank"]:x for x in result}
    if len(ranks)<count*80:
        raise ValueError(f"market cap vendor coverage too low: {len(ranks)} / {count*100} positions")
    if len({x["vendor_ticker"] for x in ranks.values()})<count*75:
        raise ValueError("too many duplicate tickers or parse failures")
    return sorted(ranks.values(),key=lambda x:x["vendor_rank"]),page_hashes,retrieved

def _fetch(url):
    r=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"text/html"})
    with urllib.request.urlopen(r,timeout=22) as response:
        data=response.read(3_000_000)
        if response.status!=200:raise ValueError("non-200 market-cap source")
        return data

def crosswalk(vendor,etf):
    bysymbol={}
    for item in vendor:
        s=item["vendor_ticker"].replace(".", "-") if item["vendor_ticker"].startswith("BRK.") else item["vendor_ticker"]
        bysymbol.setdefault(s,item)
    # Do not silently convert ADR caps into listing-specific caps. Ticker
    # identity and issuer share class need independent confirmation.
    matches=[]
    for row in etf:
        sym=row["symbol"].upper()
        x=bysymbol.get(sym)
        if x is None:continue
        matches.append({**row,"market_cap_usd":x["market_cap_usd"],"market_cap_vendor_rank":x["vendor_rank"],
                        "market_cap_vendor_ticker":x["vendor_ticker"],
                        "capitalization_coverage_status":"EXACT_TICKER_MATCH_REQUIRES_ISSUER_REVIEW",
                        "ranking_type":RANKING_TYPE})
    matches.sort(key=lambda r:-float(r["market_cap_usd"]))
    return matches

def run(now=None,fetch=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:raise ValueError("aware UTC timestamp required")
    now=now.astimezone(dt.timezone.utc)
    # Source ranking is frozen by observation day and cannot be reused for
    # any earlier historical signal, including yesterday's close.
    day=now.date().isoformat()
    latest=ROOT/"universe"/"global500_latest.csv"
    if not latest.exists():raise ValueError("ACWI crosswalk missing")
    with latest.open(encoding="utf-8",newline="") as f:etf=list(csv.DictReader(f))
    vendor,page_hashes,seen=fetch_pages(fetch=fetch)
    matches=crosswalk(vendor,etf)
    usa=sum(x["country"]=="United States" for x in matches)
    row=sum(x["country"]!="United States" for x in matches)
    coverage={"observed_at_utc":seen.isoformat(),"capture_day":day,
              "vendor":URL,"type":CAPTURED_TYPE,
              "vendor_rankings_fetched":len(vendor),"crosswalk_exact_ticker":len(matches),
              "crosswalk_usa":usa,"crosswalk_non_usa":row,
              "sufficient_for_100_usa_200_nonusa":usa>=100 and row>=200,
              "vendor_page_sha256":page_hashes,
              "historical_pit_market_cap_verified":False,
              "daily_new_source_can_change_yesterdays_original_signal":False,
              "purpose":"SHADOW_CAPITALIZATION_RANKING_NOT_LIVE_UNIVERSE_UNTIL_COVERAGE_VALIDATED"}
    OUT.mkdir(parents=True,exist_ok=True)
    daydir=OUT/"snapshots"/day
    daydir.mkdir(parents=True,exist_ok=True)
    fname=daydir/"ranking.csv"
    # Append-only snapshots; never overwrite previous observation for the day.
    if not fname.exists():
        buf=io.StringIO()
        fields=["market_cap_vendor_rank","market_cap_usd","symbol","company_name","country","sector",
                "currency","capitalization_coverage_status","ranking_type"]
        writer=csv.DictWriter(buf,fieldnames=fields,extrasaction="ignore")
        writer.writeheader();writer.writerows(matches)
        fname.write_text(buf.getvalue(),encoding="utf8")
        (daydir/"source.json").write_text(json.dumps(coverage,indent=2,sort_keys=True)+"\n")
    (OUT/"latest_status.json").write_text(json.dumps(coverage,indent=2,sort_keys=True)+"\n")
    print("MARKET_CAP_SHADOW",json.dumps({k:coverage[k] for k in
          ("capture_day","vendor_rankings_fetched","crosswalk_exact_ticker","crosswalk_usa",
           "crosswalk_non_usa","sufficient_for_100_usa_200_nonusa")}))
    return coverage

if __name__=="__main__":
    try:run()
    except Exception as exc:
        OUT.mkdir(parents=True,exist_ok=True)
        (OUT/"latest_status.json").write_text(json.dumps({
            "status":"PENDING_VENDOR_MARKET_CAP","reason":str(exc)[:500],
            "captured_at_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
            "historical_pit_market_cap_verified":False},indent=2)+"\n")
        print("MARKET_CAP_SHADOW_PENDING",str(exc)[:300])
