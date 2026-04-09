#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strategy.py — Strategy workers, orchestrator, exit signal, and entry quality assessment.

CHANGE LOG:
  MOVED    : assess_entry_quality (lines 2061–2282)
  MOVED    : _brain_route_weights (lines 2825–2847)
  MOVED    : _dip_worker (lines 2850–2948)
  MOVED    : _trend_pullback_worker (lines ~2950–3100)
  MOVED    : _momo_worker (lines ~3100–3200)
  MOVED    : _vol_breakout_worker / VBRK (lines ~3200–3288)
  MOVED    : diagnose_no_intent (lines 3291–3357)
  MOVED    : _squeeze_meanrev_worker (lines 3360–3409)
  MOVED    : collect_intents, orchestrate (lines 3411–3543)
  MOVED    : exit_signal (lines 3547–3676)
  PRESERVED: Every filter threshold, RSI guard, score formula, and DI veto exactly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional, Tuple

from config import CFG
from models import BotState, Intent, MKT, Regime, Snapshot
from regime import (
    _orch_regime_route,
    compute_direction_bias,
    infer_neutral_breakout_side,
    infer_neutral_meanrev_side,
    regime_di_gap,
)
from tp import entry_expected_edge_bps
from utils import D0, D1, _clamp_dec


# ----------------------------
# Entry quality gate
# ----------------------------
def assess_entry_quality(s: Snapshot, intent: Intent) -> Tuple[bool, Decimal, str]:
    """Unified preflight quality gate for long and short entries. Returns (ok, score, reason)."""
    edge_bps = entry_expected_edge_bps(s, intent)
    reasons: List[str] = []

    # [PHASE A — PROVEN] Low conviction hard block.
    # Trade #6 on April 7: SFOL SHORT entered at p=0.17, hit EMERGENCY at -1.093%.
    try:
        _min_p = Decimal(str(getattr(CFG, "min_p_trend_for_entry", Decimal("0.20"))))
        if s.reg.p_trend is not None and s.reg.p_trend < _min_p:
            return False, D0, f"score=0.00|blocker_family=low_conviction|p_trend={s.reg.p_trend:.2f}|edge_bps={edge_bps:.1f}"
    except Exception:
        pass

    def _clip01(x: Decimal) -> Decimal:
        return max(D0, min(D1, x))

    def _score_linear(
        x: Optional[Decimal], lo: Decimal, hi: Decimal, fallback: Decimal = Decimal("0.50")
    ) -> Decimal:
        if x is None or hi <= lo:
            return fallback
        return _clip01((Decimal(str(x)) - lo) / (hi - lo))

    di_gap = regime_di_gap(s.reg)

    if intent.side == "buy" and bool(getattr(CFG, "long_quality_enable", True)):
        try:
            if edge_bps < Decimal(str(getattr(CFG, "long_quality_edge_buffer_bps", Decimal("8")))):
                reasons.append("edge_thin")
                return False, D0, f"score=0.00|blocker_family=edge_too_thin|edge_bps={edge_bps:.1f}|reason={'/'.join(reasons)}"
        except Exception:
            pass

        if s.ema_f is not None and s.ret3_1m is not None:
            if (
                Decimal(str(s.ret3_1m))
                <= -abs(Decimal(str(getattr(CFG, "dip_falling_knife_ret3_max", Decimal("0.0030")))))
                and s.px < s.ema_f
            ):
                reasons.append("falling_knife")
                return False, D0, f"score=0.00|blocker_family=falling_knife|edge_bps={edge_bps:.1f}|reason={'/'.join(reasons)}"

        if s.ema_f is not None and s.ret5_1m is not None:
            if (
                Decimal(str(s.ret5_1m))
                <= -abs(Decimal(str(getattr(CFG, "long_quality_ret5_floor", Decimal("0.0045")))))
                and s.px <= s.ema_f
            ):
                reasons.append("ret5_impulse")
                return False, D0, f"score=0.00|blocker_family=ret5_impulse|edge_bps={edge_bps:.1f}|reason={'/'.join(reasons)}"

        p_sc = _score_linear(Decimal(str(s.reg.p_trend)), Decimal("0.50"), Decimal("0.78"), Decimal("0.50"))
        adx_sc = _score_linear(
            Decimal(str(s.reg.adx)) if s.reg.adx is not None else None,
            Decimal("18"), Decimal("30"), Decimal("0.45"),
        )
        di_sc = _score_linear(di_gap, Decimal("0"), Decimal("8"), Decimal("0.40"))

        ema_sc = Decimal("0.35")
        try:
            if s.ema_f is not None:
                dist_ema = abs(s.px - s.ema_f) / s.ema_f if s.ema_f > 0 else D0
                ema_sc = _clip01(D1 - (dist_ema / Decimal("0.006")))
        except Exception:
            pass

        edge_sc = _clip01(edge_bps / Decimal("20"))
        score = (p_sc * Decimal("0.28") + adx_sc * Decimal("0.22") + di_sc * Decimal("0.18")
                 + ema_sc * Decimal("0.16") + edge_sc * Decimal("0.16"))
        min_score = Decimal(str(getattr(CFG, "long_quality_score_min", Decimal("0.60"))))
        if score < min_score:
            reasons.append(f"score_low:{score:.2f}")
            return False, score, f"score={score:.2f}|blocker_family=quality_reject_score|edge_bps={edge_bps:.1f}|reason={'/'.join(reasons)}"
        return True, score, f"score={score:.2f}|edge_bps={edge_bps:.1f}"

    if intent.side == "sell" and bool(getattr(CFG, "short_quality_enable", True)):
        try:
            if edge_bps < Decimal(str(getattr(CFG, "short_quality_edge_buffer_bps", Decimal("8")))):
                reasons.append("edge_thin")
                return False, D0, f"score=0.00|blocker_family=edge_too_thin|edge_bps={edge_bps:.1f}|reason={'/'.join(reasons)}"
        except Exception:
            pass

        if s.ema_f is not None and s.ret5_1m is not None:
            if (
                Decimal(str(s.ret5_1m))
                >= abs(Decimal(str(getattr(CFG, "short_quality_rally_ret5_max", Decimal("0.0040")))))
                and s.px >= s.ema_f
            ):
                reasons.append("ret5_rally")
                return False, D0, f"score=0.00|blocker_family=ret5_rally|edge_bps={edge_bps:.1f}|reason={'/'.join(reasons)}"

        p_sc = _score_linear(
            D1 - Decimal(str(s.reg.p_trend)), Decimal("0.30"), Decimal("0.70"), Decimal("0.50")
        )
        adx_sc = _score_linear(
            Decimal(str(s.reg.adx)) if s.reg.adx is not None else None,
            Decimal("18"), Decimal("30"), Decimal("0.45"),
        )
        di_sc_short = _score_linear(
            -di_gap if di_gap is not None else None, Decimal("0"), Decimal("8"), Decimal("0.40")
        )
        # [AUDIT FIX RC-2] Compute ema_sc for short side (was hardcoded 0.16)
        ema_sc_short = Decimal("0.35")
        try:
            if s.ema_f is not None:
                dist_ema = abs(s.px - s.ema_f) / s.ema_f if s.ema_f > 0 else D0
                ema_sc_short = _clip01(D1 - (dist_ema / Decimal("0.006")))
        except Exception:
            pass
        edge_sc = _clip01(edge_bps / Decimal("20"))
        score = (p_sc * Decimal("0.28") + adx_sc * Decimal("0.22") + di_sc_short * Decimal("0.18")
                 + ema_sc_short * Decimal("0.16") + edge_sc * Decimal("0.16"))
        min_score = Decimal(str(getattr(CFG, "short_quality_score_min", Decimal("0.62"))))
        if score < min_score:
            reasons.append(f"score_low:{score:.2f}")
            return False, score, f"score={score:.2f}|blocker_family=quality_reject_score|edge_bps={edge_bps:.1f}|reason={'/'.join(reasons)}"
        return True, score, f"score={score:.2f}|edge_bps={edge_bps:.1f}"

    return True, Decimal("1.00"), f"edge_bps={edge_bps:.1f}"


