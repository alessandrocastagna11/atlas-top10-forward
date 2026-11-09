"""Read-only-to-strategy daily PAPER portfolio/reporting, separate from Cohort Proxy.

Never synthesizes fills or NAV values for unobserved market sessions. All holdings
are copied from the exact 35-calendar-day FIFO live simulator after parity audit.
"""
from __future__ import annotations
import csv, datetime as dt, json, math
from pathlib import Path

ORDER_FIELDS=['scenario','strategy','decision_date','date','action','ticker','shares','reference_price_eur',
              'execution_price_eur','reference_value_eur','fee_eur','slippage_bps_per_side',
              'capital_gains_tax_eur','reason']


def to_portfolio(result,model,meta):
    """Snapshot after a verified model replay: never reconstruct fills from prices."""
    date=result.equity.index[-1]
    shares=[]
    invested=0.0
    for ticker,pos in sorted(result.paper_positions.items()):
        if ticker not in model.raw.close_eur.columns:
            raise ValueError('Missing closing quote for held ticker: '+ticker)
        quote=float(model.raw.close_eur.at[date,ticker])
        if not math.isfinite(quote) or quote<=0:
            raise ValueError('Stale/invalid last close for held ticker '+ticker)
        qty=float(pos['shares'])
        lots=pos['lots']
        if abs(sum(float(z['shares']) for z in lots)-qty)>max(1e-7,qty*1e-8):
            raise ValueError('FIFO lot inventory does not reconcile for '+ticker)
        value=qty*quote
        invested+=value
        eligible=sum(float(z['shares']) for z in lots if dt.date.fromisoformat(z['sale_eligible_date'])<=date.date())
        shares.append({'ticker':ticker,'market_symbol':meta['ticker_to_market_symbol'].get(ticker,ticker),
                       'shares':qty,'cost_basis_eur':round(float(pos['cost_basis_eur']),4),
                       'market_close_eur':round(quote,8),'market_value_eur':round(value,4),
                       'portfolio_weight_pct':None,
                       'sell_eligible_shares':round(eligible,10),
                       'locked_shares':round(max(0,qty-eligible),10),
                       'lots':lots})
    cash=float(result.paper_cash_eur)
    marked_nav=cash+invested
    if marked_nav<=0 or not math.isfinite(marked_nav):raise ValueError('Invalid mark-to-market NAV')
    for pos in shares:pos['portfolio_weight_pct']=round(pos['market_value_eur']/marked_nav*100,3)
    net_liquidation=float(result.equity.iloc[-1])
    if not math.isfinite(net_liquidation):raise ValueError('Invalid net-liquidation NAV')
    # Net hypothetical liquidation NAV can differ from mark-to-market due to unrealized tax.
    # Do not interpret the hypothetical end-sale as a real order or sell-eligibility override.
    out={'schema_version':2,'type':'PAPER_ONLY','engine_id':'ATLAS_UNIFIED_REGION_ROTATE_V4_CONFIDENCE_BETA070_2026-10-07',
         'variant':'CONFIDENCE70_ORIGINAL_H4_STRICT_35_CALENDAR_DAYS_EVERY_LOT',
         'valuation_session':str(date.date()),'data_source_asof':meta.get('roster_source_asof'),
         'universe_source':meta.get('universe_kind'),'universe_market_cap_pit_certified':False,
         'initial_capital_eur':10000.,'cash_eur':round(cash,4),'invested_eur':round(invested,4),
         'nav_mark_to_market_eur':round(marked_nav,4),'nav_net_liquidation_eur':round(net_liquidation,4),
         'pnl_mark_to_market_pct':round((marked_nav/10000-1)*100,4),
         'positions_count':len(shares),'holdings':shares,'strict_min_holding_calendar_days':35,
         'orders_total':len(result.orders),'buy_orders':int((result.orders.get('action')=='BUY').sum()) if not result.orders.empty else 0,
         'sell_orders':int((result.orders.get('action')=='SELL').sum()) if not result.orders.empty else 0,
         'blocked_or_partial_sell_requests':int(result.blocked_sell_count),
         'blocked_notional_eur_repeated_requests':round(float(result.blocked_sell_notional_eur),4),
         'real_money_orders':0}
    if len(shares)>11:raise ValueError('Position cap violated')
    return out


def initial_portfolio():
    return {'schema_version':2,'type':'PAPER_ONLY','variant':'CONFIDENCE70_ORIGINAL_H4_STRICT_35_CALENDAR_DAYS_EVERY_LOT',
            'valuation_session':None,'initial_capital_eur':10000.,'cash_eur':10000.,'invested_eur':0.,
            'nav_mark_to_market_eur':10000.,'nav_net_liquidation_eur':10000.,'pnl_mark_to_market_pct':0.,
            'positions_count':0,'holdings':[],'strict_min_holding_calendar_days':35,
            'orders_total':0,'buy_orders':0,'sell_orders':0,'real_money_orders':0,
            'universe_market_cap_pit_certified':False}


