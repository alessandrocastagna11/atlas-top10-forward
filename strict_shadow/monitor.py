#!/usr/bin/env python3
"""Fail-closed Atlas V4 Confidence70 SHADOW monitor. Does not calculate signals."""
import csv, datetime as dt, hashlib, json, os, pathlib, sys
ROOT=pathlib.Path(__file__).resolve().parent
SPEC=ROOT/"v4_confidence070_spec.json"
INBOX=ROOT/"inbox"/"latest_snapshot.json"
OUT=ROOT/"status.json"
ENGINE="ATLAS_UNIFIED_REGION_ROTATE_V4_CONFIDENCE_BETA070_2026-10-07"
CONTRACT="a0c9f00733eed0decc1b50126fb5a282a3164882078f9fd2271cb0799bd67d81"

def report(status, reason, snapshot=None):
    obj={"checked_at_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
         "engine_id":ENGINE,"mode":"SHADOW","status":status,"reason":reason,
         "live_orders_authorized":False,"official_forward_unchanged":True}
    if snapshot:
        obj.update({"decision_session":snapshot["decision_session"],
                    "execution_session":snapshot["execution_session"],
                    "nav_eur":snapshot["nav_eur"],
                    "target_count":len(snapshot["targets"]),
                    "targets":snapshot["targets"]})
    OUT.write_text(json.dumps(obj,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps(obj,ensure_ascii=False))
    return 0 if status in ("PENDING_DATA","READY_SHADOW") else 1

def validate(s):
    if s.get("engine_id")!=ENGINE: raise ValueError("engine_id mismatch")
    if str(s.get("schema_version"))!="2.1": raise ValueError("schema mismatch")
    if s.get("mode")!="SHADOW": raise ValueError("SHADOW-only; no live orders")
    p=s.get("v4_proof",{})
    if p.get("package_sha256")!=CONTRACT: raise ValueError("contract SHA mismatch")
    if p.get("sizing_profile")!="CONFIDENCE_B070" or abs(float(p.get("strength_beta",-1))-0.7)>1e-10: raise ValueError("sizing mismatch")
    if not p.get("parity_passed"): raise ValueError("engine parity flag missing")
    # A self-declared flag is not sufficient for independent parity; require attested external evidence.
    if not s.get("provenance",{}).get("independent_replay_verified",False): raise ValueError("independent replay evidence missing")
    if s.get("authorization",{}).get("real_money_authorized") or s.get("authorization",{}).get("human_approved"): raise ValueError("real-money flags forbidden")
    decision=dt.date.fromisoformat(s["decision_session"])
    execution=dt.date.fromisoformat(s["execution_session"])
    if execution<=decision: raise ValueError("execution must be after decision")
    if (dt.datetime.now(dt.timezone.utc).date()-decision).days>4: raise ValueError("stale snapshot")
    if decision>dt.datetime.now(dt.timezone.utc).date(): raise ValueError("future decision date")
    targets=s["targets"]
    if not isinstance(targets,list) or len(targets)>10: raise ValueError("too many targets")
    weight=0.; sectors={}; names=set()
    for t in targets:
        ticker=t["ticker"]; w=float(t["weight"]); sector=t["sector"]
        if not ticker or ticker in names: raise ValueError("duplicate/empty ticker")
        names.add(ticker)
        if not (0<=w<=0.15+1e-9): raise ValueError("issuer cap")
        if float(t.get("price_eur",0))<=0: raise ValueError("missing verified price")
        if t.get("region") not in ("USA","ROW"): raise ValueError("invalid region")
        weight+=w; sectors[sector]=sectors.get(sector,0)+w
    if weight>0.99+1e-9 or any(w>0.30+1e-9 for w in sectors.values()): raise ValueError("exposure/sector cap")
    if float(s.get("nav_eur",0))<=0: raise ValueError("NAV invalid")
    return s

def main():
    if not SPEC.exists(): return report("BLOCKED_SOURCE_DATA","Missing frozen spec")
    if not INBOX.exists(): return report("PENDING_DATA","No independently verified exact-engine snapshot supplied")
    try: s=validate(json.loads(INBOX.read_text()))
    except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e: return report("BLOCKED_SOURCE_DATA",str(e))
    return report("READY_SHADOW","Validated external exact-engine snapshot; paper-only, no orders sent",s)

if __name__=="__main__": sys.exit(main())
