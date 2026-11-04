#!/usr/bin/env python3
"""Atlas Top10 v0.7: reproducible, stateful dual daily paper tracker.

Main strategy MA10 ±1%, challenger MA6/MA12 ±1%, equal 10k EUR separate
virtual accounts; IUSQ benchmark independently purchased at same entry date.

This script can run unattended on a persistent machine (e.g. GitHub Actions).
It DOES NOT schedule itself or send orders. Missing quotes => PENDING_DATA,
no invented execution. Never accepts data from the future.
"""
from __future__ import annotations
import argparse,copy,csv,datetime as dt,hashlib,json,os,traceback
from pathlib import Path
from zoneinfo import ZoneInfo
import engine as E

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'market_data'
REPORTS=ROOT/'reports'
STRATEGIES=ROOT/'strategies'
LABELS=('MA10_Official','MA6_MA12_Challenger')


def atomic_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    E.write_json(path,obj)


def configure_paths(folder):
    E.ROOT=folder
    E.CONFIG=folder/'config.json'
    E.STATE=folder/'state.json'
    E.JOURNAL=folder/'orders.csv'
    E.NAV=folder/'daily_nav.csv'
    E.SOURCES=folder/'quotes_audit.csv'


def snapshot_valid(data,asof):
    if not isinstance(data,dict):raise ValueError('Invalid snapshot')
    for sym,record in data.items():
        if not isinstance(record,dict) or 'bars' not in record:raise ValueError('Missing bars: '+sym)
        if any(d>asof for d in record['bars']):
            # Keep raw historical snapshot but NEVER use bars later than asof.
            record['bars']={d:x for d,x in record['bars'].items() if d<=asof}
        record['dividends']={d:x for d,x in record.get('dividends',{}).items() if d<=asof}
        record['splits']={d:x for d,x in record.get('splits',{}).items() if d<=asof}
    return data


def fetch_data(asof):
    cfg=json.loads((STRATEGIES/LABELS[0]/'config.json').read_text())
    symbols=[x['symbol'] for x in cfg['basket']]+[cfg['benchmark']['symbol'],'EURUSD=X','EURSAR=X']
    # After December membership is available, add the new year-end symbols.
    for key in LABELS:
        folder=STRATEGIES/key/'annual_rosters'/f'{int(asof[:4])-1}.json'
        if asof[:7]>='2027-02' and folder.exists():
            symbols+=json.loads(folder.read_text())['symbols']
    data={}
    for sym in dict.fromkeys(symbols):
        data[sym]=E.fetch_yahoo(sym,start='2025-09-01',end=asof)
    return snapshot_valid(data,asof)


def dates_between(last,asof):
    x=dt.date.fromisoformat(last)+dt.timedelta(days=1)
    while x<=dt.date.fromisoformat(asof):
        yield x.isoformat()
        x+=dt.timedelta(days=1)


def regenerate(folder,cfg,state):
    configure_paths(folder)
    E.replace_rows(E.NAV,E.NAVFIELDS,state.get('nav_history',[]))
    E.replace_rows(E.JOURNAL,E.FIELDS,state.get('order_history',[]))
    E.replace_rows(E.SOURCES,E.AUDITFIELDS,state.get('quote_history',[]))
    E.render_dashboard(cfg,state)
    E.replace_rows(folder/'dividend_history.csv',
                   ['date','symbol','gross_eur','tax_eur','net_eur','tax_model'],
                   state.get('dividend_history',[]))
    E.replace_rows(folder/'corporate_actions.csv',
                   ['date','symbol','type','ratio','shares_before','shares_after','cost_basis_eur_unchanged'],
                   state.get('corporate_actions',[]))


def price_audit(data,date):
    out=[]
    for sym,record in sorted(data.items()):
        try:
            d,p=E.latest(record['bars'],date)
            out.append(dict(asof_date=date,symbol=sym,bar_date=d,close=p,currency=record.get('currency',''),feed=record.get('feed','')))
        except (ValueError,KeyError):
            pass
    return out


