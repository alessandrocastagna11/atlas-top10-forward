"""Research-only exact volatility cache and parity verification."""
import math
import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

def make_estimator(close_eur, original):
    vol_panel = close_eur.pct_change(fill_method=None).rolling(63, min_periods=42).std()
    # The original computes the median across the whole panel, not only selected names.
    row_median = vol_panel.median(axis=1)
    col = {t:i for i,t in enumerate(close_eur.columns)}
    def estimate(date,tickers,prices,lookback):
        if prices is not close_eur:
            return original(date,tickers,prices,lookback)
        vol=vol_panel.loc[date]
        safe_vol=vol.reindex(tickers).replace(0.0,np.nan).fillna(row_median.loc[date]).clip(lower=0.005)
        raw=(1.0/safe_vol).replace([np.inf,-np.inf],np.nan).fillna(0.0)
        if raw.sum()<=0.0:
            raw=pd.Series(1.0,index=tickers)
        relative=raw/raw.sum()
        location=prices.index.get_loc(date)
        start=max(0,location-lookback+1)
        returns=prices.iloc[start:location+1,[col[t] for t in tickers]].pct_change(fill_method=None).dropna()
        if len(returns)>=42:
            covariance=LedoitWolf().fit(returns.to_numpy()).covariance_*252.0
        else:
            covariance=np.diag(np.square(safe_vol.to_numpy()*math.sqrt(252.0)))
        risky_vol=float(math.sqrt(max(0.0,relative.to_numpy()@covariance@relative.to_numpy())))
        return risky_vol,relative,covariance
    return estimate

def check_parity(original):
    rng=np.random.default_rng(1772)
    idx=pd.bdate_range('1990-01-01',periods=390)
    frame=pd.DataFrame(100*np.exp(np.cumsum(rng.normal(.0004,.015,(len(idx),12)),axis=0)),index=idx,columns=list('ABCDEFGHIJKL')).astype('float32')
    frame.loc[idx[40:100],'C']=np.nan
    frame.loc[idx[200:250],'H']=np.nan
    frame['L']=100.0
    cached=make_estimator(frame,original)
    checks=0
    for day in idx[[20,42,64,110,150,205,249,300,389]]:
        for names in [['B'],['A','C','L'],['D','H','B','I','A'],list('KJIHGFEDCB')]:
            for lookback in [42,126]:
                a=original(day,names,frame,lookback)
                b=cached(day,names,frame,lookback)
                np.testing.assert_allclose(a[0],b[0],rtol=1e-9,atol=1e-12,equal_nan=True)
                pd.testing.assert_series_equal(a[1],b[1],check_exact=False,rtol=1e-9,atol=1e-12)
                np.testing.assert_allclose(a[2],b[2],rtol=1e-9,atol=1e-12,equal_nan=True)
                checks+=1
    print('CACHE_PARITY_OK',checks,flush=True)
