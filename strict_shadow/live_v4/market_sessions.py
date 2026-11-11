"""Exchange-local *conservative* EOD completeness gates for free Yahoo data.

This is NOT an official holiday/early-close calendar. A reported daily bar is
never considered final before the normal full-session closing auction + buffer;
a session is never fabricated if the provider has no bar. This gate is only
about the time an existing bar can enter the LIVE dataset, not about tradability.

Exchange hours verified against exchange sites (2026):
JPX normal stock auction 15:30 JST, HKEX CAS latest 16:10 HKT, Saudi
Tadawul closing-auction/last trade 15:20 AST; see docs in audit reports.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

UTC=dt.timezone.utc

@dataclass(frozen=True)
class Market:
    zone: str
    close_hour: int
    close_minute: int
    settlement_buffer_minutes: int = 60

# These are CONSERVATIVE normal-session end times; half days are deliberately
# released late. The existence of a session is established by vendor bars.
MARKETS={
    '.T': Market('Asia/Tokyo',15,30,60),
    '.HK': Market('Asia/Hong_Kong',16,10,60),
    '.KS': Market('Asia/Seoul',15,30,60),
    '.KQ': Market('Asia/Seoul',15,30,60),
    '.TW': Market('Asia/Taipei',13,30,60),
    '.L': Market('Europe/London',16,35,60),
    '.SW': Market('Europe/Zurich',17,30,60),
    '.TO': Market('America/Toronto',16,0,90),
    '.V': Market('America/Toronto',16,0,90),
    '.AS': Market('Europe/Amsterdam',17,40,60),
    '.PA': Market('Europe/Paris',17,40,60),
    '.BR': Market('Europe/Brussels',17,40,60),
    '.DE': Market('Europe/Berlin',17,40,60),
    '.F': Market('Europe/Berlin',17,40,60),
    '.AX': Market('Australia/Sydney',16,10,60),
    '.BO': Market('Asia/Kolkata',15,30,60),
    '.NS': Market('Asia/Kolkata',15,30,60),
    '.ST': Market('Europe/Stockholm',17,30,60),
    '.CO': Market('Europe/Copenhagen',17,0,60),
    '.OL': Market('Europe/Oslo',16,30,60),
    '.HE': Market('Europe/Helsinki',18,30,60),
    '.MI': Market('Europe/Rome',17,40,60),
    '.MC': Market('Europe/Madrid',17,40,60),
    '.SI': Market('Asia/Singapore',17,10,60),
    '.SR': Market('Asia/Riyadh',15,20,60),
    '.SA': Market('America/Sao_Paulo',17,0,90),
}
US=Market('America/New_York',16,0,90)
FX=Market('America/New_York',17,0,30)
_US_DOT_SUFFIXES={'.A','.B','.C'}

def venue(symbol:str)->tuple[str,Market]:
    symbol=str(symbol).upper().removeprefix('G::')
    if symbol.endswith('=X'):
        return 'FOREX_NY_ROLLOVER',FX
    for suffix,market in sorted(MARKETS.items(),key=lambda p:-len(p[0])):
        if symbol.endswith(suffix):return suffix,market
    if '.' in symbol and not any(symbol.endswith(x) for x in _US_DOT_SUFFIXES):
        raise ValueError('Unknown market suffix, no safe local-close clock: '+symbol)
    return 'USA',US

def release_time_utc(symbol:str,session:dt.date)->dt.datetime:
    if not isinstance(session,dt.date) or isinstance(session,dt.datetime):
        session=dt.date.fromisoformat(str(session)[:10])
    name,market=venue(symbol)
    # Sunday FX candles are not a full daily close on Sunday night.
    # In the Yahoo convention a Sunday session can only be complete on Monday.
    if name=='FOREX_NY_ROLLOVER' and session.weekday() in (5,6):
        session+=dt.timedelta(days=(7-session.weekday())%7)
    local=dt.datetime.combine(session,dt.time(market.close_hour,market.close_minute),
                              tzinfo=ZoneInfo(market.zone))
    return (local+dt.timedelta(minutes=market.settlement_buffer_minutes)).astimezone(UTC)

def is_final_bar(symbol:str,session:dt.date,observed_utc:dt.datetime)->bool:
    if observed_utc.tzinfo is None:
        raise ValueError('A timezone-aware observation timestamp is required')
    return observed_utc.astimezone(UTC)>=release_time_utc(symbol,session)

def normalize_currency(raw_currency:str)->tuple[str,float]:
    """Yahoo GBp/GBX denotes pence, not pounds: 100 GBp = 1 GBP."""
    raw=str(raw_currency or '').strip()
    if raw in ('GBp','GBX','GBPENCE','GBp.'):
        return 'GBP',0.01
    val=raw.upper()
    if len(val)!=3 or not val.isalpha():
        raise ValueError('Unknown provider quote currency: '+repr(raw_currency))
    return val,1.0