def do_strategy(folder,asof,data):
    configure_paths(folder)
    cfg=json.loads(E.CONFIG.read_text())
    state=json.loads(E.STATE.read_text())
    last_start=state['last_update']
    attempts=[]
    new=[]
    for date in dates_between(last_start,asof):
        pending_before=copy.deepcopy(state)
        try:
            orders,nav=E.step(state,cfg,data,date)
            if nav is None:continue
            state.setdefault('nav_history',[]).append(nav)
            state.setdefault('order_history',[]).extend(orders)
            state.setdefault('quote_history',[]).extend(price_audit(data,date))
            new.append({'date':date,'orders':len([o for o in orders if o['symbol']!=cfg['benchmark']['symbol']]),
                        'status':nav.get('source_status','')})
        except (KeyError,ValueError,RuntimeError) as ex:
            # Important: do NOT commit partially mutated state after failure.
            state=pending_before
            attempts.append({'date':date,'reason':str(ex)[:350]})
            if state['initialized']:
                # Cannot mark subsequent days accurately without earlier market
                # data. Keep last verified NAV; return PENDING_DATA.
                break
            # Before initial entry, it is valid to search next eligible common
            # close. Never make up a trade at missing prices.
    if new:
        atomic_json(E.STATE,state)
    regenerate(folder,cfg,state)
    nav=state['nav_history'][-1]
    prior=state['nav_history'][-2] if len(state['nav_history'])>1 else nav
    def dret(f):
        return round((float(nav[f])/float(prior[f])-1)*100,5) if float(prior[f]) else 0
    stocks=[o for o in state.get('order_history',[]) if o['symbol']!=cfg['benchmark']['symbol']]
    buys=sum(o['side']=='BUY' for o in stocks)
    sells=sum(o['side']=='SELL' for o in stocks)
    today_orders=[o for o in stocks if o['date']==asof]
    audit_today=price_audit(data,asof)
    freshness='EXACT' if audit_today and all(x['bar_date']==asof for x in audit_today) else 'VALUATION_WITH_STALE_QUOTES'
    out={
       'price_freshness':freshness,'engine':cfg['name'],'asof_requested':asof,'last_verified_valuation':nav['date'],
       'report_status': ('PENDING_FIRST_REAL_CLOSE' if not state['initialized'] and not attempts else ('COMPLETE' if state['initialized'] and nav['date']==asof and not attempts else 'PENDING_DATA')),
       'source_status':nav.get('source_status',''),
       'initialized':state['initialized'],'virtual_capital_start_eur':cfg['initial_capital_eur'],
       'portfolio_eur':nav['portfolio_nav_eur'],'benchmark_eur':nav['benchmark_nav_eur'],
       'portfolio_day_pct':dret('portfolio_nav_eur'),'benchmark_day_pct':dret('benchmark_nav_eur'),
       'portfolio_cumulative_pct':nav['portfolio_return_pct'],'benchmark_cumulative_pct':nav['benchmark_return_pct'],
       'excess_pp':nav['excess_return_pp'],'regime':nav['regime'],
       'cash_eur':nav['cash_eur'],'open_positions':nav['positions'],
       'order_count':len(stocks),'buys':buys,'sells':sells,'orders_on_asof':len(today_orders),
       'total_fees_eur':round(sum(float(o['commission_eur']) for o in stocks),2),
       'realized_gain_tax_eur':round(sum(float(o['tax_eur']) for o in stocks),2),
       'bollo_accrued_eur':round(float(state.get('liabilities_eur',0)),2),
       'dividend_tax_eur':round(sum(float(x['tax_eur']) for x in state.get('dividend_history',[])),2),
       'total_slippage_eur':round(sum(float(o['slippage_eur']) for o in stocks),2),
       'pending_transition':state.get('pending_transition'),
       'data_warning':state.get('data_warning'),
       'new_valuations':len(new),'execution_attempts_blocked':attempts[:8],
       'asof_price_audit':audit_today,
       'last_update_source':'Yahoo daily chart (delayed), provider time stamped; cash 0%'
    }
    return out


