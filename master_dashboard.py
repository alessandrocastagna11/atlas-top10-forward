"""Regenerate a static, combined dashboard from verified portfolio ledgers."""
from pathlib import Path
from html import escape
import json

ROOT=Path(__file__).resolve().parent

def update():
    cards=[]
    for key in ('MA10_Official','MA6_MA12_Challenger'):
        folder=ROOT/'strategies'/key
        state=json.loads((folder/'state.json').read_text())
        nav=state['nav_history'][-1]
        cards.append(f'''<article class="card"><div class="label">{escape(key)}</div>
            <div class="big">€ {float(nav['portfolio_nav_eur']):,.2f}</div>
            <p>{escape(nav['regime'])} · {escape(nav['date'])} · {escape(nav['source_status'])}</p>
            <div class="data"><span>Benchmark IUSQ</span><b>€ {float(nav['benchmark_nav_eur']):,.2f}</b>
            <span>Rendimento</span><b>{float(nav['portfolio_return_pct']):+.2f}%</b>
            <span>Extra-rendimento</span><b>{float(nav['excess_return_pp']):+.2f} pp</b>
            <span>Ordini</span><b>{state['order_count']}</b></div>
            <p><a href="strategies/{key}/dashboard.html">Vedi andamento e titoli</a></p></article>''')
    html='''<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Atlas Top10 — Forward</title><style>
    body{background:#0b1424;color:#e4ebf7;font:16px system-ui;margin:0}
    main{max-width:1040px;margin:35px auto;padding:0 18px}.subtitle,p.note,.label{color:#a7bcd4}
    h1{font-size:2rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(295px,1fr));gap:18px}
    .card{background:#17243a;border:1px solid #33455f;border-radius:16px;padding:23px}
    .big{font-weight:700;font-size:2.3rem;margin-top:12px}.data{display:grid;grid-template-columns:1fr auto;gap:11px}
    .data span{color:#aac2da}a{color:#9bc9ff}p.note{border-top:1px solid #33455f;padding-top:15px;line-height:1.6;font-size:13px}
    </style></head><body><main><p class="subtitle">Atlas Top 10 · 3 novembre 2026 · Forward paper trading</p>
    <h1>Monitoraggio giornaliero</h1><p>Due conti virtuali indipendenti da 10.000 €. Nessun ordine reale.</p>
    <section class="grid">'''+''.join(cards)+'''</section>
    <p class="note">Dati e rendimenti riferiti all'ultima data verificata, non necessariamente alla data odierna. La dashboard si aggiorna solo quando viene eseguito il programma, su computer o servizio persistente; il report ChatGPT non modifica questi file.</p></main></body></html>'''
    (ROOT/'dashboard.html').write_text(html,encoding='utf-8')

if __name__=='__main__':update()