def write_report(out_dir,status,portfolio,orders):
    """Only present confirmed ledger; stale data explicitly labelled."""
    out_dir=Path(out_dir);out_dir.mkdir(parents=True,exist_ok=True)
    source=status.get('source') or {}
    status_text=status.get('status','PENDING_DATA')
    asof=portfolio.get('valuation_session')
    if status_text!='PAPER_LEDGER_VERIFIED':
        recency='INITIAL_CASH_ONLY_NOT_MARKET_VALUED' if not asof else 'LAST_VERIFIED_PORTFOLIO_NOT_NEW_VALUATION'
    else:
        recency='CURRENT_VERIFIED_MODEL_VALUATION'
    def money(value):return f"{float(value or 0):,.2f} €"
    lines=[
        '# Atlas V4 Confidence70 — paper originale, vincolo 35 giorni',
        '',f"**Stato:** `{status_text}` — **Validità della valutazione:** `{recency}`",
        '',f"**Ultima sessione decisionale:** {status.get('decision_session') or 'non disponibile'}",
        f"**Ultima sessione valorizzata:** {asof or 'non ancora avviata'}",
        f"**Dati mercato / universo:** {source.get('universe_kind') or 'in attesa'}; fotografia registrata il {source.get('roster_recorded_date') or 'n/d'}",
        '**Attenzione:** l’universo ACWI attuale è un proxy, non la classifica originale PIT per capitalizzazione.',
        '',
        '| Portafoglio virtuale | Importo |', '|---|---:|',
        f"| Capitale iniziale | {money(portfolio.get('initial_capital_eur',10000))} |",
        f"| NAV netto liquidabile ipotetico* | {money(portfolio.get('nav_net_liquidation_eur',10000))} |",
        f"| NAV a prezzi di mercato | {money(portfolio.get('nav_mark_to_market_eur',10000))} |",
        f"| Liquidità | {money(portfolio.get('cash_eur',10000))} |",
        f"| Valore titoli | {money(portfolio.get('invested_eur',0))} |",
        f"| Posizioni | {portfolio.get('positions_count',0)} |",
        f"| Ordini complessivi | {portfolio.get('orders_total',0)} |",
        f"| Nuovi ordini verificati | {int(status.get('new_orders',0) or 0)} |",
        '',
        '*NAV netto liquidabile = valutazione ipotetica dopo costi e imposte di liquidazione; **non è una vendita eseguita** e non supera il blocco dei 35 giorni.',
        '',
        '## Posizioni e lotti',
        '', '| Titolo | Quote | Valore € | Peso | Quote vincolate | Prima data vendibile |',
        '|---|---:|---:|---:|---:|---|',
    ]
    for p in portfolio.get('holdings',[]):
        locked=[l['sale_eligible_date'] for l in p.get('lots',[]) if l['sale_eligible_date']>(asof or '9999-12-31')]
        lines.append(f"| {p['ticker']} | {p['shares']:.6g} | {p['market_value_eur']:,.2f} | {p['portfolio_weight_pct']:.2f}% | {p['locked_shares']:.6g} | {min(locked) if locked else 'già vendibile'} |")
    if not portfolio.get('holdings'):lines.append('| — Nessuna posizione | — | — | — | — | — |')
    lines.extend(['','## Ultime operazioni verificate','','| Seduta | Tipo | Titolo | Quote | Prezzo € | Causale |', '|---|---|---|---:|---:|---|'])
    for o in orders[-15:][::-1]:
        lines.append(f"| {str(o.get('date',''))[:10]} | {o.get('action','')} | {o.get('ticker','')} | {float(o.get('shares',0)):.6g} | {float(o.get('execution_price_eur',0)):.4f} | {o.get('reason','')} |")
    if not orders:lines.append('| — Nessuna operazione | — | — | — | — | — |')
    lines+=['','**Politica ordini:** minimo 35 giorni CALENDARIO su ogni lotto, anche vendite difensive; controlli di rischio settimanali e rotazione ogni 5 settimane; nessun ordine reale.','',
            'Il workflow aggiorna i dati e verifica il modello, ma non garantisce che ogni giorno vi sia un acquisto o una vendita.',
            '',f"**Eventuale blocco:** {status.get('reason','nessuno') or 'nessuno'}",'']
    (out_dir/'latest.md').write_text('\n'.join(lines),encoding='utf8')
    with (out_dir/'orders.csv').open('w',encoding='utf8',newline='') as h:
        w=csv.DictWriter(h,fieldnames=ORDER_FIELDS,extrasaction='ignore')
        w.writeheader();w.writerows(orders)
    return recency