# ----------------------------
# Brain routing weights
# ----------------------------
def _brain_route_weights(reg: Regime, opp_decay: Decimal = D0) -> dict:
    """[DAILY AUDIT FIX] Brain-owned routing weights (no hard upstream lockouts)."""
    if not bool(getattr(CFG, "brain_route_soft_enable", True)):
        return {k: D1 for k in ("DIP", "TPB", "MOMO", "VBRK", "SQMR", "SFOL")}
    fam = _orch_regime_route(reg, opp_decay)
    on_w = Decimal(str(getattr(CFG, "brain_route_on_weight", Decimal("1.12"))))
    off_w = Decimal(str(getattr(CFG, "brain_route_off_weight", Decimal("0.82"))))
    w = {}
    for k in ("DIP", "TPB", "MOMO", "VBRK", "SQMR", "SFOL"):  # [V7.3.5] added SFOL
        w[k] = on_w if fam.get(k, fam.get("VBRK", False)) else off_w  # SFOL inherits VBRK routing
    try:
        if (
            Decimal(str(getattr(reg, "p_breakout", D0))) >= Decimal("0.80")
            and Decimal(str(getattr(reg, "p_trend", D0))) >= Decimal("0.75")
        ):
            w["MOMO"] = max(w["MOMO"], Decimal("1.18"))
            w["VBRK"] = max(w["VBRK"], Decimal("1.08"))
    except Exception:
        pass
    return w


# ----------------------------
# Strategy workers
# ----------------------------
def _dip_worker(s: Snapshot, opp_decay: Decimal = D0) -> Optional[Intent]:
    """Dip / mean-reversion worker, mirrored for LONG/SHORT via direction_bias."""
    if s.vwap is None or s.rsi is None:
        return None

    disc = getattr(CFG, "dip_disc_mixed", Decimal("0.0018"))
    rsi_max = getattr(CFG, "dip_rsi_max_mixed", Decimal("46"))
    p_trend_min = getattr(CFG, "dip_p_trend_min", Decimal("0.32"))
    if s.reg.p_chop >= Decimal("0.60") or s.reg.name == "CHOP":
        disc = getattr(CFG, "dip_disc_chop", Decimal("0.0012"))
        rsi_max = getattr(CFG, "dip_rsi_max_chop", Decimal("52"))
        p_trend_min = getattr(CFG, "dip_p_trend_min_chop", Decimal("0.28"))

    try:
        disc = max(Decimal("0.0006"), disc * (D1 - (opp_decay * Decimal("0.60"))))
        rsi_max = rsi_max + (opp_decay * Decimal("10"))
        p_trend_min = max(Decimal("0.18"), p_trend_min - (opp_decay * Decimal("0.08")))
    except Exception:
        pass

    bias = int(getattr(s.reg, "direction_bias", 0) or 0)
    if bias == 0:
        inferred = infer_neutral_meanrev_side(s, disc, rsi_max)
        if inferred is None:
            return None
        bias = 1 if inferred == "buy" else -1

    if bias > 0:
        if s.reg.p_trend < p_trend_min:
            return None
        try:
            if (
                s.ret3_1m is not None and s.reg.adx is not None
                and s.reg.di_plus is not None and s.reg.di_minus is not None
                and s.ret3_1m <= -abs(Decimal(str(getattr(CFG, "dip_falling_knife_ret3_max", Decimal("0.0030")))))
                and Decimal(str(s.reg.adx)) >= CFG.long_bear_adx_min
                and Decimal(str(s.reg.di_minus)) > Decimal(str(s.reg.di_plus))
            ):
                return None
        except Exception:
            pass
        try:
            if s.reg.adx is not None and s.reg.di_plus is not None and s.reg.di_minus is not None:
                dmi_gap = Decimal(str(s.reg.di_minus)) - Decimal(str(s.reg.di_plus))
                if Decimal(str(s.reg.adx)) >= CFG.dip_bear_adx_min and dmi_gap >= CFG.dip_bear_dmi_gap:
                    return None
                if Decimal(str(s.reg.adx)) >= CFG.dip_bear_adx_min and dmi_gap >= CFG.dip_bear_relax_gap:
                    disc = disc * Decimal("1.35")
        except Exception:
            pass
        if s.rsi > rsi_max:
            return None
        if s.px < s.vwap * (D1 - disc):
            score = float(s.reg.p_chop) + float((s.vwap - s.px) / s.vwap) * 100.0
            return Intent("buy", "DIP", score, 0, D1, "VWAP_DIP")  # [AUDIT FIX RC-3] was momo_size_mult
        return None

    # SHORT side
    if not getattr(CFG, "enable_shorts", True):
        return None
    if s.reg.p_trend < p_trend_min:
        return None
    rsi_min = Decimal("100") - rsi_max
    if s.rsi < rsi_min:
        return None
    if s.px > s.vwap * (D1 + disc):
        score = float(s.reg.p_chop) + float((s.px - s.vwap) / s.vwap) * 100.0
        return Intent("sell", "DIP", score, 0, D1, "VWAP_RALLY")  # [AUDIT FIX RC-3] was momo_size_mult
    return None


