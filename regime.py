#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regime.py — Market regime classification and related helpers.

CHANGE LOG:
  MOVED    : classify_regime_C (lines 940–982)
  MOVED    : classify_regime_prob (lines 1096–1203)
  MOVED    : combine_regimes (lines 1205–1238)
  MOVED    : compute_direction_bias (lines 2489–2511)
  MOVED    : apply_regime_hysteresis (lines 2409–2455)
  MOVED    : _corr helper (lines 2457–2472)
  MOVED    : regime_di_gap (lines 2481–2487)
  MOVED    : infer_neutral_meanrev_side, infer_neutral_breakout_side (lines 2517–2547)
  MOVED    : _orch_regime_route (referenced by _brain_route_weights in strategy.py)
  PRESERVED: All formulas, weights, sigmoid parameters, and blending logic exactly.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from config import CFG
from indicators import (
    _adx_series_f,
    _bbw_series_f,
    _er_series_f,
    _sigmoid,
    _zscore_last,
    adx_wilder,
    bollinger_bandwidth,
    directional_efficiency,
)
from models import Regime, Snapshot
from utils import D0, D1


# ----------------------------
# Legacy regime (Phase 3 / classify_regime_C)
# ----------------------------
def classify_regime_C(
    highs: List[Decimal],
    lows: List[Decimal],
    closes: List[Decimal],
) -> Regime:
    er = directional_efficiency(closes, CFG.er_len)
    bbw = bollinger_bandwidth(closes, CFG.bbw_len)
    adx, pdi, mdi = adx_wilder(highs, lows, closes, CFG.adx_len)

    if er is None or bbw is None:
        return Regime(
            name="UNKNOWN", p_trend=Decimal("0.50"), p_breakout=Decimal("0.50"),
            er=er, bbw=bbw, adx=adx, di_plus=pdi, di_minus=mdi,
            p_chop=Decimal("0.50"), indeterminate=True, reason="insufficient_data",
        )

    er_n = min(D1, max(D0, (er - Decimal("0.10")) / Decimal("0.25")))
    if adx is None:
        adx_n = Decimal("0.50")
    else:
        adx_n = min(D1, max(D0, (adx - CFG.adx_trend_lo) / (CFG.adx_trend_hi - CFG.adx_trend_lo)))

    di_bias = Decimal("0.50")
    if pdi is not None and mdi is not None and (pdi + mdi) > 0:
        di_dir = (pdi - mdi) / (pdi + mdi)
        di_bias = min(D1, max(D0, (di_dir + D1) / Decimal("2")))

    squeeze = min(D1, max(D0, (Decimal("0.010") - bbw) / Decimal("0.010")))
    p_breakout = min(D1, max(D0, Decimal("0.20") + squeeze * Decimal("0.75")))

    p_trend = (er_n * Decimal("0.45")) + (adx_n * Decimal("0.40")) + (di_bias * Decimal("0.15"))
    if squeeze > Decimal("0.25"):
        p_trend = max(Decimal("0.30"), p_trend - squeeze * Decimal("0.20"))

    if squeeze >= Decimal("0.60"):
        name = "SQUEEZE"
        reason = f"bbw={bbw:.6f}"
    elif p_trend >= Decimal("0.66") and (pdi is None or mdi is None or pdi >= mdi):
        name = "TREND"
        reason = f"adx={adx:.1f} er={er:.3f}"
    elif p_trend <= Decimal("0.30"):
        name = "CHOP"
        reason = f"adx={adx:.1f} er={er:.3f}"
    else:
        name = "MIXED"
        reason = f"adx={adx:.1f} er={er:.3f}"

    p_trend_c = min(D1, max(D0, p_trend))
    p_chop_c = min(D1, max(D0, (D1 - p_trend_c)))
    direction_bias = compute_direction_bias(pdi, mdi, adx, p_trend_c)
    return Regime(
        name=name, p_trend=p_trend_c, p_breakout=p_breakout,
        er=er, bbw=bbw, adx=adx, di_plus=pdi, di_minus=mdi,
        p_chop=p_chop_c, direction_bias=direction_bias,
        indeterminate=False, reason=reason,
    )


