# Atlas Top10 — Backtest v0.8 (close-execution proxy)

## Installazione offline

Richiede Python 3.11+, `pip install numpy pandas scipy matplotlib tabulate`.

`python backtest_v08_proxy.py` per riprodurre i risultati principali (tutti i dati necessari sono in `historical_source/data/`).

`python diagnostics.py` per rifare scenario stress e grafici. `python -m unittest discover -p 'test_*.py' -q` per 4 test automatici.

**Leggere prima `FINAL_REPORT.md`**: non è un test con OHLC open e IUSQ autentico, ranking PIT non certificato. Non apporta modifiche a ordini reali o al paper forward.