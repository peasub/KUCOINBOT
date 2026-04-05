#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tp.py — Take-profit calculation, volatility scaling, and regime-based sizing helpers.

CHANGE LOG:
  MOVED    : atrp_series, bbw_series (lines 2550–2596)
  MOVED    : compute_tp_base_from_vol (lines 2598–2633)
  MOVED    : effective_tp (lines 2634–2680)
  MOVED    : adjust_tp_for_strategy (lines 2682–2716)
  MOVED    : regime_sizing_mult, cooldown_mult (lines 2718–2743)
  MOVED    : compute_vwap (lines 2745–2753)
  MOVED    : required_move_pct (line 2031)
  MOVED    : entry_expected_edge_bps (lines 2036–2058)
  PRESERVED: All formulas, multipliers, and clamp logic exactly.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import List, Optional, Tuple, TYPE_CHECKING

from config import CFG
from models import Regime, Snapshot
from utils import D0, D1, _clamp01

if TYPE_CHECKING:
    from models import BotState


# ----------------------------
# Volatility series helpers
# ----------------------------
def atrp_series(
    highs: List[Decimal],
    lows: List[Decimal],
    closes: List[Decimal],
    length: int,
) -> List[Decimal]:
    """Return ATR% series aligned to closes indices (for i >= length)."""
    L = len(closes)
    if L < length + 1:
        return []
    trs: List[Decimal] = []
    for i in range(1, L):
        h = highs[i]
        lo = lows[i]
        pc = closes[i - 1]
        tr = max(h - lo, abs(h - pc), abs(lo - pc))
        trs.append(tr)

    out: List[Decimal] = []
    win_sum = sum(trs[:length])
    atr_v = win_sum / Decimal(length)
    if closes[length] > 0:
        out.append(atr_v / closes[length])
    else:
        out.append(D0)
    for j in range(length, len(trs)):
        win_sum += trs[j] - trs[j - length]
        atr_v = win_sum / Decimal(length)
        c = closes[j + 1]
        out.append((atr_v / c) if c > 0 else D0)
    return out


def bbw_series(closes: List[Decimal], length: int) -> List[Decimal]:
    """Return BBW series aligned to closes indices (for i >= length - 1)."""
    L = len(closes)
    if L < length:
        return []
    out: List[Decimal] = []
    for i in range(length, L + 1):
        window = closes[i - length : i]
        mean = sum(window) / Decimal(length)
        if mean == 0:
            out.append(D0)
            continue
        var = sum((x - mean) ** 2 for x in window) / Decimal(length)
        sd = Decimal(str(math.sqrt(float(var))))
        upper = mean + sd * Decimal("2")
        lower = mean - sd * Decimal("2")
        out.append((upper - lower) / mean)
    return out


# ----------------------------
# Volatility-based TP scaling
# ----------------------------
def compute_tp_base_from_vol(
    highs_5m: List[Decimal],
    lows_5m: List[Decimal],
    closes_5m: List[Decimal],
) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """Compute volatility-scaled TP base + vol stats: (tp_base, Vt, Vmin, Vmax, norm)."""
    metric = CFG.tp_vol_metric
    n = CFG.tp_vol_lookback_n
    if metric == "atrp":
        series = atrp_series(highs_5m, lows_5m, closes_5m, CFG.tp_vol_atr_len)
    else:
        series = bbw_series(closes_5m, CFG.tp_vol_bbw_len)
    if not series:
        return None, None, None, None, None

    w = series[-n:] if len(series) >= n else series[:]
    v_t = w[-1]
    v_min = min(w)
    v_max = max(w)
    denom = v_max - v_min
    if denom == 0:
        norm = D0
    else:
        norm = _clamp01((v_t - v_min) / denom)

    g = CFG.tp_vol_gamma
    try:
        norm_g = Decimal(str(float(norm) ** float(g)))
    except Exception:
        norm_g = norm

    tp_min = CFG.tp_vol_floor_pct
    tp_max = CFG.tp_vol_ceiling_pct
    tp_base = tp_min + norm_g * (tp_max - tp_min)
    tp_base = max(tp_min, min(tp_max, tp_base))
    return tp_base, v_t, v_min, v_max, norm