def _trend_pullback_worker(s: Snapshot, opp_decay: Decimal = D0) -> Optional[Intent]:
    """Trend pullback (TPB): buy/sell the retracement in a confirmed trend."""
    if s.ema_f is None or s.rsi is None:
        return None
    p_trend_min = Decimal(str(getattr(CFG, "trend_pullback_p_trend_min", Decimal("0.60"))))
    adx_min = Decimal(str(getattr(CFG, "trend_pullback_adx_min", Decimal("23"))))
    di_gap_min = Decimal(str(getattr(CFG, "trend_pullback_di_gap_min", Decimal("3"))))

    try:
        p_trend_min = max(Decimal("0.45"), p_trend_min - (opp_decay * Decimal("0.06")))
    except Exception:
        pass

    if s.reg.p_trend < p_trend_min:
        return None
    if s.reg.adx is not None and Decimal(str(s.reg.adx)) < adx_min:
        return None

    di_gap = regime_di_gap(s.reg)
    if di_gap is not None and abs(di_gap) < di_gap_min:
        return None

    if bool(getattr(CFG, "trend_pullback_block_in_squeeze", True)) and s.reg.name == "SQUEEZE":
        return None

    bias = int(getattr(s.reg, "direction_bias", 0) or 0)
    rsi_lo = Decimal(str(CFG.trend_pullback_rsi_lo))
    rsi_hi = Decimal(str(CFG.trend_pullback_rsi_hi))
    max_dist = Decimal(str(CFG.trend_pullback_max_dist))

    if bias > 0:
        if not (rsi_lo <= s.rsi <= rsi_hi):
            return None
        dist = (s.ema_f - s.px) / s.ema_f if s.ema_f > 0 else D0
        if dist < D0 or dist > max_dist:
            return None
        if bool(getattr(CFG, "trend_pullback_require_fast_reclaim", True)):
            above_ema = Decimal(str(getattr(CFG, "trend_pullback_allow_above_ema_fast", Decimal("0.0003"))))
            if s.px < s.ema_f * (D1 - above_ema):
                return None
        if s.vwap is not None:
            vwap_prem = (s.px - s.vwap) / s.vwap if s.vwap > 0 else D0
            if vwap_prem > Decimal(str(CFG.trend_pullback_max_premium_vwap)):
                return None
        score = float(s.reg.p_trend) * 1.2
        return Intent("buy", "TPB", score, 0, Decimal("1.0"), "TREND_PULLBACK")

    if not getattr(CFG, "enable_shorts", True):
        return None
    if bias < 0:
        rsi_lo_s = Decimal("100") - rsi_hi
        rsi_hi_s = Decimal("100") - rsi_lo
        if not (rsi_lo_s <= s.rsi <= rsi_hi_s):
            return None
        dist = (s.px - s.ema_f) / s.ema_f if s.ema_f > 0 else D0
        if dist < D0 or dist > max_dist:
            return None
        score = float(s.reg.p_trend) * 1.2
        return Intent("sell", "TPB", score, 0, Decimal("1.0"), "TREND_PULLBACK_SHORT")

    return None


def _momo_worker(s: Snapshot, opp_decay: Decimal = D0) -> Optional[Intent]:
    """Momentum breakout worker (MOMO): ride a confirmed directional move."""
    if s.atrp is None or s.rsi is None or s.vwap is None:
        return None

    p_trend_min = Decimal(str(getattr(CFG, "momo_p_trend_min", Decimal("0.61"))))
    atrp_min = Decimal(str(getattr(CFG, "momo_atrp_min", Decimal("0.0012"))))
    try:
        p_trend_min = max(Decimal("0.50"), p_trend_min - (opp_decay * Decimal("0.05")))
        atrp_min = max(Decimal("0.0008"), atrp_min * (D1 - (opp_decay * Decimal("0.30"))))
    except Exception:
        pass

    if s.reg.p_trend < p_trend_min:
        return None
    if Decimal(str(s.atrp)) < atrp_min:
        return None

    bias = int(getattr(s.reg, "direction_bias", 0) or 0)
    if bias == 0:
        return None

    if bias > 0:
        rsi_min = Decimal(str(getattr(CFG, "momo_rsi_min", Decimal("66"))))
        if s.rsi < rsi_min:
            return None
        _sq = s.reg.name == "SQUEEZE" or "1m:SQUEEZE" in str(getattr(s.reg, "reason", "") or "")
        _momo_cap = (
            Decimal(str(getattr(CFG, "momo_squeeze_rsi_max", Decimal("80"))))
            if _sq
            else Decimal(str(getattr(CFG, "long_continuation_rsi_max_momo", Decimal("69"))))
        )
        _momo_atrp_floor = Decimal("0.0010") if _sq else Decimal("0.0026")
        if s.rsi >= _momo_cap and Decimal(str(s.atrp)) <= _momo_atrp_floor:
            return None
        # VWAP premium check
        vwap_prem = (s.px - s.vwap) / s.vwap if s.vwap > 0 else D0
        rsi_norm_thr = Decimal(str(getattr(CFG, "momo_vwap_premium_rsi_norm_threshold", Decimal("72"))))
        max_vwap_prem = (
            Decimal(str(getattr(CFG, "momo_vwap_premium_rsi_norm_relax", Decimal("0.0070"))))
            if s.rsi <= rsi_norm_thr
            else Decimal(str(getattr(CFG, "momo_max_premium_vwap", Decimal("0.0040"))))
        )
        if vwap_prem > max_vwap_prem:
            return None
        if s.reg.name == "MIXED":
            if s.reg.p_breakout < Decimal(str(getattr(CFG, "momo_mixed_p_break_min", Decimal("0.72")))):
                return None
            if s.rsi < Decimal(str(getattr(CFG, "momo_mixed_rsi_min", Decimal("70")))):
                return None
        score = float(s.reg.p_trend) + float(s.atrp) * 20.0
        urgency = 2 if CFG.momo_use_taker else 0
        return Intent("buy", "MOMO", score, urgency, Decimal(str(CFG.momo_size_mult)), "MOMENTUM_LONG")

    if not getattr(CFG, "enable_shorts", True):
        return None
    rsi_max = Decimal("100") - Decimal(str(getattr(CFG, "momo_rsi_min", Decimal("66"))))
    if s.rsi > rsi_max:
        return None
    _sq = s.reg.name == "SQUEEZE" or "1m:SQUEEZE" in str(getattr(s.reg, "reason", "") or "")
    _momo_cap_short = (
        Decimal(str(getattr(CFG, "momo_squeeze_rsi_max", Decimal("80"))))
        if _sq
        else Decimal(str(getattr(CFG, "long_continuation_rsi_max_momo", Decimal("69"))))
    )
    if s.rsi <= (Decimal("100") - _momo_cap_short):
        return None
    score = float(s.reg.p_trend) + float(s.atrp) * 20.0
    urgency = 2 if CFG.momo_use_taker else 0
    return Intent("sell", "MOMO", score, urgency, Decimal(str(CFG.momo_size_mult)), "MOMENTUM_SHORT")


