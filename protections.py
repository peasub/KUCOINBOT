#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
protections.py — Formal protections framework for post-event behavioral rules.

PHASE A — Trade Quality Foundation
Borrowed from Freqtrade's protections concept, adapted for our desk bot.
All protections degrade gracefully — if state fields are missing, the check passes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Tuple

from config import CFG
from logger import now_ts
from utils import D0

if TYPE_CHECKING:
    from models import BotState, Intent, Snapshot


def check_entry_allowed(st: "BotState", s: "Snapshot", intent: "Intent") -> Tuple[bool, str]:
    """Check all protections in priority order. Returns (allowed, blocker_reason).
    If any protection blocks, returns (False, "protection_name:reason").
    All checks are best-effort — missing state fields default to allowing entry.
    """
    try:
        # Protection 1: LowConvictionBlock
        # PROVEN: Trade #6 on April 7 entered SFOL SHORT with p=0.17 and got emergency-stopped.
        ok, reason = _check_low_conviction(s)
        if not ok:
            return False, reason

        # Protection 2: EmergencyExitCooldown
        ok, reason = _check_emergency_cooldown(st, intent)
        if not ok:
            return False, reason

        # Protection 3: GivebackGuard
        ok, reason = _check_giveback_guard(st, intent)
        if not ok:
            return False, reason

        # Protection 4: ConsecutiveLossHalt
        ok, reason = _check_consecutive_losses(st)
        if not ok:
            return False, reason

        # Protection 5: MaxDailyLossHalt
        ok, reason = _check_daily_loss(st)
        if not ok:
            return False, reason

    except Exception:
        pass  # if protections logic itself fails, allow entry (degrade gracefully)

    return True, ""


# ----------------------------
# Individual protections
# ----------------------------

def _check_low_conviction(s: "Snapshot") -> Tuple[bool, str]:
    """Hard block when p_trend < threshold. [PROVEN defect from April 7 trade #6]"""
    try:
        min_p = Decimal(str(getattr(CFG, "min_p_trend_for_entry", Decimal("0.20"))))
        if s.reg.p_trend is not None and s.reg.p_trend < min_p:
            return False, f"low_conviction:p_trend={s.reg.p_trend:.2f}<{min_p}"
    except Exception:
        pass
    return True, ""


def _check_emergency_cooldown(st: "BotState", intent: "Intent") -> Tuple[bool, str]:
    """After EMERGENCY exit, impose directional cooldown."""
    try:
        last_reason = str(getattr(st, "prot_last_exit_reason", "") or "")
        if last_reason != "EMERGENCY":
            return True, ""
        last_side = str(getattr(st, "prot_last_exit_side", "") or "")
        # same direction = same side entry as the exited position's entry side
        # If exited SHORT (exit_side=buy), the position was SHORT, entry_side was "sell".
        # Block re-entry in same direction: if last exit was from a short (last_side="sell") and new intent is also "sell".
        if last_side and last_side == intent.side:
            cd_min = float(getattr(CFG, "emergency_cooldown_same_dir_minutes", 30))
            last_ts = float(getattr(st, "prot_last_exit_ts", 0.0) or 0.0)
            elapsed = (now_ts() - last_ts) / 60.0 if last_ts > 0 else 9999
            if elapsed < cd_min:
                return False, f"emergency_cooldown:{elapsed:.0f}m<{cd_min:.0f}m dir={last_side}"
    except Exception:
        pass
    return True, ""


def _check_giveback_guard(st: "BotState", intent: "Intent") -> Tuple[bool, str]:
    """After a position that was profitable but exited at loss, impose same-direction cooldown."""
    try:
        best_exc = getattr(st, "prot_last_exit_best_excursion_bps", None)
        pnl = getattr(st, "prot_last_exit_pnl_bps", None)
        if best_exc is None or pnl is None:
            return True, ""
        best_exc = Decimal(str(best_exc))
        pnl = Decimal(str(pnl))
        # Giveback: was profitable (best > 10bps) but exited at loss (pnl < 0)
        if best_exc > Decimal("10") and pnl < D0:
            last_side = str(getattr(st, "prot_last_exit_side", "") or "")
            if last_side and last_side == intent.side:
                cd_min = float(getattr(CFG, "giveback_cooldown_minutes", 20))
                last_ts = float(getattr(st, "prot_last_exit_ts", 0.0) or 0.0)
                elapsed = (now_ts() - last_ts) / 60.0 if last_ts > 0 else 9999
                if elapsed < cd_min:
                    return False, f"giveback_guard:{elapsed:.0f}m<{cd_min:.0f}m best={best_exc:.0f}bps pnl={pnl:.0f}bps"
    except Exception:
        pass
    return True, ""


def _check_consecutive_losses(st: "BotState") -> Tuple[bool, str]:
    """After N consecutive losing trades, pause all entries for M minutes."""
    try:
        consec = int(getattr(st, "prot_consecutive_losses", 0) or 0)
        max_consec = int(getattr(CFG, "max_consecutive_losses", 3))
        if consec >= max_consec:
            pause_min = float(getattr(CFG, "consecutive_loss_pause_minutes", 45))
            last_ts = float(getattr(st, "prot_last_exit_ts", 0.0) or 0.0)
            elapsed = (now_ts() - last_ts) / 60.0 if last_ts > 0 else 9999
            if elapsed < pause_min:
                return False, f"consecutive_loss_halt:{consec}losses {elapsed:.0f}m<{pause_min:.0f}m"
    except Exception:
        pass
    return True, ""


