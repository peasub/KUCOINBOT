#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state.py — BotState persistence (save/load to JSON on disk).

CHANGE LOG:
  MOVED    : STATE_FILE path (line 1362)
  MOVED    : state_save (lines 1364–1402), state_load (lines 1404–1458)
  PRESERVED: All serialization/deserialization logic, field exclusions, and error-swallowing exactly.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import Optional

from logger import LOG
from models import BotState, OrderRef
from utils import D0


# ----------------------------
# Persistence path
# ----------------------------
STATE_FILE = Path.home() / "Desktop" / LOG.version / "state.json"


# ----------------------------
# Save
# ----------------------------
def state_save(st: BotState) -> None:
    try:
        data = dataclasses.asdict(st)

        # Encode order refs (price + size to strings for JSON safety)
        def enc_order(o: Optional[dict]):
            if o is None:
                return None
            for kk in ["price", "size"]:
                if kk in o:
                    o[kk] = str(o[kk])
            return o

        data["entry_order"] = enc_order(data.get("entry_order"))
        data["tp1_order"]   = enc_order(data.get("tp1_order"))
        data["tp2_order"]   = enc_order(data.get("tp2_order"))
        data["exit_order"]  = enc_order(data.get("exit_order"))

        # All Decimal scalar fields must be serialised to strings
        _decimal_fields = [
            "position_qty", "avg_cost", "peak_price",
            "trade_tp1_eff", "trade_tp2_eff",
            "trade_vol_metric", "trade_vol_min", "trade_vol_max", "trade_vol_norm",
            "trade_tp_base",
            "entry_tp1_eff", "entry_tp2_eff",
            # Additional Decimal fields that must be stringified before JSON
            "entry_qty_hint", "adverse_sel_ema_bps",
        ]
        for k in _decimal_fields:
            v = data.get(k)
            if v is not None and not isinstance(v, str):
                data[k] = str(v)

        data["trade_tp_mode"] = getattr(st, "trade_tp_mode", "")

        # Do not persist volatile caches (balances/obi/entry-decay). Keep state minimal.
        for kk in [
            "bal_q_free", "bal_q_total", "bal_b_free", "bal_b_total",
            "bal_q_liab", "bal_b_liab",
            "last_bal_refresh_ts", "force_bal_refresh",
            "entry_last_replace_ts", "entry_replace_count",
            "entry_intent_tag", "entry_intent_urg",
            "opp_decay", "last_regime_name", "last_pause_log_ts",
            "last_entry_attempt_ts", "entry_price_hint", "entry_qty_hint", "entry_side_hint",
            "ghost_exit_order_id", "ghost_exit_side", "ghost_exit_guard_until",
            "last_ghost_exit_poll_ts",
            # Monitoring state that resets on restart
            "adverse_sel_ema_bps", "adverse_sel_samples",
            "pending_markout_ts", "pending_markout_px", "pending_markout_side",
        ]:
            data.pop(kk, None)

        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(STATE_FILE)
    except Exception:
        # never crash
        pass


# ----------------------------
# Load
# ----------------------------
def state_load() -> BotState:
    try:
        if not STATE_FILE.exists():
            return BotState()
        data = json.loads(STATE_FILE.read_text())
        st = BotState()
        st.mode = data.get("mode", "FLAT")
        st.position_side = data.get("position_side", None) or None  # [AUDIT FIX RC-6]
        st.position_dir = int(data.get("position_dir", 0) or 0)    # [AUDIT FIX RC-6]
        st.position_qty = Decimal(data.get("position_qty", "0"))
        st.avg_cost = Decimal(data["avg_cost"]) if data.get("avg_cost") else None
        st.peak_price = Decimal(data["peak_price"]) if data.get("peak_price") else None
        st.pos_open_ts = float(data.get("pos_open_ts", 0.0) or 0.0)
        st.cooldown_until = float(data.get("cooldown_until", 0.0) or 0.0)
        st.exit_inflight = bool(data.get("exit_inflight", False))
        st.exit_attempts = int(data.get("exit_attempts", 0) or 0)
        st.last_exit_ts = float(data.get("last_exit_ts", 0.0) or 0.0)
        st.last_entry_fill_ts = float(data.get("last_entry_fill_ts", 0.0) or 0.0)
        st.hold_until_ts = float(data.get("hold_until_ts", 0.0) or 0.0)
        st.last_tp_reprice_ts = float(data.get("last_tp_reprice_ts", 0.0) or 0.0)

        st.trade_tp1_eff = Decimal(data["trade_tp1_eff"]) if data.get("trade_tp1_eff") else None
        st.trade_tp2_eff = Decimal(data["trade_tp2_eff"]) if data.get("trade_tp2_eff") else None
        st.trade_tp_mode = data.get("trade_tp_mode", "")
        st.trade_vol_metric = Decimal(data["trade_vol_metric"]) if data.get("trade_vol_metric") else None
        st.trade_vol_min = Decimal(data["trade_vol_min"]) if data.get("trade_vol_min") else None
        st.trade_vol_max = Decimal(data["trade_vol_max"]) if data.get("trade_vol_max") else None
        st.trade_vol_norm = Decimal(data["trade_vol_norm"]) if data.get("trade_vol_norm") else None
        st.trade_tp_base = Decimal(data["trade_tp_base"]) if data.get("trade_tp_base") else None
        st.entry_tp1_eff = Decimal(data["entry_tp1_eff"]) if data.get("entry_tp1_eff") else None
        st.entry_tp2_eff = Decimal(data["entry_tp2_eff"]) if data.get("entry_tp2_eff") else None
        st.last_tp_modify_ts = float(data.get("last_tp_modify_ts", 0.0) or 0.0)
        st.last_tp_eval_bucket = int(data.get("last_tp_eval_bucket", 0) or 0)
        st.pause_until = float(data.get("pause_until", 0.0) or 0.0)
        st.halt_reason = data.get("halt_reason", "")

        def dec_order(o):
            if not o:
                return None
            return OrderRef(
                order_id=o["order_id"],
                client_oid=o["client_oid"],
                side=o["side"],
                price=Decimal(str(o["price"])),
                size=Decimal(str(o["size"])),
                created_ts=float(o["created_ts"]),
                purpose=o.get("purpose", ""),
            )

        st.entry_order = dec_order(data.get("entry_order"))
        st.tp1_order = dec_order(data.get("tp1_order"))
        st.tp2_order = dec_order(data.get("tp2_order"))
        st.exit_order = dec_order(data.get("exit_order"))
        st.err_ts = [float(x) for x in data.get("err_ts", [])]
        st.err_ts_fatal = [float(x) for x in data.get("err_ts_fatal", [])]  # [AUDIT FIX RC-7]
        return st
    except Exception:
        return BotState()
