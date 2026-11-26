"""Research-only, decision-close economic gate; no future quote or tax credit."""
from __future__ import annotations
import math

def net_edge(notional, basis, alpha_old, alpha_new, fee, slippage_bps, tax_rate,
             min_hold_days, youngest_age, minimum_trade, buffer=0.005):
    """Expected incremental 63-session relative return less full switch friction.
    Charge all immediate tax conservatively; do not assume loss credits.
    """
    if not all(math.isfinite(x) for x in (notional,basis,alpha_old,alpha_new,fee,slippage_bps,tax_rate)):
        return None
    if youngest_age<min_hold_days or notional<=0 or notional<minimum_trade:
        return None
    slip=slippage_bps/10000.
    sell_proceeds=max(0.,notional*(1-slip)-fee)
    tax=max(0.,sell_proceeds-basis)*tax_rate
    investable=(sell_proceeds-tax-fee)/(1+slip)
    if investable<minimum_trade:return None
    friction=notional-investable
    gross=notional*(alpha_new-alpha_old)
    hurdle=friction+buffer*notional
    if gross<=hurdle:return None
    return {"net_edge":gross-hurdle,"gross_edge":gross,"friction":friction,
            "tax":tax,"investable":investable,"hurdle":hurdle}

def select_swap(day, execution_day, positions, lots, score, alpha, eligible, membership,
                close, sectors, cfg, rate, cash, regime, nav):
    """At most one switch. All selection inputs as of the prior completed close.
    Eligibility, region (US-only proxy) and sector constraints precede economics.
    """
    if regime<=0 or nav<=0 or not positions:return None
    held=set(positions); minimum=max(cfg.minimum_trade_eur,cfg.rebalance_band_weight*nav)
    eligible_names=(eligible.loc[day]&membership.loc[day]).fillna(False)
    ranked=score.loc[day].where(eligible_names).dropna().sort_values(ascending=False)
    candidates=[t for t in ranked.index[:120] if t not in held]
    if not candidates:return None
    a=alpha.loc[day]; px=close.loc[day]; best=None
    for old,p in positions.items():
        if old not in px or old not in a or not math.isfinite(float(px[old])) or px[old]<=0:
            continue
        inventory=lots.get(old,[])
        if not inventory:continue
        age=min((day-lot["date"]).days for lot in inventory)
        if age<cfg.minimum_holding_calendar_days:continue
        notional=float(px[old])*p.shares
        # A voluntary replacement must itself fit the position cap. The
        # simulator still enforces the actual fill-day cash and FIFO constraints.
        if notional>cfg.maximum_position_weight*nav:continue
        for new in candidates:
            if not math.isfinite(float(a.get(new,float("nan")))):continue
            if not math.isfinite(float(px.get(new,float("nan")))) or px[new]<=0:continue
            if float(ranked[new])<=float(score.loc[day].get(old,float("inf"))):continue
            sec=sectors[new]
            other=[t for t in held-{old} if sectors.get(t)==sec]
            if len(other)>=cfg.max_names_per_sector:continue
            sector_value=sum(float(px[t])*positions[t].shares for t in other
                             if t in px and math.isfinite(float(px[t])))
            edge=net_edge(notional,p.cost_basis_eur,float(a.get(old,float("nan"))),
                          float(a[new]),cfg.fixed_fee_eur,cfg.default_slippage_bps_per_side,
                          rate,cfg.minimum_holding_calendar_days,age,minimum)
            if edge is None:continue
            if sector_value+edge["investable"]>cfg.maximum_sector_weight*nav:continue
            if best is None or edge["net_edge"]>best["net_edge"]:
                best={**edge,"old":old,"new":new,"decision_date":str(day.date()),
                      "youngest_age_days":age,"notional":notional}
    return best