# ----------------------------
# Probabilistic regime (v5.0 / prob_z)
# ----------------------------
def classify_regime_prob(
    highs: List[Decimal],
    lows: List[Decimal],
    closes: List[Decimal],
) -> Regime:
    if len(closes) < max(
        CFG.er_len + 5,
        CFG.bbw_len + 5,
        CFG.adx_len + 5,
        CFG.regime_z_window // 2,
    ):
        return Regime(
            name="UNKNOWN", p_trend=Decimal("0.50"), p_breakout=Decimal("0.50"),
            er=None, bbw=None, adx=None, di_plus=None, di_minus=None,
            p_chop=Decimal("0.50"), indeterminate=True, reason="insufficient_data",
        )

    C = [float(c) for c in closes]
    H = [float(h) for h in highs]
    L = [float(lo) for lo in lows]

    # Compute rolling indicator series
    er_series = _er_series_f(C, CFG.er_len)
    bbw_series = _bbw_series_f(C, CFG.bbw_len)
    adx_series, pdi_series, mdi_series = _adx_series_f(H, L, C, CFG.adx_len)
    dmi_series = [abs(p - m) for p, m in zip(pdi_series, mdi_series)]

    z_window = CFG.regime_z_window
    z_er, indet_er = _zscore_last(er_series, z_window)
    z_bbw, indet_bbw = _zscore_last(bbw_series, z_window)
    z_adx, indet_adx = _zscore_last(adx_series, z_window)
    z_dmi, indet_dmi = _zscore_last(dmi_series, z_window)

    any_indet = indet_er or indet_bbw or indet_adx

    # Dynamic orthogonalisation (reduce multi-collinearity drift)
    w_adx = CFG.regime_w_adx
    w_bbw = CFG.regime_w_bbw
    w_er = CFG.regime_w_er
    w_dmi = CFG.regime_w_dmi
    if getattr(CFG, "regime_dyn_orthogonalize", True) and not any_indet:
        corr_er_adx = _corr(er_series, adx_series)
        if abs(corr_er_adx) > getattr(CFG, "regime_corr_floor", 0.20):
            reduction = (abs(corr_er_adx) - getattr(CFG, "regime_corr_floor", 0.20)) * 0.5
            w_er = max(0.05, w_er - reduction)
            w_adx = min(0.50, w_adx + reduction)

    composite_z = (
        (w_adx * z_adx) + (w_bbw * (-z_bbw)) + (w_er * z_er) + (w_dmi * z_dmi)
    )
    p_trend = _sigmoid(composite_z, CFG.regime_sigmoid_k, CFG.regime_sigmoid_theta)

    # BBW-based breakout probability
    p_breakout = _sigmoid(-z_bbw, CFG.breakout_sigmoid_k, -CFG.breakout_theta_z_bbw)

    p_trend_d = Decimal(str(round(p_trend, 6)))
    p_breakout_d = Decimal(str(round(p_breakout, 6)))
    p_chop_d = min(D1, max(D0, D1 - p_trend_d))

    # Current indicator values for display
    er_last = Decimal(str(er_series[-1])) if er_series else None
    bbw_last = Decimal(str(bbw_series[-1])) if bbw_series else None
    adx_last = Decimal(str(adx_series[-1])) if adx_series else None
    pdi_last = Decimal(str(pdi_series[-1])) if pdi_series else None
    mdi_last = Decimal(str(mdi_series[-1])) if mdi_series else None

    # Regime name from hysteresis-safe thresholds
    z_bbw_d = round(z_bbw, 4)
    if z_bbw_d < CFG.reg_enter_squeeze_zbbw:
        name = "SQUEEZE"
    elif p_trend > 0.60:
        name = "TREND"
    elif (1.0 - p_trend) > 0.60:
        name = "CHOP"
    else:
        name = "MIXED"

    direction_bias = compute_direction_bias(pdi_last, mdi_last, adx_last, p_trend_d)

    z_scores = {
        "Z_ADX": Decimal(str(round(z_adx, 4))),
        "Z_BBW": Decimal(str(round(z_bbw, 4))),
        "Z_ER": Decimal(str(round(z_er, 4))),
        "Z_DMI": Decimal(str(round(z_dmi, 4))),
        "Z_COMP": Decimal(str(round(composite_z, 4))),
    }

    return Regime(
        name=name,
        p_trend=p_trend_d,
        p_breakout=p_breakout_d,
        er=er_last,
        bbw=bbw_last,
        adx=adx_last,
        di_plus=pdi_last,
        di_minus=mdi_last,
        p_chop=p_chop_d,
        direction_bias=direction_bias,
        indeterminate=any_indet,
        z_scores=z_scores,
        reason=f"Z_COMP={composite_z:.3f} Z_BBW={z_bbw:.3f} Z_ADX={z_adx:.3f}",
    )


