"""Mathematically equivalent inverse FX recovery, no equity-series stitching."""
from __future__ import annotations
import re
import numpy as np
import pandas as pd

PAIR=re.compile(r'^EUR([A-Z]{3})=X$')

def inverse_symbol(symbol):
    m=PAIR.fullmatch(symbol)
    return m.group(1)+'EUR=X' if m and m.group(1)!='EUR' else None

def validate_inverse(symbol,inverse,primary,expected,*,min_overlap=60):
    """Return a full transformed frame only after empirical overlap validation.
    Same vendor, opposite currency pair; not an independent price source.
    Never append to the existing adjusted series or guess missing FX bars.
    """
    alt=inverse_symbol(symbol)
    if not alt or primary is None or inverse is None:
        raise ValueError('FX inverse requires existing direct-pair evidence')
    cols=['adj_open','adj_close','raw_close']
    for frame in (inverse,primary):
        if not set(cols).issubset(frame) or frame.index.has_duplicates:
            raise ValueError('FX inverse malformed frame')
    if 'quote_currency' not in inverse or set(inverse.quote_currency.dropna())!={'EUR'}:
        raise ValueError('FX inverse currency mismatch')
    ccy=symbol[3:6]
    if 'quote_currency' not in primary or set(primary.quote_currency.dropna())!={ccy}:
        raise ValueError('FX direct currency mismatch')
    inv=inverse.copy(deep=True).sort_index()
    inv=inv.loc[:pd.Timestamp(expected)]
    if len(inv)<300 or inv.index[-1]!=pd.Timestamp(expected):
        raise ValueError('FX inverse incomplete or stale')
    for c in cols:
        values=pd.to_numeric(inv[c],errors='coerce')
        if not np.isfinite(values).all() or not (values>0).all():
            raise ValueError('FX inverse invalid values')
        inv[c]=1.0/values
    dates=primary.index.intersection(inv.index)
    dates=dates[dates<pd.Timestamp(expected)][-120:]
    if len(dates)<min_overlap:raise ValueError('FX inverse insufficient overlap')
    # Daily close timestamps can vary slightly by vendor instrument. Reject
    # systematic or large deviations instead of treating them as equivalent.
    errors=[]
    for c in cols:
        a=pd.to_numeric(primary.loc[dates,c],errors='coerce')
        err=(inv.loc[dates,c]/a-1).abs()
        if not np.isfinite(err).all():raise ValueError('FX inverse invalid overlap')
        errors.extend(err.tolist())
    if max(errors)>.01 or float(np.median(errors))>.002:
        raise ValueError('FX inverse overlap mismatch')
    inv['quote_currency']=ccy
    inv['quote_unit_scale']=1.
    inv['adjustment_source']='FX_RECIPROCAL_VERIFIED_OVERLAP'
    inv['source_symbol']=alt
    inv['inverse_overlap_sessions']=len(dates)
    inv['inverse_max_overlap_error']=max(errors)
    inv['dividend']=0.
    inv['volume']=0.
    return inv