def _vol_breakout_worker(s: Snapshot, opp_decay: Decimal = D0) -> Optional[Intent]:
    """Volatility/squeeze breakout worker (VBRK) — LONG side only after V7.3.5 split.
    [V7.3.5 FIX] Short follow-through moved to _short_followthrough_worker.
    [V7.3.5 FIX] VBRK long blocked in CHOP unless p_breakout >= 0.55.
    """
    if s.atrp is None or s.rsi is None:
        return None

    p_break = Decimal(str(getattr(s.reg, "p_breakout", D0)))
    p_trend = Decimal(str(getattr(s.reg, "p_trend", D0)))

    p_break_min = Decimal(str(getattr(CFG, "vbrk_p_break_min", Decimal("0.55"))))
    p_trend_min = Decimal(str(getattr(CFG, "vbrk_p_trend_min", Decimal("0.55"))))

    try:
        p_break_min = max(Decimal("0.40"), p_break_min - (opp_decay * Decimal("0.05")))
        p_trend_min = max(Decimal("0.40"), p_trend_min - (opp_decay * Decimal("0.05")))
    except Exception:
        pass

    squeeze_like = s.reg.name == "SQUEEZE" or "1m:SQUEEZE" in str(getattr(s.reg, "reason", "") or "")

    # [V7.3.5 FIX] Block VBRK long in CHOP unless breakout probability is strong
    if s.reg.name == "CHOP" and p_break < Decimal("0.55"):
        return None

    if not squeeze_like:
        if p_break < p_break_min:
            return None
        if p_trend < p_trend_min:
            return None

    if p_trend < Decimal(str(getattr(CFG, "squeeze_break_p_trend_min", Decimal("0.60")))) and squeeze_like:
        return None

    urgency = 2 if CFG.vbrk_use_taker else 0
    score = float(s.reg.p_breakout) + float(s.reg.p_trend)

    bias = int(getattr(s.reg, "direction_bias", 0) or 0)
    if bias == 0:
        inferred = infer_neutral_breakout_side(s)
        if inferred is None or inferred == "sell":
            return None  # short side handled by _short_followthrough_worker
        bias = 1

    if bias <= 0:
        return None  # short side handled by _short_followthrough_worker

    _vbrk_squeeze = squeeze_like
    _vbrk_rsi_cap = (
        Decimal(str(getattr(CFG, "vbrk_squeeze_rsi_max", Decimal("85"))))
        if _vbrk_squeeze
        else Decimal(str(getattr(CFG, "long_continuation_rsi_max_vbrk", Decimal("76"))))
    )
    _vbrk_atrp_floor = Decimal("0.0010") if _vbrk_squeeze else Decimal("0.0022")
    if s.rsi >= _vbrk_rsi_cap and Decimal(str(s.atrp)) <= _vbrk_atrp_floor:
        return None
    if _vbrk_squeeze:
        _sq_p_break_min = Decimal(str(getattr(CFG, "vbrk_squeeze_p_break_min", Decimal("0.45"))))
        if p_break < _sq_p_break_min:
            return None
    if s.ema_f is not None and s.px <= s.ema_f:
        return None
    return Intent("buy", "VBRK", score, urgency, Decimal(str(CFG.vbrk_size_mult)), "VOL_BREAKOUT")


def _short_followthrough_worker(s: Snapshot, opp_decay: Decimal = D0) -> Optional[Intent]:
    """[V7.3.5 NEW] Short follow-through / bearish breakdown worker (SFOL).
    Extracted from VBRK to give it independent gating and scoring.
    """
    if s.atrp is None or s.rsi is None:
        return None
    if not getattr(CFG, "enable_shorts", True):
        return None
    if not bool(getattr(CFG, "short_followthrough_enable", True)):
        return None

    p_break = Decimal(str(getattr(s.reg, "p_breakout", D0)))
    p_trend = Decimal(str(getattr(s.reg, "p_trend", D0)))
    squeeze_like = s.reg.name == "SQUEEZE" or "1m:SQUEEZE" in str(getattr(s.reg, "reason", "") or "")

    # Gate: need either breakout probability or trend signal for short follow-through
    p_break_min = Decimal(str(getattr(CFG, "short_followthrough_p_break_min", Decimal("0.60"))))
    p_trend_min_sf = Decimal(str(getattr(CFG, "short_followthrough_p_trend_min", Decimal("0.36"))))
    try:
        p_break_min = max(Decimal("0.40"), p_break_min - (opp_decay * Decimal("0.05")))
    except Exception:
        pass

    if not squeeze_like and p_break < p_break_min and p_trend < p_trend_min_sf:
        return None

    bias = int(getattr(s.reg, "direction_bias", 0) or 0)
    if bias == 0:
        inferred = infer_neutral_breakout_side(s)
        if inferred is None or inferred == "buy":
            return None
        bias = -1
    if bias > 0:
        return None

    # Price must be below EMA
    if s.ema_f is not None and s.px >= s.ema_f:
        return None

    # RSI floor check (prevent entering after extreme exhaustion)
    if s.rsi is not None:
        squeeze_rsi_floor = Decimal(str(getattr(CFG, "short_followthrough_rsi_min", Decimal("24"))))
        if s.reg.name == "CHOP":
            squeeze_rsi_floor = max(
                squeeze_rsi_floor,
                Decimal(str(getattr(CFG, "short_followthrough_rsi_min_chop", Decimal("24")))),
            )
        if Decimal(str(s.rsi)) < squeeze_rsi_floor:
            return None

    urgency = 2 if CFG.vbrk_use_taker else 0
    score = float(p_break) + float(p_trend) + float(getattr(CFG, "short_followthrough_score_boost", Decimal("0.18")))

    # VWAP extension check
    if s.vwap is not None:
        vwap_ext = (s.vwap - s.px) / s.vwap if s.vwap > 0 else D0
        if vwap_ext > Decimal(str(getattr(CFG, "short_followthrough_vwap_ext_max", Decimal("0.020")))):
            return None

    return Intent("sell", "SFOL", score, urgency, Decimal(str(CFG.vbrk_size_mult)), "SHORT_FOLLOWTHROUGH")