# ----------------------------
# Master TP computation
# ----------------------------
def effective_tp(
    tp_base: Decimal,
    tp_req: Decimal,
    reg: Regime,
    atrp: Optional[Decimal],
) -> Tuple[Decimal, Decimal]:
    """One master TP knob with regime-aware scaling + volatility realism.

    Returns (tp1, tp2).
    """
    if reg.name == "CHOP":
        base_mult = Decimal("0.55")
    elif reg.name == "MIXED":
        base_mult = Decimal("0.75")
    elif reg.name == "SQUEEZE":
        base_mult = Decimal("0.85")
    else:  # TREND
        base_mult = Decimal("1.00")

    prob_mult = Decimal("0.85") + (reg.p_trend * Decimal("0.50"))  # 0.85..1.35
    tp_struct = tp_base * base_mult * prob_mult

    if atrp is not None and atrp > 0:
        tp_cap = max(tp_req, atrp * CFG.tp_atr_cap_mult)
    else:
        tp_cap = tp_struct

    tp1 = max(tp_req, min(max(tp_req, tp_struct), tp_cap))
    tp1 = min(tp1, tp_base * Decimal("1.60"))

    tp2 = tp1 * CFG.tp2_mult
    return tp1, tp2


# ----------------------------
# Strategy-aware TP shaping
# ----------------------------
def adjust_tp_for_strategy(
    st: "BotState",
    s: Snapshot,
    tp1_eff: Decimal,
    tp2_eff: Decimal,
) -> Tuple[Decimal, Decimal]:
    """[DAILY AUDIT FIX] Compress TP distances for short VBRK in CHOP/MIXED and low-ATR long continuations."""
    try:
        entry_tag = str(getattr(st, "entry_intent_tag", "") or "")
        side_pos = str(
            getattr(st, "position_side", "") or getattr(s, "pos_side", "") or ""
        )
        if (
            bool(getattr(CFG, "vbrk_tp_compress_enable", True))
            and entry_tag == "VBRK"
            and side_pos == "SHORT"
            and getattr(s.reg, "name", "") in ("CHOP", "MIXED")
            and Decimal(str(getattr(s.reg, "p_trend", D0))) <= Decimal(str(getattr(CFG, "vbrk_tp_compress_short_p_trend_max", Decimal("0.62"))))
        ):
            mult = Decimal(str(getattr(CFG, "vbrk_tp_compress_short_mult", Decimal("0.78"))))
            tp1_eff = max(Decimal(str(getattr(s, "tp_req", D0))), tp1_eff * mult)
            tp2_eff = max(tp1_eff * Decimal("1.55"), tp2_eff * mult)

        if (
            bool(getattr(CFG, "long_continuation_tp_compress_enable", True))
            and entry_tag in ("MOMO", "VBRK", "TPB")
            and side_pos == "LONG"
            and (
                s.atrp is not None
                and Decimal(str(s.atrp)) <= Decimal(str(getattr(CFG, "long_continuation_tp_compress_atrp_max", Decimal("0.0014"))))
            )
            and Decimal(str(getattr(s.reg, "p_trend", D0))) <= Decimal(str(getattr(CFG, "long_continuation_tp_compress_p_trend_max", Decimal("0.72"))))
        ):
            mult = Decimal(str(getattr(CFG, "long_continuation_tp_compress_mult", Decimal("0.72"))))
            tp1_eff = max(Decimal(str(getattr(s, "tp_req", D0))), tp1_eff * mult)
            tp2_eff = max(tp1_eff * Decimal("1.55"), tp2_eff * mult)
    except Exception:
        pass
    return tp1_eff, tp2_eff


# ----------------------------
# Regime-based sizing + cooldown
# ----------------------------
def regime_sizing_mult(reg: Regime) -> Decimal:
    """Scale position size by regime strength and DI direction."""
    m = Decimal("0.85") + (reg.p_trend * Decimal("0.55"))  # 0.85..1.40
    if reg.di_plus is not None and reg.di_minus is not None and reg.di_minus > reg.di_plus:
        m *= Decimal("0.70")  # bearish dominance haircut
    if reg.name == "SQUEEZE":
        m *= Decimal("0.80")
    return min(Decimal("1.40"), max(Decimal("0.40"), m))


