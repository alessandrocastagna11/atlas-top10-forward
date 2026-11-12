#!/usr/bin/env python3
"""ATLAS ETF Global V2.3 live EOD signal engine (stdlib only).

- No broker connection; produces suggested actions only.
- Replays the V2.3 core/satellite state machine on a real global-equity ETF.
- Uses previous completed month for credit/breadth/rates stress inputs.
- Online providers: Yahoo chart JSON, FRED CSV, Ken French ZIP files.
- Successful downloads are cached locally; if online providers fail, the last cache is used.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = Path(__file__).resolve().parent
CACHE = ROOT / "live_cache"
OUT = ROOT / "live_output"
DEFAULT_CONFIG = LIVE_DIR / "live_config.json"
CACHE.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

UA = "Mozilla/5.0 ATLAS-ETF-V2.3-Live/1.0"


def fetch_bytes(url: str, timeout: int = 45) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def safe_ticker_name(ticker: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in ticker)


def parse_iso_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def previous_month_key(date_str: str) -> str:
    y, m = int(date_str[:4]), int(date_str[5:7]) - 1
    if m == 0:
        y -= 1
        m = 12
    return f"{y:04d}-{m:02d}"


def add_months(month_key: str, delta: int) -> str:
    y, m = int(month_key[:4]), int(month_key[5:7]) + delta
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def percentile(values, q):
    xs = sorted(x for x in values if math.isfinite(x))
    if not xs:
        return math.nan
    pos = (len(xs) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def sma(a, i, n):
    if i < n - 1:
        return math.nan
    return sum(a[i - n + 1 : i + 1]) / n


def rolling_mean(a, i, n):
    j = max(0, i - n + 1)
    xs = a[j : i + 1]
    return sum(xs) / len(xs)


def mom(a, i, n):
    if i < n or a[i - n] <= 0:
        return math.nan
    return a[i] / a[i - n] - 1.0


def yahoo_daily(ticker: str, start_date: str, cfg: dict) -> list[dict]:
    cache_path = CACHE / f"yahoo_{safe_ticker_name(ticker)}.csv"
    start_epoch = int(parse_iso_date(start_date).replace(tzinfo=timezone.utc).timestamp())
    end_epoch = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
    enc = urllib.parse.quote(ticker, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{enc}"
        f"?period1={start_epoch}&period2={end_epoch}&interval=1d&events=div%2Csplits"
        f"&includeAdjustedClose=true"
    )
    rows = []
    try:
        obj = json.loads(fetch_bytes(url).decode("utf-8"))
        result = obj["chart"]["result"][0]
        ts = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        adj = ((result["indicators"].get("adjclose") or [{}])[0].get("adjclose") or [])
        close = quote.get("close") or []
        for i, t in enumerate(ts):
            p = adj[i] if i < len(adj) and adj[i] is not None else (close[i] if i < len(close) else None)
            if p is None or not math.isfinite(float(p)) or float(p) <= 0:
                continue
            ds = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
            rows.append({"date": ds, "close": float(p)})
        if len(rows) < 160:
            raise RuntimeError(f"Too few Yahoo rows for {ticker}: {len(rows)}")
        with cache_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "close"])
            w.writeheader(); w.writerows(rows)
    except Exception as e:
        if not cache_path.exists():
            raise RuntimeError(f"Yahoo download failed for {ticker} and no cache exists: {e}") from e
        with cache_path.open("r", encoding="utf-8", newline="") as f:
            rows = [{"date": r["date"], "close": float(r["close"])} for r in csv.DictReader(f)]
        print(f"WARNING: Yahoo unavailable for {ticker}; using cache {cache_path.name}: {e}")

    # Avoid using an unfinished same-day bar before a conservative local cutoff.
    try:
        local_now = datetime.now(ZoneInfo(cfg.get("timezone", "Europe/Rome")))
        cutoff = int(cfg.get("drop_today_before_local_hour", 18))
        if rows and rows[-1]["date"] == local_now.date().isoformat() and local_now.hour < cutoff:
            rows = rows[:-1]
    except Exception:
        pass
    return rows


def fred_monthly(series_id: str) -> dict[str, float]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}"
    raw = fetch_bytes(url).decode("utf-8-sig", errors="replace")
    out = {}
    for r in csv.DictReader(io.StringIO(raw)):
        ds = (r.get("DATE") or r.get("observation_date") or "").strip()
        val = (r.get(series_id) or "").strip()
        if not ds or val in ("", "."):
            continue
        try:
            out[ds[:7]] = float(val)
        except ValueError:
            pass
    if not out:
        raise RuntimeError(f"No FRED data for {series_id}")
    return out


def french_zip_text(url: str) -> str:
    raw = fetch_bytes(url, timeout=60)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".csv", ".txt"))]
        if not names:
            names = z.namelist()
        return z.read(names[0]).decode("utf-8-sig", errors="replace")


def french_daily_rf_monthly() -> dict[str, float]:
    txt = french_zip_text("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip")
    prod = {}
    for line in txt.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 5 or len(p[0]) != 8 or not p[0].isdigit():
            continue
        try:
            rf = float(p[4]) / 100.0
        except ValueError:
            continue
        k = f"{p[0][:4]}-{p[0][4:6]}"
        prod[k] = prod.get(k, 1.0) * (1.0 + rf)
    return {k: (v ** 12 - 1.0) * 100.0 for k, v in prod.items()}


def french_six_monthly() -> dict[str, list[float]]:
    txt = french_zip_text("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/6_Portfolios_2x3_CSV.zip")
    out = {}
    in_vw = False
    for line in txt.splitlines():
        if "Average Value Weighted Returns -- Monthly" in line:
            in_vw = True
            continue
        if not in_vw:
            continue
        if "Average Equal Weighted Returns" in line:
            break
        p = [x.strip() for x in line.split(",")]
        if len(p) < 7 or len(p[0]) != 6 or not p[0].isdigit():
            continue
        try:
            vals = [float(x) / 100.0 for x in p[1:7]]
        except ValueError:
            continue
        if all(v > -0.90 for v in vals):
            out[f"{p[0][:4]}-{p[0][4:6]}"] = vals
    if not out:
        raise RuntimeError("No Ken French 6-portfolios monthly data")
    return out


def load_macro(cfg: dict, synthetic: bool = False) -> dict:
    cache_path = CACHE / "macro_v23.json"
    if synthetic:
        return synthetic_macro()
    try:
        aaa = fred_monthly("AAA")
        baa = fred_monthly("BAA")
        gs10 = fred_monthly("GS10")
        rf = french_daily_rf_monthly()
        six = french_six_monthly()
        obj = {"aaa": aaa, "baa": baa, "gs10": gs10, "rf_annualized": rf, "six": six,
               "downloaded_utc": datetime.now(timezone.utc).isoformat()}
        cache_path.write_text(json.dumps(obj), encoding="utf-8")
        return obj
    except Exception as e:
        if not cache_path.exists():
            raise RuntimeError(f"Macro download failed and no cache exists: {e}") from e
        print(f"WARNING: macro providers unavailable; using cache {cache_path.name}: {e}")
        return json.loads(cache_path.read_text(encoding="utf-8"))


def synthetic_macro() -> dict:
    aaa = {}; baa = {}; gs10 = {}; rf = {}; six = {}
    y, m = 2000, 1
    for n in range(360):
        k = f"{y:04d}-{m:02d}"
        aaa[k] = 4.0 + 0.15 * math.sin(n / 19)
        baa[k] = aaa[k] + 1.0 + 0.15 * math.sin(n / 11)
        gs10[k] = 3.5 + 0.5 * math.sin(n / 27)
        rf[k] = 2.0 + 0.25 * math.sin(n / 31)
        six[k] = [0.01 + 0.004 * math.sin(n / (j + 3)) for j in range(6)]
        m += 1
        if m == 13:
            m = 1; y += 1
    return {"aaa": aaa, "baa": baa, "gs10": gs10, "rf_annualized": rf, "six": six,
            "downloaded_utc": "synthetic"}


class StressModel:
    def __init__(self, cfg: dict, macro: dict):
        self.cfg = cfg
        self.aaa = {k: float(v) for k, v in macro["aaa"].items()}
        self.baa = {k: float(v) for k, v in macro["baa"].items()}
        self.gs10 = {k: float(v) for k, v in macro["gs10"].items()}
        self.rf = {k: float(v) for k, v in macro["rf_annualized"].items()}
        self.six = {k: [float(x) for x in v] for k, v in macro["six"].items()}
        self.credit = [(k, self.baa[k] - self.aaa[k]) for k in sorted(self.aaa) if k in self.baa]
        self.cix = {k: i for i, (k, _) in enumerate(self.credit)}
        self.term = [(k, self.gs10[k] - self.rf[k]) for k in sorted(self.gs10) if k in self.rf]
        self.tix = {k: i for i, (k, _) in enumerate(self.term)}

    def credit_signal(self, month: str):
        ix = self.cix.get(month)
        if ix is None:
            return {"flag": False, "spread": math.nan, "p80": math.nan, "change_3m": math.nan}
        start = max(0, ix - int(self.cfg["credit_lookback_months"]) + 1)
        hist = [x[1] for x in self.credit[start : ix + 1]]
        p80 = percentile(hist, float(self.cfg["credit_percentile"])) if len(hist) >= int(self.cfg["stress_min_history_months"]) else math.nan
        ch3 = self.credit[ix][1] - self.credit[ix - 3][1] if ix >= 3 else math.nan
        spread = self.credit[ix][1]
        flag = (math.isfinite(p80) and spread >= p80) or (math.isfinite(ch3) and ch3 >= float(self.cfg["credit_3m_widening_pp"]))
        return {"flag": flag, "spread": spread, "p80": p80, "change_3m": ch3}

    def breadth_signal(self, month: str):
        healthy = 0; valid = 0
        for j in range(6):
            prod = 1.0; ok = True
            for z in range(int(self.cfg["breadth_return_months"])):
                vals = self.six.get(add_months(month, -z))
                if vals is None:
                    ok = False; break
                prod *= 1.0 + vals[j]
            if ok:
                valid += 1
                if prod - 1.0 > 0:
                    healthy += 1
        flag = valid == 6 and healthy <= int(self.cfg["breadth_max_healthy"])
        return {"flag": flag, "healthy": healthy, "valid": valid}

    def rate_signal(self, month: str):
        ix = self.tix.get(month)
        if ix is None:
            return {"flag": False, "term": math.nan, "p20": math.nan}
        start = max(0, ix - int(self.cfg["rates_lookback_months"]) + 1)
        hist = [x[1] for x in self.term[start : ix + 1]]
        p20 = percentile(hist, float(self.cfg["rates_percentile"])) if len(hist) >= int(self.cfg["stress_min_history_months"]) else math.nan
        term = self.term[ix][1]
        return {"flag": math.isfinite(p20) and term <= p20, "term": term, "p20": p20}

    def regime(self, date_str: str):
        month = previous_month_key(date_str)
        c = self.credit_signal(month); b = self.breadth_signal(month); r = self.rate_signal(month)
        score = int(c["flag"]) + int(b["flag"]) + int(r["flag"])
        return {
            "month": month, "score": score,
            "credit": bool(c["flag"]), "breadth": bool(b["flag"]), "rates": bool(r["flag"]),
            "credit_spread": finite_or_none(c["spread"]), "credit_p80": finite_or_none(c["p80"]),
            "credit_change_3m": finite_or_none(c["change_3m"]), "breadth_healthy": b["healthy"],
            "term_spread": finite_or_none(r["term"]), "term_p20": finite_or_none(r["p20"]),
        }


def finite_or_none(x):
    return x if isinstance(x, (int, float)) and math.isfinite(x) else None


@dataclass
class ReplayResult:
    latest: dict
    history: list[dict]
    events: list[dict]


def replay_strategy(rows: list[dict], cfg: dict, stress: StressModel) -> ReplayResult:
    dates = [r["date"] for r in rows]
    px = [float(r["close"]) for r in rows]
    n = len(px)
    if n < 160:
        raise RuntimeError("Need at least 160 daily observations")

    dd=[]; dd5=[]; dd10=[]; s150=[]; bull=[]; r10=[]
    peak=-math.inf
    for i,p in enumerate(px):
        peak=max(peak,p); dd.append(p/peak-1.0)
        dd5.append(rolling_mean(dd,i,5)); dd10.append(rolling_mean(dd,i,10))
        s=sma(px,i,150); s150.append(s)
        bull.append(i>=154 and math.isfinite(s) and p>s and s>s150[i-5])
        r10.append(mom(px,i,10))

    sat_in=True; core_in=True; best=dd[0]; trough=dd10[0]; exit_count=0; bear=False; second_dd=None
    reset_bull=0; core_zero=False; warning=None; gate_active=False
    p_sat=None; p_core_exit=False; p_core_entry=False
    events=[]; hist=[]

    for i in range(n):
        executed=[]
        # Execute prior close's pending instructions on this bar.
        if p_sat == "EXIT" and sat_in:
            sat_in=False; exit_count += 1; trough=dd10[i]; reset_bull=0
            executed.append("SATELLITE_EXIT")
            events.append({"date":dates[i],"event":"SATELLITE_EXIT","exit_count":exit_count,"drawdown":dd[i]})
            if exit_count >= 2:
                bear=True; second_dd=dd[i]; gate_active=True
                if core_in:
                    rg=stress.regime(dates[i])
                    if rg["score"] >= int(cfg["stress_immediate_score"]):
                        p_core_exit=True; gate_active=False
                        events.append({"date":dates[i],"event":"CORE_EXIT_STRESS_2OF3","stress":rg})
                    elif rg["score"] == 1:
                        warning={"start":i,"start_dd":dd[i]}
                        events.append({"date":dates[i],"event":"CORE_WARNING_START","stress":rg})
        elif p_sat == "ENTRY" and not sat_in:
            sat_in=True; best=dd[i]; warning=None; gate_active=False
            executed.append("SATELLITE_ENTRY")
            events.append({"date":dates[i],"event":"SATELLITE_ENTRY","drawdown":dd[i]})
        p_sat=None

        if p_core_exit and core_in:
            core_in=False; core_zero=True; p_core_exit=False; warning=None; gate_active=False
            executed.append("CORE_EXIT")
            events.append({"date":dates[i],"event":"CORE_EXIT","drawdown":dd[i]})
        if p_core_entry and not core_in:
            core_in=True; p_core_entry=False; warning=None; gate_active=False
            executed.append("CORE_ENTRY")
            events.append({"date":dates[i],"event":"CORE_ENTRY","drawdown":dd[i]})

        active=exit_count>0 or bear or not core_in
        reset_bull=(reset_bull+1 if bull[i] else 0) if active else 0
        reset_need=10 if (bear or exit_count>=2) else 5
        if dd[i] >= -1e-12 or (active and reset_bull >= reset_need):
            exit_count=0; bear=False; second_dd=None; reset_bull=0; core_zero=False; warning=None; gate_active=False
            if not core_in:
                p_core_entry=True

        force=math.isfinite(r10[i]) and r10[i] <= -0.10
        if sat_in:
            best=max(best,dd[i]); threshold=0.075 if (exit_count==0 and bull[i]) else 0.05
            if dd[i] <= best-threshold or force:
                p_sat="EXIT"
        else:
            trough=min(trough,dd10[i])
            if exit_count <= 1:
                if dd10[i] >= trough + float(cfg["satellite_rebound"]) and dd5[i] > dd10[i]:
                    p_sat="ENTRY"
            elif bull[i]:
                p_sat="ENTRY"

        if core_in and gate_active:
            rg=stress.regime(dates[i])
            if warning is not None:
                age=i-warning["start"]
                deteriorated=dd[i] <= warning["start_dd"] - float(cfg["warning_extra_drawdown"])
                crash=math.isfinite(r10[i]) and r10[i] <= -0.10
                if rg["score"] >= int(cfg["stress_immediate_score"]):
                    p_core_exit=True; warning=None; gate_active=False
                    events.append({"date":dates[i],"event":"CORE_EXIT_WARNING_STRESS_SIGNAL","stress":rg})
                elif deteriorated or crash:
                    p_core_exit=True; warning=None; gate_active=False
                    events.append({"date":dates[i],"event":"CORE_EXIT_WARNING_PRICE_SIGNAL","stress":rg})
                elif age >= int(cfg["warning_sessions"]):
                    if not sat_in:
                        p_core_exit=True; warning=None; gate_active=False
                        events.append({"date":dates[i],"event":"CORE_EXIT_WARNING_TIMEOUT_SIGNAL","stress":rg})
                    else:
                        warning=None; gate_active=False
            else:
                if rg["score"] >= int(cfg["stress_immediate_score"]):
                    p_core_exit=True; gate_active=False
                    events.append({"date":dates[i],"event":"CORE_EXIT_LATE_STRESS_SIGNAL","stress":rg})
                elif rg["score"] == 1:
                    warning={"start":i,"start_dd":dd[i]}
                    events.append({"date":dates[i],"event":"CORE_WARNING_LATE_START","stress":rg})

        if bear and not core_zero and second_dd is not None and core_in and dd[i] <= second_dd - 0.05:
            p_core_exit=True; warning=None; gate_active=False
            events.append({"date":dates[i],"event":"CORE_EXIT_LEGACY_5PP_SIGNAL","stress":stress.regime(dates[i])})
        if not core_in and bull[i]:
            p_core_entry=True

        rg=stress.regime(dates[i])
        hist.append({
            "date": dates[i], "close": px[i], "market_drawdown": dd[i], "dd5": dd5[i], "dd10": dd10[i],
            "sma150": finite_or_none(s150[i]), "bull": bool(bull[i]), "r10": finite_or_none(r10[i]),
            "core_in": bool(core_in), "satellite_in": bool(sat_in), "exit_count": exit_count,
            "bear": bool(bear), "gate_active": bool(gate_active), "warning_active": warning is not None,
            "stress_score": rg["score"], "executed": ";".join(executed),
        })

    pending=[]
    if p_sat == "EXIT": pending.append("SELL_SATELLITE_NEXT_SESSION")
    if p_sat == "ENTRY": pending.append("BUY_SATELLITE_NEXT_SESSION")
    if p_core_exit: pending.append("SELL_CORE_NEXT_SESSION")
    if p_core_entry: pending.append("BUY_CORE_NEXT_SESSION")
    last_rg=stress.regime(dates[-1])
    latest={
        **hist[-1], "stress": last_rg, "pending_actions": pending,
        "target_equity_fraction": (0.5 if core_in else 0.0) + (0.5 if sat_in else 0.0),
        "state_label": ("CORE+SATELLITE" if core_in and sat_in else "CORE_ONLY" if core_in else "SATELLITE_ONLY" if sat_in else "DEFENSIVE/CASH"),
    }
    return ReplayResult(latest=latest, history=hist, events=events)


def defensive_status(cfg: dict, synthetic: bool = False) -> dict:
    if synthetic:
        return {"ticker":cfg["defensive_ticker"],"available":True,"momentum_126":0.02,"recommendation":"BOND"}
    try:
        rows=yahoo_daily(cfg["defensive_ticker"],cfg["history_start"],cfg)
        vals=[r["close"] for r in rows]
        i=len(vals)-1
        m=mom(vals,i,int(cfg.get("defensive_momentum_sessions",126)))
        return {"ticker":cfg["defensive_ticker"],"available":True,"last_date":rows[-1]["date"],"last_close":vals[-1],
                "momentum_126":finite_or_none(m),"recommendation":"BOND" if math.isfinite(m) and m>0 else "CASH"}
    except Exception as e:
        return {"ticker":cfg["defensive_ticker"],"available":False,"error":str(e),"recommendation":"CASH"}


def synthetic_prices() -> list[dict]:
    rows=[]; p=100.0; d=datetime(2010,1,1)
    for i in range(4300):
        if d.weekday()<5:
            cyc=0.00025 + 0.0035*math.sin(i/61.0)
            # deterministic stress patches
            shock = -0.018 if 2200 < i < 2210 else (-0.006 if 3000 < i < 3040 else 0.0)
            p=max(5.0,p*(1+cyc+shock))
            rows.append({"date":d.date().isoformat(),"close":p})
        d += timedelta(days=1)
    return rows


def write_outputs(cfg: dict, replay: ReplayResult, defensive: dict, data_mode: str):
    latest={"strategy":cfg["strategy_name"],"generated_utc":datetime.now(timezone.utc).isoformat(),
            "data_mode":data_mode,"signal_ticker":cfg["signal_ticker"],"equity_ticker":cfg["equity_ticker"],
            "defensive":defensive,"latest":replay.latest,
            "warning":"Signal-only/paper. No broker connection and no automatic order execution."}
    (OUT/"latest_signal.json").write_text(json.dumps(latest,indent=2),encoding="utf-8")
    hist=replay.history[-260:]
    with (OUT/"signal_history.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(hist[0].keys())); w.writeheader(); w.writerows(hist)
    (OUT/"events.json").write_text(json.dumps(replay.events[-200:],indent=2),encoding="utf-8")
    html=dashboard_html(latest,replay.events[-20:])
    (OUT/"dashboard.html").write_text(html,encoding="utf-8")
    return latest


def esc(x):
    import html
    return html.escape(str(x))


def dashboard_html(obj: dict, events: list[dict]) -> str:
    l=obj["latest"]; s=l["stress"]; d=obj["defensive"]
    acts=l["pending_actions"] or ["NESSUNA AZIONE"]
    rows="".join(f"<tr><td>{esc(e.get('date',''))}</td><td>{esc(e.get('event',''))}</td></tr>" for e in reversed(events))
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>ATLAS V2.3 Live</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:1000px;margin:32px auto;padding:0 18px;background:#111;color:#eee}}.card{{background:#1b1b1b;border:1px solid #333;border-radius:12px;padding:18px;margin:12px 0}}.big{{font-size:32px;font-weight:700}}table{{width:100%;border-collapse:collapse}}td,th{{padding:8px;border-bottom:1px solid #333;text-align:left}}code{{background:#222;padding:2px 6px;border-radius:4px}}</style></head><body>
<h1>ATLAS ETF Global V2.3 · Live EOD</h1><div class='card'><div>Ultima barra completa</div><div class='big'>{esc(l['date'])}</div><p>{esc(obj['signal_ticker'])} · close {l['close']:.2f} · DD {100*l['market_drawdown']:.2f}%</p></div>
<div class='card'><h2>Stato</h2><div class='big'>{esc(l['state_label'])}</div><p>Target equity: {100*l['target_equity_fraction']:.0f}% · Bull: {esc(l['bull'])} · Exit count: {esc(l['exit_count'])}</p><p>Gate stress: {esc(l['gate_active'])} · Warning: {esc(l['warning_active'])}</p></div>
<div class='card'><h2>Azioni per la prossima seduta</h2><p class='big'>{'<br>'.join(esc(a) for a in acts)}</p><p>Difensivo: {esc(d.get('recommendation'))} ({esc(d.get('ticker'))})</p></div>
<div class='card'><h2>Stress macro · mese {esc(s['month'])}</h2><p>Score: <b>{esc(s['score'])}/3</b> · Credit {esc(s['credit'])} · Breadth {esc(s['breadth'])} · Rates {esc(s['rates'])}</p><p>Breadth healthy: {esc(s['breadth_healthy'])}/6 · Credit spread: {esc(s['credit_spread'])} · Term spread: {esc(s['term_spread'])}</p></div>
<div class='card'><h2>Eventi recenti</h2><table><tr><th>Data</th><th>Evento</th></tr>{rows}</table></div>
<p>Signal-only/paper. Nessuna connessione al broker. Generato {esc(obj['generated_utc'])}.</p></body></html>"""


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",default=str(DEFAULT_CONFIG))
    ap.add_argument("--offline-smoke-test",action="store_true")
    args=ap.parse_args()
    cfg=json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.offline_smoke_test:
        prices=synthetic_prices(); macro=load_macro(cfg,synthetic=True); mode="synthetic-offline-smoke-test"
    else:
        prices=yahoo_daily(cfg["signal_ticker"],cfg["history_start"],cfg); macro=load_macro(cfg); mode="online-or-cache"
    stress=StressModel(cfg,macro)
    replay=replay_strategy(prices,cfg,stress)
    defensive=defensive_status(cfg,synthetic=args.offline_smoke_test)
    result=write_outputs(cfg,replay,defensive,mode)
    l=result["latest"]
    print("\nATLAS ETF Global V2.3 LIVE EOD")
    print(f"Bar:       {l['date']}  {cfg['signal_ticker']} close={l['close']:.2f}")
    print(f"State:     {l['state_label']}  target equity={100*l['target_equity_fraction']:.0f}%")
    print(f"DD:        {100*l['market_drawdown']:.2f}%  bull={l['bull']}  stress={l['stress']['score']}/3")
    print("Actions:   " + (", ".join(l["pending_actions"]) if l["pending_actions"] else "NONE"))
    print(f"Defensive: {defensive.get('recommendation')}  {defensive.get('ticker')}")
    print(f"Output:    {OUT}")


if __name__ == "__main__":
    main()
