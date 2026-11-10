#!/usr/bin/env python3
"""As-recorded global market-cap research feed; NEVER redate a vendor's data.

Separate from official live cohort until mapping and PIT coverage are proven.
Source is a third-party contemporaneous public ranking, not filing-grade shares.
"""
from __future__ import annotations
import csv,datetime as dt,hashlib,html,io,json,re,urllib.request,unicodedata
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
            # The vendor sometimes places a favourite/star column before rank.
            start=next((k for k in range(min(3,len(row)-3))
                        if re.fullmatch(r"\d+",_plain("".join(row[k]["text"])))
                        and (row[k+1]["symbol"] or "company-code" in
                             _plain("".join(row[k+1]["text"])).lower())),None)
            if start is None:
                start=next((k for k in range(min(3,len(row)-3))
                            if re.fullmatch(r"\d+",_plain("".join(row[k]["text"])))),None)
            if start is None:continue
            rank=int(_plain("".join(row[start]["text"])))
            name=_plain("".join(row[start+1]["text"]))
            symbols=row[start+1]["symbol"]
            ticker=_plain("".join(symbols)).upper()
            # Some layouts omit the separate company-code div.
            if not ticker:
                m=re.search(r"([A-Z0-9]{1,9}(?:[.-][A-Z0-9]{1,5})?)$",name)
                ticker=m.group(1) if m else ""
            if not re.fullmatch(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*",ticker):continue
            cap=_number("".join(row[start+2]["text"]))
            if not cap and row[start+2]["sort"]:cap=_number(row[start+2]["sort"])
            if not cap or cap<1e7:continue
            country=_plain("".join(row[-1]["text"]))
            if not country:continue
            out.append({"vendor_rank":rank,"vendor_ticker":ticker,"market_cap_usd":round(cap,2),
                        "vendor_company_name":name,"vendor_country":country})
        except (ValueError,AttributeError,IndexError,TypeError):
            continue
    return out

def fetch_pages(count=25,fetch=None):
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

def _country(s):
    t=unicodedata.normalize("NFKD",str(s)).encode("ascii","ignore").decode("ascii")
    t=re.sub(r"\s+"," ",t).strip().upper()
    codes={"USA":"UNITED STATES","UK":"UNITED KINGDOM","S. KOREA":"SOUTH KOREA",
           "KOREA (SOUTH)":"SOUTH KOREA","S. ARABIA":"SAUDI ARABIA"}
    return codes.get(t,t)

_STOP=set("THE GROUP HOLDINGS HOLDING INC INCORPORATED CLASS CORP CORPORATION LTD "
          "LIMITED PLC COMPANY CO AG SA SE NV GROEP BANCO MOTOR PS PAR NON VOTING "
          "PREFERRED PRE SUBORDINATE ORDINARY A B C H N OF AND".split())
def _issuer_name(s):
    s=unicodedata.normalize("NFKD",str(s)).encode("ascii","ignore").decode("ascii")
    s=re.sub(r"\([^)]*\)"," ",s).upper()
    t=[w for w in re.sub(r"[^A-Z0-9]+"," ",s).split() if w and w not in _STOP]
    return " ".join(sorted(t))

# Reviewed issuer-level correspondence. These are NOT arbitrary Yahoo suffix
# substitutions: each vendor ticker must also match the exchange's country.
# Whitelist deliberately excludes Samsung preferred vs Samsung ordinary and
# other duplicate classes that could distort the issuer top-N ranking.
_CURATED_ISSUER_ALIASES={
    "2330.TW":"TSM", "TD.TO":"TD", "BNS.TO":"BNS","CM.TO":"CM",
    "GSK.L":"GSK", "CP.TO":"CP", "ABX.TO":"B", "3711.TW":"ASX",
    "2318.HK":"601318.SS", "105560.KS":"KB"
}

def crosswalk(vendor,etf):
    bysymbol={}
    for item in vendor:
        s=item["vendor_ticker"].replace(".", "-") if item["vendor_ticker"].startswith("BRK.") else item["vendor_ticker"]
        bysymbol.setdefault(s,item)
    # A name/country match is allowed only when both are unique, with exact
    # issuer-name normalization; no fuzzy edit-distance guesses.
    nameindex={}
    for item in vendor:
        raw=item["vendor_company_name"]
        ticker=item["vendor_ticker"]
        if raw.upper().endswith(ticker.upper()):raw=raw[:-len(ticker)]
        key=(_country(item["vendor_country"]),_issuer_name(raw))
        if key[1]:nameindex.setdefault(key,[]).append(item)
    matches=[];used_vendor=set()
    for row in etf:
        symbol=row["symbol"].upper()
        v=bysymbol.get(symbol)
        if v and _country(v["vendor_country"])==_country(row["country"]) and v["vendor_ticker"] not in used_vendor:
            method="EXACT_TICKER_AND_COUNTRY"
        else:
            v=None
            method=""
        if v is None:
            country=_country(row["country"])
            candidates=[item for item in nameindex.get((country,_issuer_name(row["company_name"])),[])
                        if item["vendor_ticker"] not in used_vendor]
            if len(candidates)==1:
                v=candidates[0];method="UNIQUE_NORMALIZED_ISSUER_NAME_AND_COUNTRY"
        if v is None:
            alias=_CURATED_ISSUER_ALIASES.get(symbol)
            possibility=bysymbol.get(alias)
            if possibility and possibility["vendor_ticker"] not in used_vendor and (
                 _country(possibility["vendor_country"])==_country(row["country"])):
                v=possibility;method="CURATED_ISSUER_ALIAS_AND_COUNTRY"
        if v is None:continue
        used_vendor.add(v["vendor_ticker"])
        matches.append({**row,"market_cap_usd":v["market_cap_usd"],"market_cap_vendor_rank":v["vendor_rank"],
                        "market_cap_vendor_ticker":v["vendor_ticker"],
                        "capitalization_coverage_status":method+"_NOT_FULL_PIT_CERTIFIED",
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
    # Separate expanded observations preserve the initial same-day snapshot;
    # no overwrite or backdating when the vendor ranking gets deeper/better.
    observation_dir=OUT/"observations"/seen.strftime("%Y-%m-%dT%H%M%SZ")
    observation_dir.mkdir(parents=True,exist_ok=True)
    buf=io.StringIO()
    fields=["market_cap_vendor_rank","market_cap_usd","symbol","company_name","country","sector",
            "currency","capitalization_coverage_status","ranking_type"]
    writer=csv.DictWriter(buf,fieldnames=fields,extrasaction="ignore")
    writer.writeheader();writer.writerows(matches)
    (observation_dir/"ranking.csv").write_text(buf.getvalue(),encoding="utf8")
    # Preserve raw issuer evidence and unmatched ACWI rows for alias review.
    raw_buf=io.StringIO()
    original_fields=["vendor_rank","vendor_ticker","market_cap_usd","vendor_company_name","vendor_country"]
    raw_writer=csv.DictWriter(raw_buf,fieldnames=original_fields)
    raw_writer.writeheader();raw_writer.writerows(vendor)
    (observation_dir/"raw_vendor.csv").write_text(raw_buf.getvalue(),encoding="utf8")
    matched_tickers={x["symbol"] for x in matches}
    unmatched_rows=[{"symbol":x["symbol"],"company_name":x["company_name"],
                    "country":x["country"]} for x in etf if x["symbol"] not in matched_tickers]
    out_buf=io.StringIO()
    unmatched_writer=csv.DictWriter(out_buf,fieldnames=["symbol","company_name","country"])
    unmatched_writer.writeheader();unmatched_writer.writerows(unmatched_rows)
    (observation_dir/"unmatched_issuers.csv").write_text(out_buf.getvalue(),encoding="utf8")
    (observation_dir/"source.json").write_text(json.dumps(coverage,indent=2,sort_keys=True)+"\n")
    coverage["latest_observation_path"]=str(observation_dir.relative_to(ROOT))
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