def render_report(asof,reports,data_hash):
    md=[f'# Atlas Top 10 — report EOD {asof}','',
      '**Capitale virtuale:** 10.000 € per ciascuna simulazione; nessun ordine reale.',
      '**Benchmark:** IUSQ.DE (MSCI ACWI ETF in EUR), conto virtuale indipendente.',
      '**Prezzi:** Yahoo daily end-of-day; nessuna esecuzione con prezzi ipotetici.',
      f'**Impronta snapshot:** `{data_hash}`.','',
      '| Strategia | Stato | NAV € | IUSQ € | Cum. % | IUSQ cum. % | Excess pp | Acquisti | Vendite |',
      '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for key,r in reports.items():
      md.append(f"| {key} | {r['report_status']} / {r['regime']} | {r['portfolio_eur']:,.2f} | {r['benchmark_eur']:,.2f} | {r['portfolio_cumulative_pct']:.2f} | {r['benchmark_cumulative_pct']:.2f} | {r['excess_pp']:+.2f} | {r['buys']} | {r['sells']} |")
    md+=['','**Contabilità:** commissione fissa 1 € per BUY/SELL; slippage 0,1% per lato; imposta 26% sulle plusvalenze realizzate con minusvalenze fino a 4 anni; bollo titoli 0,2% annuo maturato; dividendi esteri con proxy 26% (non sostituisce regime reale).',
      '**Nessun anticipo imposte** sulle plusvalenze latenti nel NAV. Il benchmark è un ETF ad accumulazione e ha tassazione diversa fino alla liquidazione.',
      '**Ordini di benchmark non inclusi nel conteggio della strategia.**','']
    for k,r in reports.items():
      md += [f'## {k}',
        f"- Ultima data NAV verificabile: **{r['last_verified_valuation']}**, richiesta **{asof}**; stato: **{r['report_status']}**; freschezza quotazioni: **{r['price_freshness']}**.",
        f"- Patrimonio {r['portfolio_eur']:,.2f} €, benchmark {r['benchmark_eur']:,.2f} €; variazione dall'ultima osservazione: {r['portfolio_day_pct']:+.3f}% vs {r['benchmark_day_pct']:+.3f}%.",
        f"- Operazioni: {r['buys']} acquisti + {r['sells']} vendite = {r['order_count']} ordini; nella data richiesta: {r['orders_on_asof']}.",
        f"- Commissioni accumulate {r['total_fees_eur']:.2f} €; slippage {r['total_slippage_eur']:.2f} €; imposte plusvalenze {r['realized_gain_tax_eur']:.2f} €; imposte dividendi proxy {r['dividend_tax_eur']:.2f} €; bollo maturato {r['bollo_accrued_eur']:.2f} €."]
      if r['execution_attempts_blocked']:
        md.append('- **Dati mancanti/esecuzione bloccata:** '+ '; '.join(f"{x['date']}: {x['reason']}" for x in r['execution_attempts_blocked'][:3]))
      if r['pending_transition']:
        md.append('- **Cambio regime in attesa di esecuzione:** '+json.dumps(r['pending_transition'],ensure_ascii=False))
      if r['data_warning']:
        md.append('- **Ranking annuale non confermato:** '+r['data_warning'])
      md.append('')
    md.append('**Avvertenza:** se ultima data NAV è precedente alla richiesta, il valore è storico e NON rappresenta una nuova quotazione giornaliera.')
    (REPORTS/'latest.md').write_text('\n'.join(md)+'\n',encoding='utf8')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--asof',default=dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat(),
                    help='end-of-day date YYYY-MM-DD (default: current America/New_York date)')
    ap.add_argument('--fixture',help='SYNTHETIC TEST DATA ONLY; produces no official updates')
    ap.add_argument('--snapshot',help='User-supplied external JSON; emits UNVERIFIED_EXTERNAL flag, use for research')
    ap.add_argument('--check',action='store_true',help='validate portfolio files without downloading')
    ap.add_argument('--seed-report',action='store_true',help='write truthful zero-order report without downloading')
    args=ap.parse_args()
    REPORTS.mkdir(exist_ok=True)
    if args.asof>'2026-11-03' and args.asof>dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat():
        raise SystemExit('Refusing future date')
    if args.check:
        for key in LABELS:
            d=STRATEGIES/key; state=json.loads((d/'state.json').read_text());cfg=json.loads((d/'config.json').read_text())
            assert state['order_count']==sum(o['symbol']!=cfg['benchmark']['symbol'] for o in state['order_history'])
            assert all(float(x['shares'])>=0 for x in state['positions'].values())
            assert state['last_update']==state['nav_history'][-1]['date']
            print(key,'PASS',state['last_update'],state['order_count'])
        return
    if args.seed_report:
        initial={}
        for key in LABELS:
            d=STRATEGIES/key;cfg=json.loads((d/'config.json').read_text());st=json.loads((d/'state.json').read_text())
            regenerate(d,cfg,st)
            n=st['nav_history'][-1]
            initial[key]={'report_status':'PENDING_FIRST_REAL_CLOSE','regime':'RISK_ON' if st['risk_on'] else 'PENDING_DUAL_SIGNAL',
              'portfolio_eur':float(n['portfolio_nav_eur']),'benchmark_eur':float(n['benchmark_nav_eur']),
              'portfolio_cumulative_pct':float(n['portfolio_return_pct']),'benchmark_cumulative_pct':float(n['benchmark_return_pct']),
              'excess_pp':float(n['excess_return_pp']),'buys':0,'sells':0}
        REPORTS.mkdir(exist_ok=True)
        md=['# Atlas Top 10 — seed 03/11/2026','', '**Solo capitale VIRTUALE, nessun ordine realizzato.**','',
            '| Strategia | Patrimonio iniziale € | Benchmark € | Ordini | Stato |',
            '|---|---:|---:|---:|---|']
        for key,r in initial.items(): md.append(f"| {key} | {r['portfolio_eur']:,.2f} | {r['benchmark_eur']:,.2f} | 0 | {r['report_status']} |")
        md.extend(['', 'Nessun prezzo del 04/11/2026 e nessuna compravendita sono stati simulati.',
                   'Il primo aggiornamento effettivo richiede quotazioni giornaliere pubblicate e i cambi EUR/USD ed EUR/SAR.'])
        (REPORTS/'latest.md').write_text('\n'.join(md)+'\n',encoding='utf8')
        atomic_json(REPORTS/'latest_status.json',{'date':'2026-11-03','status':'PENDING_FIRST_REAL_CLOSE','results':initial})
        from master_dashboard import update as refresh_master
        refresh_master()
        print('seed report generated; 0 trades in both accounts')
        return
    if args.fixture:
        raise SystemExit('Synthetic fixtures are reserved for unit tests; cannot run in production state')
    if args.snapshot:
        raise SystemExit('Manual data snapshots cannot update the official ledger: use the market provider (or a separate test copy).')
    ny_now=dt.datetime.now(ZoneInfo('America/New_York'))
    if args.asof==ny_now.date().isoformat() and ny_now.time()<dt.time(17,0):
        msg={'date':args.asof,'status':'PENDING_US_CLOSE','info':'Wait until at least 17:00 America/New_York; all prices must be EOD', 'portfolio_state':'UNCHANGED'}
        atomic_json(REPORTS/'latest_status.json',msg)
        (REPORTS/'latest.md').write_text(f"# Atlas — mercati ancora aperti\n\nData: {args.asof}. Nessun ordine nuovo: attendere la chiusura USA e la pubblicazione delle quote EOD.\n",encoding='utf8')
        print(json.dumps(msg,ensure_ascii=False));return
    else:
        try:
            data=fetch_data(args.asof)
            source='YAHOO_CHART'
        except Exception as exc:
            # A missing feed must not mutate portfolio positions or NAV.
            result={'date':args.asof,'status':'PENDING_DATA','error':str(exc),
                    'portfolio_state':'UNCHANGED','source':'Yahoo Finance daily chart'}
            atomic_json(REPORTS/'latest_status.json',result)
            (REPORTS/'latest.md').write_text('# Atlas Top10 — aggiornamento sospeso\n\n'
                   f"Data richiesta: {args.asof}. Dati di mercato non disponibili: `{str(exc)[:400]}`.\n\n"
                   'Nessuna operazione registrata; i registri precedenti restano invariati.\n')
            print(json.dumps(result,ensure_ascii=False))
            return
    data_json=json.dumps(data,sort_keys=True,ensure_ascii=False).encode('utf8')
    data_hash=hashlib.sha256(data_json).hexdigest()
    # keep last market snapshot for audit without duplicating it forever
    DATA.mkdir(exist_ok=True)
    atomic_json(DATA/'latest_snapshot.json',{'asof':args.asof,'source':source,'digest_sha256':data_hash,
                'downloaded_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(), 'data':data})
    reports={}
    for key in LABELS:
        reports[key]=do_strategy(STRATEGIES/key,args.asof,data)
    render_report(args.asof,reports,data_hash)
    from master_dashboard import update as refresh_master
    refresh_master()
    output={'asof':args.asof,'source':source,'digest_sha256':data_hash,'results':reports}
    atomic_json(REPORTS/'latest_status.json',output)
    print(json.dumps({k:{q:r[q] for q in ['report_status','portfolio_eur','benchmark_eur','buys','sells','order_count','regime']} for k,r in reports.items()},ensure_ascii=False,indent=2))

if __name__=='__main__': main()