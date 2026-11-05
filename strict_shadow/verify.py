#!/usr/bin/env python3
"""Validate frozen Atlas Strict v1.2 ledger; never synthesize historical data."""
import argparse
import csv
import hashlib
import json
import math
from datetime import date
from pathlib import Path

TARGET_CAGR = 0.11304069694
TARGET_DD = -0.14304488103

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def calculate(navfile):
    with open(navfile, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if len(rows) < 2:
        raise ValueError("At least two daily NAV observations required")
    if not {"date", "nav"} <= set(rows[0]):
        raise ValueError("CSV requires date,nav columns")
    dates = [date.fromisoformat(r["date"]) for r in rows]
    values = [float(r["nav"]) for r in rows]
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("NAV must be positive and finite")
    if any(a >= b for a, b in zip(dates, dates[1:])):
        raise ValueError("Dates must be strictly increasing")
    years = (dates[-1] - dates[0]).days / 365.25
    if years <= 0:
        raise ValueError("Invalid observation period")
    cagr = (values[-1] / values[0]) ** (1 / years) - 1
    peak = values[0]
    maxdd = 0.
    for value in values:
        peak = max(peak, value)
        maxdd = min(maxdd, value / peak - 1)
    return {"start": str(dates[0]), "end": str(dates[-1]),
            "observations": len(rows), "cagr": cagr, "max_drawdown": maxdd,
            "nav_sha256": sha256(navfile)}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nav", required=True, help="Original daily NAV CSV (date,nav)")
    p.add_argument("--manifest", required=True, help="Original SHA256 manifest JSON")
    p.add_argument("--cagr-tolerance", type=float, default=1e-5)
    p.add_argument("--dd-tolerance", type=float, default=1e-5)
    args = p.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    expected = manifest.get("nav_sha256")
    if not expected:
        raise SystemExit("BLOCKED: manifest missing nav_sha256")
    actual = sha256(args.nav)
    if expected.lower() != actual.lower():
        raise SystemExit("FAILED: NAV SHA256 differs from frozen manifest")
    result = calculate(args.nav)
    result["cagr_parity"] = abs(result["cagr"] - TARGET_CAGR) <= args.cagr_tolerance
    result["dd_parity"] = abs(result["max_drawdown"] - TARGET_DD) <= args.dd_tolerance
    result["status"] = "NAV_PARITY_ONLY" if result["cagr_parity"] and result["dd_parity"] else "FAILED_PARITY"
    print(json.dumps(result, indent=2))
    if result["status"] != "NAV_PARITY_ONLY":
        raise SystemExit(2)
    print("Note: NAV parity is NOT proof of strategy replay; source code and orders still require independent verification.")

if __name__ == "__main__":
    main()
