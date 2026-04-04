#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
indicators.py — Pure technical indicator functions (no side effects, no I/O).

CHANGE LOG:
  MOVED    : ema, rsi, atr, bollinger_bandwidth, directional_efficiency, adx_wilder (lines 747–861)
  MOVED    : _zscore_last, _sigmoid (lines 985–1007)
  MOVED    : _er_series_f, _bbw_series_f, _adx_series_f (lines 1009–1094)
  PRESERVED: All formulas, rounding, and Wilder-smoothing logic exactly as in the original.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import List, Optional, Tuple

from utils import D0, D1


# ----------------------------
# Classic indicators (Decimal precision)
# ----------------------------
def ema(vals: List[Decimal], length: int) -> Optional[Decimal]:
    """Exponential moving average (EMA) over the last `length` values."""
    if len(vals) < length or length <= 0:
        return None
    k = Decimal("2") / (Decimal(length) + Decimal("1"))
    e = vals[0]
    for v in vals[1:]:
        e = (v * k) + (e * (D1 - k))
    return e


def rsi(closes: List[Decimal], length: int) -> Optional[Decimal]:
    """Wilder RSI over the last `length` periods."""
    if len(closes) < length + 1:
        return None
    gains = []
    losses = []
    for i in range(1, length + 1):
        ch = closes[-i] - closes[-i - 1]
        if ch >= 0:
            gains.append(ch)
            losses.append(D0)
        else:
            gains.append(D0)
            losses.append(-ch)
    avg_g = sum(gains) / Decimal(length)
    avg_l = sum(losses) / Decimal(length)
    if avg_l == 0:
        return Decimal("100")
    rs = avg_g / avg_l
    return Decimal("100") - (Decimal("100") / (D1 + rs))


def atr(
    highs: List[Decimal],
    lows: List[Decimal],
    closes: List[Decimal],
    length: int,
) -> Optional[Decimal]:
    """Simple ATR (arithmetic mean of true ranges)."""
    if len(closes) < length + 1:
        return None
    trs: List[Decimal] = []
    for i in range(-length, 0):
        h = highs[i]
        lo = lows[i]
        pc = closes[i - 1]
        tr = max(h - lo, abs(h - pc), abs(lo - pc))
        trs.append(tr)
    return sum(trs) / Decimal(length)


def bollinger_bandwidth(closes: List[Decimal], length: int) -> Optional[Decimal]:
    """Bollinger Band Width = (upper - lower) / middle."""
    if len(closes) < length:
        return None
    window = closes[-length:]
    mean = sum(window) / Decimal(length)
    var = sum((x - mean) ** 2 for x in window) / Decimal(length)
    sd = Decimal(str(math.sqrt(float(var))))
    if mean == 0:
        return None
    upper = mean + sd * Decimal("2")
    lower = mean - sd * Decimal("2")
    return (upper - lower) / mean


def directional_efficiency(closes: List[Decimal], length: int) -> Optional[Decimal]:
    """Kaufman Efficiency Ratio: net change / sum of absolute moves."""
    if len(closes) < length + 1:
        return None
    start = closes[-length - 1]
    end = closes[-1]
    change = abs(end - start)
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - length, len(closes)))
    if path == 0:
        return Decimal("0")
    return change / path


def adx_wilder(
    highs: List[Decimal],
    lows: List[Decimal],
    closes: List[Decimal],
    n: int,
) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """Return (ADX, +DI, -DI) using Wilder smoothing. Uses floats internally for speed."""
    if len(closes) < n + 2:
        return None, None, None

    H = [float(x) for x in highs]
    L = [float(x) for x in lows]
    C = [float(x) for x in closes]

    tr = [0.0]
    pdm = [0.0]
    mdm = [0.0]
    for i in range(1, len(C)):
        up = H[i] - H[i - 1]
        dn = L[i - 1] - L[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])))

    tr_s = sum(tr[1 : n + 1])
    pdm_s = sum(pdm[1 : n + 1])
    mdm_s = sum(mdm[1 : n + 1])

    def safe_div(a: float, b: float) -> float:
        return a / b if b > 0 else 0.0

    di_plus: List[float] = []
    di_minus: List[float] = []
    dx: List[float] = []
    for i in range(n + 1, len(tr)):
        tr_s = tr_s - (tr_s / n) + tr[i]
        pdm_s = pdm_s - (pdm_s / n) + pdm[i]
        mdm_s = mdm_s - (mdm_s / n) + mdm[i]
        pdi = 100.0 * safe_div(pdm_s, tr_s)
        mdi = 100.0 * safe_div(mdm_s, tr_s)
        di_plus.append(pdi)
        di_minus.append(mdi)
        denom = pdi + mdi
        dx.append(100.0 * safe_div(abs(pdi - mdi), denom))

    if len(dx) < n:
        return (
            None,
            Decimal(str(di_plus[-1])) if di_plus else None,
            Decimal(str(di_minus[-1])) if di_minus else None,
        )

    adx_v = sum(dx[:n]) / n
    for j in range(n, len(dx)):
        adx_v = ((adx_v * (n - 1)) + dx[j]) / n

    return (
        Decimal(str(adx_v)),
        Decimal(str(di_plus[-1])),
        Decimal(str(di_minus[-1])),
    )


