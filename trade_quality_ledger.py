#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trade_quality_ledger.py — Append-only trade quality ledger for every entry candidate.

PHASE A — Trade Quality Foundation
Records every trade candidate (entered, rejected, or missed) and every exit.
This is the core dataset for measuring and improving trade quality.

All writes are best-effort: if the ledger file is missing or a write fails,
the bot continues trading normally.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Optional

from logger import LOG

# ----------------------------
# Ledger file path
# ----------------------------
_LEDGER_DIR = Path.home() / "Desktop" / LOG.version
_LEDGER_PATH = _LEDGER_DIR / "trade_quality_ledger.csv"

# Fixed blocker family vocabulary
BLOCKER_FAMILIES = frozenset({
    "",  # no blocker
    "regime_unsuitable",
    "no_directional_bias",
    "score_too_low",
    "edge_too_thin",
    "falling_knife",
    "cooldown_active",
    "quality_reject_score",
    "quality_reject_edge",
    "low_conviction",
    "maturity_exhausted",
    "protection_block",
    "neutral_no_side",
    "ret5_impulse",
    "ret5_rally",
    "orch_standdown",
})


# ----------------------------
# Record dataclass
# ----------------------------
@dataclass
class TradeQualityRecord:
    ts: float                                   # unix timestamp
    event: str                                  # ENTRY_TAKEN, ENTRY_REJECTED, ENTRY_MISSED, EXIT_DONE
    regime_name: str                            # TREND, MIXED, CHOP, SQUEEZE, UNKNOWN
    p_trend: str                                # stringified Decimal
    p_chop: str
    p_breakout: str
    direction_bias: int                         # -1, 0, +1
    worker_tag: str                             # DIP, SFOL, MOMO, TPULL, VBRK, SQMR
    raw_score: str                              # stringified Decimal
    orch_adjusted_score: str
    blocker_family: str                         # from BLOCKER_FAMILIES
    edge_bps: str
    side: str                                   # buy or sell
    entry_px: str                               # "" if not filled
    exit_px: str                                # "" until exit
    best_excursion_bps: str                     # "" until exit
    worst_excursion_bps: str                    # "" until exit
    hold_seconds: str                           # "" until exit
    realized_pnl_bps: str                       # "" until exit
    exit_reason: str                            # TP1, TP2, EMERGENCY, THESIS_STALE, MARKET, ""
    maturity_score: str                         # "" if N/A
    notes: str


# ----------------------------
# Column names (order matters — must match dataclass field order)
# ----------------------------
_COLUMNS = [f.name for f in fields(TradeQualityRecord)]


def _ensure_header() -> None:
    """Create ledger file with header if it doesn't exist."""
    try:
        _LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        if not _LEDGER_PATH.exists() or _LEDGER_PATH.stat().st_size == 0:
            with open(_LEDGER_PATH, "w", newline="", encoding="utf-8") as fp:
                w = csv.writer(fp)
                w.writerow(_COLUMNS)
    except Exception:
        pass  # never crash


def record_trade_quality(rec: TradeQualityRecord) -> None:
    """Append one record to the ledger CSV. Best-effort, never crashes."""
    try:
        _ensure_header()
        row = [getattr(rec, col, "") for col in _COLUMNS]
        with open(_LEDGER_PATH, "a", newline="", encoding="utf-8") as fp:
            w = csv.writer(fp)
            w.writerow(row)
            fp.flush()
    except Exception:
        pass  # never crash the bot


# ----------------------------
# Convenience builders
# ----------------------------
def _dec_str(v) -> str:
    """Safely stringify a Decimal or None to a ledger-safe string."""
    if v is None:
        return ""
    try:
        return str(v)
    except Exception:
        return ""


def build_entry_taken_record(
    ts: float,
    regime_name: str, p_trend, p_chop, p_breakout,
    direction_bias: int,
    worker_tag: str, raw_score, orch_adjusted_score,
    edge_bps, side: str, entry_px,
    maturity_score=None, notes: str = "",
) -> TradeQualityRecord:
    return TradeQualityRecord(
        ts=ts, event="ENTRY_TAKEN",
        regime_name=regime_name,
        p_trend=_dec_str(p_trend), p_chop=_dec_str(p_chop), p_breakout=_dec_str(p_breakout),
        direction_bias=direction_bias,
        worker_tag=worker_tag,
        raw_score=_dec_str(raw_score), orch_adjusted_score=_dec_str(orch_adjusted_score),
        blocker_family="", edge_bps=_dec_str(edge_bps),
        side=side, entry_px=_dec_str(entry_px),
        exit_px="", best_excursion_bps="", worst_excursion_bps="",
        hold_seconds="", realized_pnl_bps="", exit_reason="",
        maturity_score=_dec_str(maturity_score), notes=notes,
    )


def build_entry_rejected_record(
    ts: float,
    regime_name: str, p_trend, p_chop, p_breakout,
    direction_bias: int,
    worker_tag: str, raw_score, orch_adjusted_score,
    blocker_family: str, edge_bps, side: str,
    maturity_score=None, notes: str = "",
) -> TradeQualityRecord:
    return TradeQualityRecord(
        ts=ts, event="ENTRY_REJECTED",
        regime_name=regime_name,
        p_trend=_dec_str(p_trend), p_chop=_dec_str(p_chop), p_breakout=_dec_str(p_breakout),
        direction_bias=direction_bias,
        worker_tag=worker_tag,
        raw_score=_dec_str(raw_score), orch_adjusted_score=_dec_str(orch_adjusted_score),
        blocker_family=blocker_family, edge_bps=_dec_str(edge_bps),
        side=side, entry_px="",
        exit_px="", best_excursion_bps="", worst_excursion_bps="",
        hold_seconds="", realized_pnl_bps="", exit_reason="",
        maturity_score=_dec_str(maturity_score), notes=notes,
    )


def build_exit_record(
    ts: float,
    regime_name: str, p_trend, p_chop, p_breakout,
    direction_bias: int,
    worker_tag: str, side: str,
    entry_px, exit_px,
    best_excursion_bps, worst_excursion_bps,
    hold_seconds, realized_pnl_bps,
    exit_reason: str, notes: str = "",
) -> TradeQualityRecord:
    return TradeQualityRecord(
        ts=ts, event="EXIT_DONE",
        regime_name=regime_name,
        p_trend=_dec_str(p_trend), p_chop=_dec_str(p_chop), p_breakout=_dec_str(p_breakout),
        direction_bias=direction_bias,
        worker_tag=worker_tag,
        raw_score="", orch_adjusted_score="",
        blocker_family="", edge_bps="",
        side=side, entry_px=_dec_str(entry_px), exit_px=_dec_str(exit_px),
        best_excursion_bps=_dec_str(best_excursion_bps),
        worst_excursion_bps=_dec_str(worst_excursion_bps),
        hold_seconds=_dec_str(hold_seconds),
        realized_pnl_bps=_dec_str(realized_pnl_bps),
        exit_reason=exit_reason,
        maturity_score="", notes=notes,
    )