def _check_daily_loss(st: "BotState") -> Tuple[bool, str]:
    """If cumulative daily PnL exceeds negative threshold, halt for the rest of the day.
    [V7.4.1] Tiered recovery: after N minutes of halt, allow entry at reduced size.
    [PROVEN RC-5] Apr 8: blocked 87 profitable short signals for 3+ hours.
    """
    try:
        daily_pnl = Decimal(str(getattr(st, "prot_daily_pnl_bps", D0) or D0))
        max_loss = Decimal(str(getattr(CFG, "max_daily_loss_bps", Decimal("-200"))))
        if daily_pnl <= max_loss:
            # [V7.4.1] Check tiered recovery
            if bool(getattr(CFG, "daily_loss_recovery_enable", True)):
                last_exit_ts = float(getattr(st, "prot_last_exit_ts", 0.0) or 0.0)
                elapsed_min = (now_ts() - last_exit_ts) / 60.0 if last_exit_ts > 0 else 0
                recovery_after = float(getattr(CFG, "daily_loss_recovery_after_minutes", 120))
                if elapsed_min >= recovery_after:
                    # Allow entry — the engine will apply reduced size via prot_recovery_size_mult
                    st.prot_in_recovery_mode = True  # type: ignore[attr-defined]
                    return True, ""
            return False, f"max_daily_loss:daily_pnl={daily_pnl:.0f}bps<={max_loss:.0f}bps"
    except Exception:
        pass
    return True, ""


# ----------------------------
# State update helpers (called after exits)
# ----------------------------

def update_protection_state_on_exit(
    st: "BotState",
    exit_reason: str,
    entry_side: str,
    pnl_bps,
    best_excursion_bps,
) -> None:
    """Update protection state fields after a position exit. Best-effort."""
    try:
        st.prot_last_exit_reason = exit_reason  # type: ignore[attr-defined]
        st.prot_last_exit_side = entry_side  # type: ignore[attr-defined]
        st.prot_last_exit_ts = now_ts()  # type: ignore[attr-defined]

        pnl = Decimal(str(pnl_bps)) if pnl_bps is not None else D0
        st.prot_last_exit_pnl_bps = pnl  # type: ignore[attr-defined]

        best = Decimal(str(best_excursion_bps)) if best_excursion_bps is not None else D0
        st.prot_last_exit_best_excursion_bps = best  # type: ignore[attr-defined]

        # Consecutive losses tracking
        if pnl < D0:
            st.prot_consecutive_losses = int(getattr(st, "prot_consecutive_losses", 0) or 0) + 1  # type: ignore[attr-defined]
        else:
            st.prot_consecutive_losses = 0  # type: ignore[attr-defined]

        # Daily PnL accumulation
        from logger import vancouver_date
        today = vancouver_date()
        if str(getattr(st, "prot_daily_pnl_reset_date", "") or "") != today:
            st.prot_daily_pnl_bps = D0  # type: ignore[attr-defined]
            st.prot_daily_pnl_reset_date = today  # type: ignore[attr-defined]
        st.prot_daily_pnl_bps = Decimal(str(getattr(st, "prot_daily_pnl_bps", D0) or D0)) + pnl  # type: ignore[attr-defined]

    except Exception:
        pass  # never crash


def update_maturity_on_entry(st: "BotState", worker_tag: str, side: str) -> None:
    """Update continuation maturity tracking after a successful entry."""
    try:
        last_tag = str(getattr(st, "prot_last_worker_tag", "") or "")
        last_side = str(getattr(st, "prot_last_entry_side", "") or "")
        if last_tag == worker_tag and last_side == side:
            st.prot_same_direction_streak = int(getattr(st, "prot_same_direction_streak", 0) or 0) + 1  # type: ignore[attr-defined]
        else:
            st.prot_same_direction_streak = 0  # type: ignore[attr-defined]
        st.prot_last_worker_tag = worker_tag  # type: ignore[attr-defined]
        st.prot_last_entry_side = side  # type: ignore[attr-defined]
    except Exception:
        pass


def continuation_maturity_penalty(st: "BotState", intent: "Intent") -> Decimal:
    """Returns a score penalty (0.0 to 0.30) for continuation entries.
    [V7.4.1] Steepened from 0.05/step to configurable (default 0.10/step).
    [V7.4.1] Hard block after max_streak consecutive same-worker same-direction.
    [PROVEN] April 8: SFOL SHORT fired 4x in CHOP, trades #2-5 lost -82 bps net.
    """
    try:
        last_tag = str(getattr(st, "prot_last_worker_tag", "") or "")
        last_side = str(getattr(st, "prot_last_entry_side", "") or "")
        if last_tag == intent.strategy_id and last_side == intent.side:
            streak = int(getattr(st, "prot_same_direction_streak", 0) or 0)
            # [V7.4.1] Hard cap — return infinite penalty (blocks entry)
            max_streak = int(getattr(CFG, "maturity_max_streak", 3))
            if streak >= max_streak:
                return Decimal("9.99")  # effectively blocks any entry
            # [V7.4.1] Steepened from 0.05 to configurable
            step = Decimal(str(getattr(CFG, "maturity_penalty_per_step", Decimal("0.10"))))
            penalty = min(Decimal("0.30"), step * Decimal(str(streak + 1)))
            return penalty
    except Exception:
        pass
    return D0
