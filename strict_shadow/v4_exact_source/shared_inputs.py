"""Versioned shared Atlas decision input: lossless JSON-in-ZIP, no executable
serialization. One rolling payload in Git; exact earlier versions remain in
Git history. The original model publishes only its actual ingested frames,
roster vintages and cash-rate series. The challenger never refetches them.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import zipfile
import numpy as np
import pandas as pd

SCHEMA=1
RELATIVE_PATH='shared_inputs/latest.zip'

def json_default(x):
    if isinstance(x,np.generic): return x.item()
    if isinstance(x,(dt.date,dt.datetime)): return x.isoformat()
    raise TypeError(type(x).__name__)

def enc(x):
    return json.dumps(x,default=json_default,ensure_ascii=False,separators=(',',':'),allow_nan=True).encode('utf8')

def frame_pack(df):
    if not isinstance(df.index,pd.DatetimeIndex):raise ValueError('Expected datetime index')
    if not df.index.is_unique or not df.index.is_monotonic_increasing:raise ValueError('Invalid input dates')
    return {'index':[x.isoformat() for x in df.index],'index_name':df.index.name,
            'columns':df.columns.tolist(),'dtypes':[str(t) for t in df.dtypes],
            'rows':df.astype(object).where(df.notna(),None).values.tolist()}

def frame_unpack(obj):
    ix=pd.DatetimeIndex(obj['index'],name=obj['index_name'])
    df=pd.DataFrame(obj['rows'],index=ix,columns=obj['columns'])
    for c,t in zip(obj['columns'],obj['dtypes']):df[c]=df[c].astype(t)
    return df

def file_hash(path):
    h=hashlib.sha256()
    with path.open('rb') as src:
        for block in iter(lambda:src.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def publish(forward,asof,vintages,frames,cash_rate,meta,recorded_at):
    path=Path(forward)/RELATIVE_PATH
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    names=sorted(frames)
    manifest={'schema':SCHEMA,'decision_session':str(asof),'recorded_at_utc':recorded_at,
              'roster_sha256':meta['roster_sha256'],'roster_recorded_date':meta['roster_recorded_date'],
              'frame_symbols':names,'vintages':vintages,'cash_name':cash_rate.name}
    try:
        with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=3) as z:
            z.writestr('manifest.json',enc(manifest))
            for i,ticker in enumerate(names):
                obj=frame_pack(frames[ticker])
                pd.testing.assert_frame_equal(frames[ticker],frame_unpack(json.loads(enc(obj))),check_exact=True,check_freq=False)
                z.writestr(f'frames/{i}.json',enc(obj))
            obj=frame_pack(cash_rate.to_frame('cash_rate'))
            pd.testing.assert_frame_equal(cash_rate.to_frame('cash_rate'),frame_unpack(json.loads(enc(obj))),check_exact=True,check_freq=False)
            z.writestr('cash_rate.json',enc(obj))
        if tmp.stat().st_size>90*1024*1024:raise ValueError('Snapshot exceeds safe Git blob size')
        desc={'schema':SCHEMA,'path':RELATIVE_PATH,'sha256':file_hash(tmp),
              'decision_session':str(asof),'roster_sha256':meta['roster_sha256'],
              'roster_recorded_date':meta['roster_recorded_date'],'frame_count':len(names),
              'bytes':tmp.stat().st_size,'recorded_at_utc':recorded_at,'lossless_roundtrip_verified':True}
        os.replace(tmp,path)
        return desc
    finally:
        if tmp.exists():tmp.unlink()

def load(forward,descriptor,expected_session,expected_roster_sha):
    if not isinstance(descriptor,dict) or descriptor.get('schema')!=SCHEMA or descriptor.get('path')!=RELATIVE_PATH:
        raise ValueError('Shared input snapshot missing/unsupported; no independent download')
    if descriptor.get('decision_session')!=str(expected_session) or descriptor.get('roster_sha256')!=expected_roster_sha:
        raise ValueError('Shared input descriptor session/universe mismatch')
    path=Path(forward)/RELATIVE_PATH
    if not path.is_file() or path.stat().st_size!=descriptor['bytes'] or file_hash(path)!=descriptor['sha256']:
        raise ValueError('Shared input snapshot checksum/size mismatch')
    with zipfile.ZipFile(path,'r') as z:
        m=json.loads(z.read('manifest.json'))
        for k in ('schema','decision_session','roster_sha256','roster_recorded_date','recorded_at_utc'):
            if m[k]!=descriptor[k]:raise ValueError('Shared input manifest mismatch: '+k)
        names=m['frame_symbols']
        if len(names)!=descriptor['frame_count'] or len(names)!=len(set(names)):raise ValueError('Shared input symbol count mismatch')
        frames={t:frame_unpack(json.loads(z.read(f'frames/{i}.json'))) for i,t in enumerate(names)}
        cash=frame_unpack(json.loads(z.read('cash_rate.json')))['cash_rate'].rename(m['cash_name'])
        vintages=[(dt.date.fromisoformat(v[0]),dt.date.fromisoformat(v[1]),*v[2:]) for v in m['vintages']]
    if not vintages or vintages[-1][2]!=expected_roster_sha:raise ValueError('Shared input vintage mismatch')
    for t,df in frames.items():
        if df.empty or not df.index.is_unique or not df.index.is_monotonic_increasing or df.index[-1].date()>dt.date.fromisoformat(str(expected_session)):
            raise ValueError('Shared input invalid or future data: '+t)
    return vintages,frames,cash