def _squeeze_meanrev_worker(s: Snapshot, opp_decay: Decimal = D0) -> Optional[Intent]:
    """Squeeze mean-reversion worker (SQMR).
    [V7.3.5 FIX] Uses p_chop + bbw threshold instead of name=="SQUEEZE" hard gate.
    This lets SQMR fire in tight-range CHOP markets, not just when regime name is exactly SQUEEZE.
    """
    if s.vwap is None or s.rsi is None:
        return None

    # [V7.3.5 FIX] Probability-based gate replaces name check
    # [V7.3.6 FIX] bbw threshold raised from 0.008 to 0.012 (was too tight — SQMR never fired)
    is_squeeze_like = s.reg.name == "SQUEEZE"
    _sqmr_bbw_max = Decimal(str(getattr(CFG, "orch_vol_exp_bbw_min", Decimal("0.012"))))
    is_tight_chop = (
        s.reg.p_chop >= Decimal(str(getattr(CFG, "sqmr_p_chop_min", Decimal("0.65"))))
        and s.reg.bbw is not None
        and Decimal(str(s.reg.bbw)) <= _sqmr_bbw_max
    )
    if not is_squeeze_like and not is_tight_chop:
        return None

    p_chop_min = CFG.sqmr_p_chop_min
    rsi_max = CFG.sqmr_rsi_max
    disc = CFG.sqmr_disc
    try:
        p_chop_min = max(Decimal("0.55"), p_chop_min - (opp_decay * Decimal("0.05")))
        rsi_max = min(Decimal("70"), rsi_max + (opp_decay * Decimal("6")))
        disc = max(Decimal("0.0006"), disc * (D1 - (opp_decay * Decimal("0.50"))))
    except Exception:
        pass

    if s.reg.p_chop < p_chop_min:
        return None

    bias = int(getattr(s.reg, "direction_bias", 0) or 0)
    if bias == 0:
        inferred = infer_neutral_meanrev_side(s, disc, rsi_max)
        if inferred is None:
            return None
        bias = 1 if inferred == "buy" else -1

    if bias > 0:
        if s.rsi > rsi_max:
            return None
        # [V7.4.1 RC-1] SQMR exhaustion guard — Apr 8 trade #6 bought at $2254 into $2270 top.
        # Block SQMR buy when price is near recent high AND falling (exhaustion/rejection pattern).
        if bool(getattr(CFG, "sqmr_exhaustion_guard_enable", True)):
            try:
                _lookback = int(getattr(CFG, "sqmr_exhaustion_lookback", 30))
                _closes = getattr(MKT, "closes_1m", None)
                if _closes and len(_closes) >= _lookback:
                    _recent_high = max(_closes[-_lookback:])
                    _prox_bps = Decimal(str(getattr(CFG, "sqmr_exhaustion_proximity_bps", Decimal("50"))))
                    _dist_from_high = ((_recent_high - s.px) / _recent_high) * Decimal("10000") if _recent_high > 0 else Decimal("9999")
                    _ret5_max = Decimal(str(getattr(CFG, "sqmr_exhaustion_ret5_max", Decimal("-0.0015"))))
                    if _dist_from_high < _prox_bps and s.ret5_1m is not None and Decimal(str(s.ret5_1m)) < _ret5_max:
                        return None  # block: near recent high and falling = exhaustion
            except Exception:
                pass
        if s.px < s.vwap * (D1 - disc):
            score = float(s.reg.p_chop) + float((s.vwap - s.px) / s.vwap) * 100.0
            return Intent("buy", "SQMR", score, 0, Decimal(str(CFG.sqmr_size_mult)), "SQUEEZE_MEANREV")
        return None

    if not getattr(CFG, "enable_shorts", True):
        return None
    p_trend_floor_short = Decimal(str(getattr(CFG, "dip_p_trend_min_chop", Decimal("0.34"))))
    if s.reg.p_trend < p_trend_floor_short:
        return None
    rsi_min = Decimal("100") - rsi_max
    if s.rsi < rsi_min:
        return None
    if s.px > s.vwap * (D1 + disc):
        score = float(s.reg.p_chop) + float((s.px - s.vwap) / s.vwap) * 100.0
        return Intent("sell", "SQMR", score, 0, Decimal(str(CFG.sqmr_size_mult)), "SQUEEZE_MEANREV_SHORT")
    return None