def cooldown_mult(reg: Regime, atrp: Optional[Decimal]) -> Decimal:
    """Dynamic cooldown multiplier: longer in chop/squeeze, shorter in trend."""
    m = Decimal("1.0")
    if reg.name == "CHOP":
        m *= Decimal("1.8")
    elif reg.name == "SQUEEZE":
        m *= Decimal("2.2")
    elif reg.name == "TREND":
        m *= Decimal("0.75")
    else:
        m *= Decimal("1.2")
    if atrp is not None:
        if atrp >= Decimal("0.0030"):
            m *= Decimal("0.85")
        elif atrp <= Decimal("0.0012"):
            m *= Decimal("1.10")
    return min(Decimal("4.0"), max(Decimal("0.6"), m))


# ----------------------------
# VWAP
# ----------------------------
def compute_vwap(
    closes: List[Decimal],
    vols: List[Decimal],
    length: int = 60,
) -> Optional[Decimal]:
    """Volume-weighted average price over the last `length` bars."""
    if len(closes) < length or len(vols) < length:
        return None
    c = closes[-length:]
    v = vols[-length:]
    denom = sum(v)
    if denom == 0:
        return None
    return sum(ci * vi for ci, vi in zip(c, v)) / denom


# ----------------------------
# Edge economics
# ----------------------------
def required_move_pct(spread_pct: Decimal) -> Decimal:
    """Round-trip fees + spread + adverse + minimum edge floor."""
    return (CFG.fee_buy + CFG.fee_sell) + spread_pct + CFG.adverse_select_pct + CFG.min_net_edge_pct


def entry_expected_edge_bps(s: Snapshot, intent: Optional["Intent"] = None) -> Decimal:  # type: ignore[name-defined]
    """Expected gross cushion above the required move floor, in basis points.

    RC18 uses a blended TP1/TP2 target by default (entry_edge_model="weighted_tp").
    """
    try:
        tp1 = Decimal(str(s.tp1_eff))
        tp2 = Decimal(str(getattr(s, "tp2_eff", s.tp1_eff)))
        req = Decimal(str(s.tp_req))
        model = str(getattr(CFG, "entry_edge_model", "weighted_tp") or "weighted_tp").lower()
        if model == "tp1":
            target = tp1
        elif model == "tp2":
            target = tp2
        else:
            split1 = Decimal(str(getattr(CFG, "tp_split_1", Decimal("0.62"))))
            runner_credit = Decimal(str(getattr(CFG, "entry_edge_runner_credit", D1)))
            target = (tp1 * split1) + (tp2 * (D1 - split1) * runner_credit)
        return max(D0, (target - req) * Decimal("10000"))
    except Exception:
        return D0


# ----------------------------
# TP effective value lookup (used by backtest)
# ----------------------------
def _tp_eff_from_mode(
    tp_mode: str,
    tp_req: Decimal,
    reg: Regime,
    atrp_v: Optional[Decimal],
    highs_5m: List[Decimal],
    lows_5m: List[Decimal],
    closes_5m: List[Decimal],
) -> Tuple[Decimal, Decimal, Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """Return (tp1_eff, tp2_eff, tp_base_dyn, vol_t, vol_min, vol_max, vol_norm) using same logic as build_snapshot."""
    if tp_mode == "static":
        tp1_eff, tp2_eff = effective_tp(CFG.tp_static_pct, tp_req, reg, atrp_v)
        return tp1_eff, tp2_eff, None, None, None, None, None
    if tp_mode == "vol":
        tp_base_dyn, vol_t, vol_min, vol_max, vol_norm = compute_tp_base_from_vol(highs_5m, lows_5m, closes_5m)
        if tp_base_dyn is None:
            tp1_eff, tp2_eff = effective_tp(CFG.tp_pct_base, tp_req, reg, atrp_v)
            return tp1_eff, tp2_eff, None, None, None, None, None
        # [AUDIT FIX RC-4] Route through effective_tp for regime scaling
        tp1_eff, tp2_eff = effective_tp(tp_base_dyn, tp_req, reg, atrp_v)
        return tp1_eff, tp2_eff, tp_base_dyn, vol_t, vol_min, vol_max, vol_norm
    # default: regime
    tp1_eff, tp2_eff = effective_tp(CFG.tp_pct_base, tp_req, reg, atrp_v)
    return tp1_eff, tp2_eff, None, None, None, None, None