# ----------------------------
# Probabilistic regime helpers
# ----------------------------
def _zscore_last(vals: List[float], window: int) -> Tuple[float, bool]:
    """Return (z_last, indeterminate). Indeterminate when history is too short or sigma ~ 0."""
    if not vals:
        return 0.0, True
    w = vals[-window:] if len(vals) >= window else vals
    if len(w) < max(20, window // 4):
        return 0.0, True
    mu = sum(w) / len(w)
    var = sum((x - mu) * (x - mu) for x in w) / max(1, (len(w) - 1))
    sig = math.sqrt(var)
    if sig < 1e-12:
        return 0.0, True
    z = (w[-1] - mu) / sig
    return z, False


def _sigmoid(x: float, k: float, theta: float) -> float:
    """Numerically stable sigmoid used in probabilistic regime classification."""
    y = k * (x - theta)
    if y >= 0:
        ez = math.exp(-y)
        return 1.0 / (1.0 + ez)
    ez = math.exp(y)
    return ez / (1.0 + ez)


# ----------------------------
# Float-precision series functions (for Z-score rolling windows)
# ----------------------------
def _er_series_f(C: List[float], n: int) -> List[float]:
    """Compute ER (Kaufman Efficiency Ratio) series in O(L)."""
    L = len(C)
    if L < n + 2:
        return []
    diffs = [0.0] * L
    for i in range(1, L):
        diffs[i] = abs(C[i] - C[i - 1])
    out: List[float] = []
    roll = sum(diffs[1 : n + 1])
    for i in range(n, L):
        if i > n:
            roll += diffs[i] - diffs[i - n]
        net = abs(C[i] - C[i - n])
        out.append(net / roll if roll > 0 else 0.0)
    return out


def _bbw_series_f(C: List[float], n: int) -> List[float]:
    """Compute BBW series (normalised width) in O(L) using rolling mean/std."""
    L = len(C)
    if L < n:
        return []
    out: List[float] = []
    s = sum(C[:n])
    ss = sum(x * x for x in C[:n])
    for i in range(n - 1, L):
        if i >= n:
            x_add = C[i]
            x_rem = C[i - n]
            s += x_add - x_rem
            ss += x_add * x_add - x_rem * x_rem
        mean = s / n
        var = max(0.0, (ss / n) - (mean * mean))
        sd = math.sqrt(var)
        bbw = (4.0 * sd / mean) if mean != 0 else 0.0
        out.append(bbw)
    return out


def _adx_series_f(
    H: List[float], L: List[float], C: List[float], n: int
) -> Tuple[List[float], List[float], List[float]]:
    """Compute ADX/+DI/-DI series (float) using Wilder smoothing."""
    m = len(C)
    if m < n + 2:
        return [], [], []

    tr = [0.0] * m
    pdm = [0.0] * m
    mdm = [0.0] * m
    for i in range(1, m):
        up = H[i] - H[i - 1]
        dn = L[i - 1] - L[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))

    atr_s = sum(tr[1 : n + 1])
    pdm_s = sum(pdm[1 : n + 1])
    mdm_s = sum(mdm[1 : n + 1])

    pdi_list: List[float] = []
    mdi_list: List[float] = []
    dx_list: List[float] = []

    for i in range(n, m):
        if i > n:
            atr_s = atr_s - (atr_s / n) + tr[i]
            pdm_s = pdm_s - (pdm_s / n) + pdm[i]
            mdm_s = mdm_s - (mdm_s / n) + mdm[i]
        if atr_s <= 0:
            pdi = 0.0
            mdi = 0.0
        else:
            pdi = 100.0 * (pdm_s / atr_s)
            mdi = 100.0 * (mdm_s / atr_s)
        pdi_list.append(pdi)
        mdi_list.append(mdi)
        denom = pdi + mdi
        dx_list.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)

    if len(dx_list) < n:
        return [], [], []

    adx_v = sum(dx_list[:n]) / n
    adx_list: List[float] = [adx_v]
    for i in range(n, len(dx_list)):
        adx_v = ((adx_v * (n - 1)) + dx_list[i]) / n
        adx_list.append(adx_v)
    return adx_list, pdi_list, mdi_list