# ----------------------------
# Diagnostics
# ----------------------------
def diagnose_no_intent(s: Snapshot) -> str:
    """[DAILY AUDIT FIX] Lightweight no-intent diagnostics for operator visibility."""
    notes: List[str] = []
    try:
        if s.reg.direction_bias < 0:
            if s.vwap is not None and s.px <= s.vwap * (D1 + CFG.dip_disc_chop):
                notes.append("DIP:no_rally")
            if s.rsi is not None and s.rsi < (Decimal("100") - CFG.dip_rsi_max_chop):
                notes.append("DIP:rsi_low")
            if s.reg.p_trend < CFG.trend_pullback_p_trend_min:
                notes.append("TPB:p_trend_low")
            if s.rsi is not None and not (CFG.trend_pullback_rsi_lo <= s.rsi <= CFG.trend_pullback_rsi_hi):
                notes.append("TPB:rsi_band")
            if s.reg.p_trend < CFG.momo_p_trend_min:
                notes.append("MOMO:p_trend_low")
            if s.atrp is not None and s.atrp < CFG.momo_atrp_min:
                notes.append("MOMO:atrp_low")
            pbreak = Decimal(str(getattr(s.reg, "p_breakout", D0)))
            if pbreak < CFG.short_followthrough_p_break_min:
                notes.append("VBRK:p_break_low")
            if s.reg.p_trend < CFG.short_followthrough_p_trend_min:
                notes.append("VBRK:p_trend_low")
            if s.rsi is not None and s.rsi > CFG.short_followthrough_rsi_max:
                notes.append("VBRK:rsi_not_weak")
            _sf_rsi_min = Decimal(str(getattr(CFG, "short_followthrough_rsi_min", Decimal("20"))))
            if getattr(s.reg, "name", "") == "CHOP":
                _sf_rsi_min = max(_sf_rsi_min, Decimal(str(getattr(CFG, "short_followthrough_rsi_min_chop", Decimal("24")))))
            if s.rsi is not None and s.rsi < _sf_rsi_min:
                notes.append("VBRK:rsi_exhausted")
        elif s.reg.direction_bias > 0:
            if s.vwap is not None and s.px >= s.vwap * (D1 - CFG.dip_disc_chop):
                notes.append("DIP:no_discount")
            if s.reg.p_trend < CFG.trend_pullback_p_trend_min:
                notes.append("TPB:p_trend_low")
            if s.reg.p_trend < CFG.momo_p_trend_min:
                notes.append("MOMO:p_trend_low")
            if s.atrp is not None and s.atrp < CFG.momo_atrp_min:
                notes.append("MOMO:atrp_low")
            try:
                _sq = s.reg.name == "SQUEEZE" or "1m:SQUEEZE" in str(getattr(s.reg, "reason", "") or "")
                _momo_cap = (
                    Decimal(str(getattr(CFG, "momo_squeeze_rsi_max", Decimal("80"))))
                    if _sq
                    else Decimal(str(getattr(CFG, "long_continuation_rsi_max_momo", Decimal("69"))))
                )
                _momo_atrp_floor = Decimal("0.0010") if _sq else Decimal("0.0026")
                if s.rsi is not None and s.atrp is not None:
                    if s.rsi >= _momo_cap and Decimal(str(s.atrp)) <= _momo_atrp_floor:
                        notes.append("MOMO:overheat")
                if s.vwap is not None and s.px > s.vwap * (D1 + CFG.momo_max_premium_vwap):
                    notes.append("MOMO:vwap_premium")
                _vbrk_cap = (
                    Decimal(str(getattr(CFG, "vbrk_squeeze_rsi_max", Decimal("85"))))
                    if _sq
                    else Decimal(str(getattr(CFG, "long_continuation_rsi_max_vbrk", Decimal("76"))))
                )
                _vbrk_atrp_floor = Decimal("0.0010") if _sq else Decimal("0.0022")
                if s.rsi is not None and s.atrp is not None:
                    if s.rsi >= _vbrk_cap and Decimal(str(s.atrp)) <= _vbrk_atrp_floor:
                        notes.append("VBRK:overheat")
            except Exception:
                pass
            if Decimal(str(getattr(s.reg, "p_breakout", D0))) < CFG.vbrk_p_break_min:
                notes.append("VBRK:p_break_low")
        else:
            notes.append("neutral_no_side")
    except Exception:
        notes.append("diag_err")
    return ";".join(notes[:4]) if notes else "no_worker_emit"


# ----------------------------
# Collect intents + orchestrate
# ----------------------------
def collect_intents(s: Snapshot, st: Optional[BotState] = None) -> List[Intent]:
    """[DAILY AUDIT FIX] The brain now sees every eligible worker. No upstream hard router."""
    intents: List[Intent] = []
    opp_decay = getattr(st, "opp_decay", D0) if st is not None else D0
    if s.spread_pct > CFG.max_spread_pct or s.cooldown_left > 0:
        return intents

    for fn in (
        _dip_worker, _trend_pullback_worker, _momo_worker,
        _vol_breakout_worker, _short_followthrough_worker, _squeeze_meanrev_worker,  # [V7.3.5] added SFOL
    ):
        try:
            i = fn(s, opp_decay)
            if i:
                intents.append(i)
        except Exception:
            continue
    return intents