# ----------------------------
# Multi-timeframe blending
# ----------------------------
def combine_regimes(r1: Regime, r5: Regime) -> Regime:
    """Multi-timeframe confirmation: 1m reacts, 5m confirms. Soft blend.

    IMPORTANT: Blend *all* fields used downstream (not just p_trend/name) to avoid
    confusing situations like: reg label from 5m but ADX/DI from 1m.
    """
    w1 = Decimal("0.40")
    w5 = Decimal("0.60")

    p_trend = (r1.p_trend * w1) + (r5.p_trend * w5)
    p_break = (r1.p_breakout * Decimal("0.50")) + (r5.p_breakout * Decimal("0.50"))

    name = r5.name if r5.name != "UNKNOWN" else r1.name
    reason = f"1m:{r1.name} 5m:{r5.name}"

    def _blend(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
        if a is None:
            return b
        if b is None:
            return a
        return (a * w1) + (b * w5)

    er = _blend(r1.er, r5.er)
    bbw = _blend(r1.bbw, r5.bbw)
    adx = _blend(r1.adx, r5.adx)
    # [AUDIT FIX RC-9] Use 5m DI as primary (confirming TF) — blending
    # directional indicators across timeframes can create artificial neutrality
    pdi = r5.di_plus if r5.di_plus is not None else r1.di_plus
    mdi = r5.di_minus if r5.di_minus is not None else r1.di_minus

    # [DAILY AUDIT FIX] Blend chop probability too (do NOT pass reason positionally).
    p_chop = min(D1, max(D0, (r1.p_chop * w1) + (r5.p_chop * w5)))
    direction_bias = (
        compute_direction_bias(pdi, mdi, adx, p_trend)
        if (pdi is not None and mdi is not None)
        else (r5.direction_bias if r5.direction_bias != 0 else r1.direction_bias)
    )
    return Regime(
        name=name,
        p_trend=p_trend,
        p_breakout=p_break,
        er=er,
        bbw=bbw,
        adx=adx,
        di_plus=pdi,
        di_minus=mdi,
        p_chop=p_chop,
        direction_bias=direction_bias,
        indeterminate=(r1.indeterminate or r5.indeterminate),
        z_scores=(r5.z_scores if r5.z_scores is not None else r1.z_scores),
        reason=reason,
    )


# ----------------------------
# Direction bias
# ----------------------------
def compute_direction_bias(
    pdi: Optional[Decimal],
    mdi: Optional[Decimal],
    adx: Optional[Decimal],
    p_trend: Optional[Decimal],
) -> int:
    """[DAILY AUDIT FIX] Centralised bias derivation. Requires meaningful DI gap, ADX, and p_trend."""
    try:
        if pdi is None or mdi is None:
            return 0
        di_gap = Decimal(str(pdi)) - Decimal(str(mdi))
        if abs(di_gap) < Decimal(str(getattr(CFG, "direction_bias_di_gap_min", Decimal("2.0")))):
            return 0
        if adx is not None and Decimal(str(adx)) < Decimal(str(getattr(CFG, "direction_bias_adx_min", Decimal("18")))):
            return 0
        if p_trend is not None and Decimal(str(p_trend)) < Decimal(str(getattr(CFG, "direction_bias_p_trend_min", Decimal("0.35")))):
            return 0
        return 1 if di_gap > 0 else -1
    except Exception:
        return 0


# ----------------------------
# Regime hysteresis (v5.2.0)
# ----------------------------
def apply_regime_hysteresis(reg: Regime, st: "BotState") -> Regime:  # type: ignore[name-defined]
    """[v5.2.0 AUDIT] Regime switch hysteresis to prevent flicker around thresholds."""
    try:
        prev = getattr(st, "last_regime_name", "") or ""
        name = reg.name
        p_tr = reg.p_trend
        p_ch = reg.p_chop
        p_br = reg.p_breakout
        zbw = float(reg.z_scores.get("Z_BBW", 0.0)) if reg.z_scores else 0.0

        # SQUEEZE (BBW compression)
        if prev == "SQUEEZE":
            if zbw <= CFG.reg_exit_squeeze_zbbw:
                name = "SQUEEZE"
        else:
            if zbw < CFG.reg_enter_squeeze_zbbw:
                name = "SQUEEZE"

        # BREAKOUT (probability)
        if prev == "BREAKOUT":
            if p_br >= CFG.reg_exit_break:
                name = "BREAKOUT"
        else:
            if p_br >= CFG.reg_enter_break and zbw >= 0.0:
                name = "BREAKOUT"

        # TREND / CHOP
        if prev == "TREND":
            if p_tr >= CFG.reg_exit_trend:
                name = "TREND"
        else:
            if p_tr >= CFG.reg_enter_trend and zbw >= 0.0:
                name = "TREND"

        if prev == "CHOP":
            if p_ch >= CFG.reg_exit_chop:
                name = "CHOP"
        else:
            if p_ch >= CFG.reg_enter_chop and zbw <= 0.5:
                name = "CHOP"

        st.last_regime_name = name
        reg.name = name
        return reg
    except Exception:
        return reg


# ----------------------------
# Correlation helper (used in orthogonalisation)
# ----------------------------
def _corr(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n < 20:
        return 0.0
    aa = a[-n:]
    bb = b[-n:]
    ma = sum(aa) / n
    mb = sum(bb) / n
    va = sum((x - ma) ** 2 for x in aa)
    vb = sum((x - mb) ** 2 for x in bb)
    if va <= 1e-12 or vb <= 1e-12:
        return 0.0
    cov = sum((aa[i] - ma) * (bb[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


# ----------------------------
# DI gap helper
# ----------------------------
def regime_di_gap(reg: Regime) -> Optional[Decimal]:
    try:
        if reg.di_plus is None or reg.di_minus is None:
            return None
        return Decimal(str(reg.di_plus)) - Decimal(str(reg.di_minus))
    except Exception:
        return None


# ----------------------------
# Neutral-state side inference helpers
# ----------------------------
def infer_neutral_meanrev_side(s: Snapshot, disc: Decimal, rsi_max: Decimal) -> Optional[str]:
    """Infer buy/sell side from VWAP stretch when direction_bias is neutral."""
    if not bool(getattr(CFG, "neutral_meanrev_enable", True)):
        return None
    if s.vwap is None or s.rsi is None:
        return None
    buy_rsi_max = min(
        Decimal(str(getattr(CFG, "neutral_meanrev_buy_rsi_max", Decimal("46")))),
        Decimal(str(rsi_max)),
    )
    sell_rsi_min = max(
        Decimal(str(getattr(CFG, "neutral_meanrev_sell_rsi_min", Decimal("54")))),
        Decimal("100") - Decimal(str(rsi_max)),
    )
    if s.px < s.vwap * (D1 - disc) and s.rsi <= buy_rsi_max:
        return "buy"
    if bool(getattr(CFG, "enable_shorts", True)) and s.px > s.vwap * (D1 + disc) and s.rsi >= sell_rsi_min:
        return "sell"
    return None


def infer_neutral_breakout_side(s: Snapshot) -> Optional[str]:
    """Infer breakout side from EMA stack + impulse when direction_bias is neutral."""
    if not bool(getattr(CFG, "neutral_breakout_enable", True)):
        return None
    if s.ema_f is None or s.ema_s is None or s.rsi is None:
        return None
    p_break = Decimal(str(getattr(s.reg, "p_breakout", D0)))
    p_trend = Decimal(str(getattr(s.reg, "p_trend", D0)))
    if p_break < Decimal(str(getattr(CFG, "orch_neutral_breakout_min", Decimal("0.58")))) and \
       p_trend < Decimal(str(getattr(CFG, "orch_neutral_trend_min", Decimal("0.62")))):
        return None
    r3 = Decimal(str(s.ret3_1m)) if s.ret3_1m is not None else D0
    if (
        s.px >= s.ema_f >= s.ema_s
        and s.rsi >= Decimal(str(getattr(CFG, "neutral_breakout_long_rsi_min", Decimal("58"))))
        and r3 >= Decimal(str(getattr(CFG, "neutral_breakout_ret3_min", Decimal("0.0006"))))
    ):
        return "buy"
    if (
        bool(getattr(CFG, "enable_shorts", True))
        and s.px <= s.ema_f <= s.ema_s
        and s.rsi <= Decimal(str(getattr(CFG, "neutral_breakout_short_rsi_max", Decimal("42"))))
        and r3 <= Decimal(str(getattr(CFG, "neutral_breakout_ret3_max_short", Decimal("-0.0006"))))
    ):
        return "sell"
    return None


# ----------------------------
# Orchestrator routing helper (used by strategy._brain_route_weights)
# ----------------------------
def _orch_regime_route(reg: Regime, opp_decay: Decimal = D0) -> Dict[str, bool]:
    """Map regime to which worker families are 'on' (for brain weight assignment)."""
    name = reg.name
    p_tr = float(reg.p_trend)
    p_ch = float(reg.p_chop)
    p_br = float(reg.p_breakout)
    bbw = float(reg.bbw) if reg.bbw is not None else 0.0

    on = float(CFG.orch_trend_on)
    chop_on = float(CFG.orch_chop_on)
    orch_breakout_on = float(CFG.orch_breakout_route_on)

    trend_active = p_tr >= on or name == "TREND"
    chop_active = p_ch >= chop_on or name in ("CHOP", "SQUEEZE")
    break_active = (
        p_br >= orch_breakout_on
        or bbw >= float(CFG.orch_vol_exp_bbw_min)
        or name in ("BREAKOUT", "SQUEEZE")
    )

    return {
        "DIP": chop_active,
        "SQMR": chop_active,
        "TPB": trend_active,
        "MOMO": trend_active or break_active,
        "VBRK": break_active,
    }