def orchestrate(intents: List[Intent], reg: Regime, st: Optional[BotState]) -> Optional[Intent]:
    """Orchestrator: select exactly one intent per cycle, LONG/SHORT, conflict-safe."""
    if reg.indeterminate or not intents:
        return None

    bias = int(getattr(reg, "direction_bias", 0) or 0)
    want_side = None if bias == 0 else ("buy" if bias > 0 else "sell")
    if want_side == "buy" and not getattr(CFG, "enable_longs", True):
        return None
    if want_side == "sell" and not getattr(CFG, "enable_shorts", True):
        return None

    # Extreme directional pressure veto
    bear_extreme = False
    bull_extreme = False
    try:
        if reg.adx is not None and reg.di_plus is not None and reg.di_minus is not None:
            adx = Decimal(str(reg.adx))
            dmi_gap_bear = Decimal(str(reg.di_minus)) - Decimal(str(reg.di_plus))
            dmi_gap_bull = Decimal(str(reg.di_plus)) - Decimal(str(reg.di_minus))
            bear_extreme = (adx >= CFG.long_bear_adx_min) and (dmi_gap_bear >= CFG.long_bear_dmi_gap)
            bull_extreme = (adx >= CFG.short_bull_adx_min) and (dmi_gap_bull >= CFG.short_bull_dmi_gap)
    except Exception:
        pass

    if want_side == "buy" and bear_extreme:
        allow = [
            it for it in intents
            if it.side == "buy"
            and it.strategy_id in ("MOMO", "VBRK")
            and it.urgency >= 2
            and Decimal(str(reg.p_breakout)) >= CFG.bear_allow_breakout_min
        ]
        if not allow:
            return None
        intents = allow

    if want_side == "sell" and bull_extreme:
        allow = [
            it for it in intents
            if it.side == "sell" and it.strategy_id in ("MOMO", "VBRK", "SFOL") and it.urgency >= 2
        ]
        if not allow:
            return None
        intents = allow

    cand = intents[:] if want_side is None else [i for i in intents if i.side == want_side]
    if not cand:
        return None

    opp = getattr(st, "opp_decay", D0) if st is not None else D0

    pri = {"DIP": 1.00, "TPB": 1.00, "MOMO": 1.00, "VBRK": 1.00, "SQMR": 1.00, "SFOL": 1.00}  # [V7.3.5] added SFOL
    route_w = _brain_route_weights(reg, opp)
    if reg.name == "TREND":
        pri.update({"MOMO": 1.20, "TPB": 1.15, "VBRK": 1.10, "SFOL": 1.05, "DIP": 0.90, "SQMR": 0.85})
    elif reg.name == "BREAKOUT":
        pri.update({"VBRK": 1.25, "MOMO": 1.15, "SFOL": 1.10, "TPB": 1.00, "DIP": 0.85, "SQMR": 0.80})
    elif reg.name == "CHOP":
        pri.update({"DIP": 1.20, "SQMR": 1.15, "SFOL": 1.10, "TPB": 0.95, "MOMO": 0.85, "VBRK": 0.85})
    elif reg.name == "SQUEEZE":
        pri.update({"SQMR": 1.20, "VBRK": 1.15, "SFOL": 1.10, "DIP": 1.00, "TPB": 0.90, "MOMO": 0.85})

    def adj(i: Intent) -> float:
        base = float(i.score)
        bonus = pri.get(i.strategy_id, 1.0)
        route_bonus = float(route_w.get(i.strategy_id, D1))
        alpha_boost = float(opp) * (0.40 if i.strategy_id in ("MOMO", "VBRK", "SFOL") else 0.25)
        result = (base * bonus * route_bonus) + alpha_boost + (0.05 * float(i.urgency))
        # [V7.4.1 RC-4] SFOL CHOP penalty — SFOL lost -178bps in 6 CHOP trades on Apr 8
        if i.strategy_id == "SFOL" and reg.name == "CHOP":
            result -= float(getattr(CFG, "sfol_chop_score_penalty", Decimal("0.20")))
        return result

    if want_side is None and bool(getattr(CFG, "orch_neutral_enable", True)):
        buy_cand = [i for i in cand if i.side == "buy"]
        sell_cand = [i for i in cand if i.side == "sell"]

        # [V7.3.5 FIX] Relaxed neutral path for DIP/SQMR in CHOP regime.
        # In strong CHOP (p_chop >= 0.65), mean-reversion workers are allowed through
        # the neutral filter with p_chop replacing the p_trend/p_breakout requirement.
        _meanrev_in_chop = (
            reg.p_chop >= Decimal("0.65")
            and all(i.strategy_id in ("DIP", "SQMR", "SFOL") for i in cand)
        )

        if buy_cand and sell_cand:
            buy_top = max(buy_cand, key=adj)
            sell_top = max(sell_cand, key=adj)
            buy_score = Decimal(str(adj(buy_top)))
            sell_score = Decimal(str(adj(sell_top)))
            win = buy_top if buy_score >= sell_score else sell_top
            gap = abs(buy_score - sell_score)
            if gap < Decimal(str(getattr(CFG, "orch_neutral_conflict_gap", Decimal("0.18")))):
                return None
            if max(buy_score, sell_score) < Decimal(str(getattr(CFG, "orch_neutral_min_score", Decimal("0.78")))):
                return None
            if not _meanrev_in_chop and (
                Decimal(str(reg.p_trend)) < Decimal(str(getattr(CFG, "orch_neutral_trend_min", Decimal("0.62"))))
                and Decimal(str(reg.p_breakout)) < Decimal(str(getattr(CFG, "orch_neutral_breakout_min", Decimal("0.58"))))
            ):
                return None
            cand = [win]
        elif buy_cand or sell_cand:
            win = max((buy_cand or sell_cand), key=adj)
            if Decimal(str(adj(win))) < Decimal(str(getattr(CFG, "orch_neutral_min_score", Decimal("0.78")))):
                return None
            if not _meanrev_in_chop and (
                Decimal(str(reg.p_trend)) < Decimal(str(getattr(CFG, "orch_neutral_trend_min", Decimal("0.62"))))
                and Decimal(str(reg.p_breakout)) < Decimal(str(getattr(CFG, "orch_neutral_breakout_min", Decimal("0.58"))))
            ):
                return None
            cand = [win]
        else:
            return None

    cand.sort(key=lambda x: (adj(x), x.urgency), reverse=True)
    top = cand[0]
    if adj(top) < 0.60 and opp <= Decimal("0.05"):
        return None

    try:
        top.size_mult = _clamp_dec(top.size_mult, Decimal("0.60"), Decimal("1.40"))
    except Exception:
        pass

    if int(getattr(top, "urgency", 0)) >= 2 and top.strategy_id not in ("MOMO", "VBRK", "SFOL"):  # [V7.3.5] added SFOL
        top.urgency = 1
        top.reason = (top.reason + " | taker_downgraded") if getattr(top, "reason", "") else "taker_downgraded"
    return top


# ----------------------------
# Exit signal
# ----------------------------
def exit_signal(s: Snapshot, st: BotState) -> Tuple[Optional[str], str]:
    """Determine if an exit should be triggered. Returns (kind, reason) or (None, reason)."""
    if st.exit_inflight or st.mode == "EXIT_PENDING":
        return None, "exit_inflight"
    if st.avg_cost is None or st.position_qty <= 0:
        return None, "no_pos"

    side_pos = st.position_side or s.pos_side
    if side_pos == "SHORT":
        if st.peak_price is None or s.px < st.peak_price:
            st.peak_price = s.px
    else:
        if st.peak_price is None or s.px > st.peak_price:
            st.peak_price = s.px

    # Emergency drawdown
    if st.peak_price is not None and st.peak_price > 0:
        if side_pos == "SHORT":
            dd = (s.px - st.peak_price) / st.peak_price
        else:
            dd = (st.peak_price - s.px) / st.peak_price
        dd_th = CFG.emergency_dd_floor
        if s.atrp is not None:
            dd_th = max(dd_th, s.atrp * CFG.emergency_dd_atrp_mult)
        if dd >= dd_th and s.pos_age_min is not None and s.pos_age_min >= 5:
            return "EMERGENCY", f"dd={dd:.3%} th={dd_th:.3%}"

    entry_tag = str(getattr(st, "entry_intent_tag", "") or "")
    if entry_tag in ("MOMO", "VBRK", "SFOL") and s.pos_age_min is not None and s.upnl_pct is not None:  # [V7.3.5] added SFOL
        bias_now = int(getattr(s.reg, "direction_bias", 0) or 0)
        try:
            if bool(getattr(CFG, "continuation_giveback_enable", True)) and st.peak_price is not None:
                if side_pos == "SHORT":
                    best_upnl = (st.avg_cost - st.peak_price) / st.avg_cost if (st.avg_cost and st.avg_cost > 0) else D0
                else:
                    best_upnl = (st.peak_price - st.avg_cost) / st.avg_cost if (st.avg_cost and st.avg_cost > 0) else D0
                giveback_floor = max(
                    Decimal(str(getattr(CFG, "continuation_giveback_floor_pct", Decimal("0.0015")))),
                    Decimal(str(getattr(CFG, "continuation_giveback_fee_floor_mult", Decimal("0.75")))) * Decimal(str(getattr(s, "tp_req", D0))),
                )
                # [V7.4.1 RC-3] Dynamic trailing floor — trade #8 had best=91bps, exited at 50bps
                # Trail at configurable % of peak profit (default 65%)
                if bool(getattr(CFG, "giveback_dynamic_enable", True)) and best_upnl > D0:
                    _trail_pct = Decimal(str(getattr(CFG, "giveback_dynamic_trail_pct", Decimal("0.55"))))
                    _dynamic_floor = best_upnl * _trail_pct
                    giveback_floor = max(giveback_floor, _dynamic_floor)
                if (
                    best_upnl >= Decimal(str(getattr(CFG, "continuation_giveback_arm_pct", Decimal("0.0045"))))
                    and s.upnl_pct <= giveback_floor
                    and (
                        s.reg.p_trend <= Decimal(str(getattr(CFG, "continuation_giveback_p_trend_max", Decimal("0.60"))))
                        or s.reg.p_breakout <= Decimal(str(getattr(CFG, "continuation_giveback_p_break_max", Decimal("0.60"))))
                    )
                    and s.pos_age_min >= 10
                ):
                    return "GIVEBACK", f"tag={entry_tag} best={(best_upnl*100):.2f}% cur={(s.upnl_pct*100):.2f}% floor={(giveback_floor*100):.2f}% p={s.reg.p_trend:.2f}"

                if (
                    s.pos_age_min >= int(getattr(CFG, "continuation_dead_age_min", 45))
                    and best_upnl <= Decimal(str(getattr(CFG, "continuation_dead_best_upnl_min", Decimal("0.0025"))))
                    and s.upnl_pct <= Decimal(str(getattr(CFG, "continuation_dead_cur_upnl_max", Decimal("0.0008"))))
                    and s.reg.p_trend <= Decimal(str(getattr(CFG, "continuation_dead_p_trend_max", Decimal("0.55"))))
                    and s.reg.p_breakout <= Decimal(str(getattr(CFG, "continuation_dead_p_break_max", Decimal("0.55"))))
                ):
                    return "THESIS_DEAD", f"tag={entry_tag} best={(best_upnl*100):.2f}% age={s.pos_age_min}m"

                # [V7.4.1 RC-2] Faster THESIS_DEAD in CHOP — 45min too slow, Apr 8 trade #10
                # held 117min to EMERGENCY. Use shorter timeout when regime is CHOP.
                _chop_dead_age = int(getattr(CFG, "continuation_dead_age_min_chop", 25))
                if (
                    s.reg.name == "CHOP"
                    and s.pos_age_min >= _chop_dead_age
                    and best_upnl <= Decimal(str(getattr(CFG, "continuation_dead_best_upnl_min", Decimal("0.0025"))))
                    and s.upnl_pct <= Decimal(str(getattr(CFG, "continuation_dead_cur_upnl_max_chop", Decimal("0.0015"))))  # looser than standard 0.0008
                ):
                    return "THESIS_DEAD", f"tag={entry_tag} best={(best_upnl*100):.2f}% age={s.pos_age_min}m chop_fast"
        except Exception:
            pass

        bias_now = int(getattr(s.reg, "direction_bias", 0) or 0)
        dir_flip = ((side_pos == "LONG") and bias_now < 0) or ((side_pos == "SHORT") and bias_now > 0)
        dir_bad = dir_flip or (
            bool(getattr(CFG, "momo_thesis_break_allow_neutral", True))
            and (((side_pos == "LONG") and bias_now <= 0) or ((side_pos == "SHORT") and bias_now >= 0))
        )
        if (
            int(getattr(CFG, "momo_thesis_break_min_age_min", 15)) <= s.pos_age_min <= int(getattr(CFG, "momo_thesis_break_max_age_min", 90))
            and s.upnl_pct <= -abs(Decimal(str(getattr(CFG, "momo_thesis_break_loss_pct", Decimal("0.0055")))))
            and s.reg.p_trend <= Decimal(str(getattr(CFG, "momo_thesis_break_p_trend_max", Decimal("0.45"))))
            and s.reg.p_breakout <= Decimal(str(getattr(CFG, "momo_thesis_break_p_break_max", Decimal("0.35"))))
            and dir_bad
        ):
            return "THESIS_BREAK", f"tag={entry_tag} age={s.pos_age_min}m p={s.reg.p_trend:.2f}"
        if (
            s.pos_age_min >= int(getattr(CFG, "momo_thesis_break_stale_age_min", 20))
            and s.upnl_pct <= -abs(Decimal(str(getattr(CFG, "momo_thesis_break_stale_loss_pct", Decimal("0.0030")))))
            and s.reg.p_trend <= Decimal(str(getattr(CFG, "momo_thesis_break_stale_p_trend_max", Decimal("0.35"))))
            and s.reg.p_breakout <= Decimal(str(getattr(CFG, "momo_thesis_break_stale_p_break_max", Decimal("0.40"))))
            and dir_bad
        ):
            return "THESIS_STALE", f"tag={entry_tag} age={s.pos_age_min}m p={s.reg.p_trend:.2f}"

    # Hold-extension check
    if st.hold_until_ts > 0 and s.ts < st.hold_until_ts:
        return None, f"hold_extended_until={int(st.hold_until_ts - s.ts)}s"

    # Time exit
    if s.pos_age_min is not None and s.pos_age_min >= CFG.max_hold_minutes:
        up = s.upnl_pct if s.upnl_pct is not None else Decimal("0")
        bbw = s.reg.bbw if s.reg.bbw is not None else None
        compression = (
            s.reg.p_breakout >= CFG.hold_extend_p_break_min
            and (("SQUEEZE" in (s.reg.reason or "")) or (bbw is not None and bbw <= CFG.hold_extend_bbw_max))
        )
        if compression:
            st.hold_until_ts = s.ts + (CFG.hold_extend_minutes * 60)
            return None, f"extend_hold_breakout p_break={s.reg.p_breakout:.2f}"
        if CFG.time_exit_only_losers:
            if up < D0 and s.reg.p_trend < CFG.bear_bias_gate_p_trend_max:
                return "TIME", f"age={s.pos_age_min}m"
            if up < (s.tp_req * CFG.time_exit_min_edge_mult):
                return None, "hold_deadmoney"
            return "TIME", f"age={s.pos_age_min}m"
        else:
            return "TIME", f"age={s.pos_age_min}m"

    return None, "hold"
