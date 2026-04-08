#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
logger.py — Logging, timezone utilities, and global monitoring state.

CHANGE LOG:
  MOVED    : _get_tz, fmt_ts, vancouver_date, Logger class (lines 583–704)
  MOVED    : BOT_VERSION, LOG, GLOBAL_STATE_REF, LAT_WATCH (lines 707–710)
  PRESERVED: All logic, file-rotation behaviour, CSV format, and console format exactly.
"""

from __future__ import annotations

import asyncio
import csv
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore


# ----------------------------
# Time helpers
# ----------------------------
def _get_tz():
    tz_name = os.getenv("BOT_LOG_TZ", "America/Vancouver")
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


LOG_TZ = _get_tz()


def now_ts() -> float:
    """Current wall-clock time as a Unix timestamp (float seconds)."""
    return time.time()


def fmt_ts(ts: Optional[float] = None) -> str:
    """Format a timestamp to ISO-8601 in the configured timezone."""
    ts = now_ts() if ts is None else ts
    if LOG_TZ is not None:
        try:
            import datetime as _dt
            return _dt.datetime.fromtimestamp(ts, LOG_TZ).isoformat(timespec="seconds")
        except Exception:
            pass
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except Exception:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def vancouver_date(ts: Optional[float] = None) -> str:
    """Return 'YYYY-MM-DD' in the configured timezone (used for log file rotation)."""
    ts = now_ts() if ts is None else ts
    try:
        import datetime as _dt
        if LOG_TZ is not None:
            return _dt.datetime.fromtimestamp(ts, LOG_TZ).strftime("%Y-%m-%d")
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d", time.localtime(ts))


# ----------------------------
# Logger
# ----------------------------
class Logger:
    """Safe logger that writes BOTH human-readable console lines and CSV rows to disk.

    CSV format (easy Google Sheets import):
      ts,level,code,detail
    """

    def __init__(self, version: str):
        self.version = version
        self.base_dir = Path.home() / "Desktop" / version
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._day = vancouver_date()
        self._path = self.base_dir / f"{version}_{self._day}.csv"
        self._fp = open(self._path, "a", newline="", encoding="utf-8")
        self._csvw = csv.writer(self._fp)
        # header if new/empty
        try:
            if self._path.stat().st_size == 0:
                self._csvw.writerow(["ts", "level", "code", "detail"])
                self._fp.flush()
        except Exception:
            pass
        self._lock = asyncio.Lock()

    async def _rotate_if_needed(self):
        day = vancouver_date()
        if day != self._day:
            try:
                self._fp.close()
            except Exception:
                pass
            self._day = day
            self._path = self.base_dir / f"{self.version}_{self._day}.csv"
            self._fp = open(self._path, "a", newline="", encoding="utf-8")
            self._csvw = csv.writer(self._fp)
            try:
                if self._path.stat().st_size == 0:
                    self._csvw.writerow(["ts", "level", "code", "detail"])
                    self._fp.flush()
            except Exception:
                pass

    @staticmethod
    def _split_msg(msg: str) -> Tuple[str, str]:
        msg = msg.strip()
        if not msg:
            return ("", "")
        if " " in msg:
            code, rest = msg.split(" ", 1)
            return (code, rest.strip())
        return (msg, "")

    async def log(self, level: str, msg: str):
        # logging must never crash the engine
        try:
            async with self._lock:
                await self._rotate_if_needed()
                ts = fmt_ts()
                lvl = level.upper()
                code, detail = self._split_msg(msg)
                try:
                    self._csvw.writerow([ts, lvl, code, detail])
                    self._fp.flush()
                except Exception:
                    pass
                try:
                    line = f"[{ts}] {lvl} {msg}"
                    print(line, flush=True)
                except Exception:
                    pass
        except Exception:
            try:
                print(f"[{fmt_ts()}] LOGFAIL {level} {msg}", flush=True)
            except Exception:
                pass


# ----------------------------
# Global singletons — PRESERVED
# ----------------------------
BOT_VERSION = os.getenv("BOT_VERSION", "kucoin_bot_V7.4.0")  # [AUDIT FIX] V7.3.5 — TP edge scaling, SQMR prob gate, SFOL worker, CHOP fixes
LOG = Logger(BOT_VERSION)

# Reference to the live BotState (set by engine_loop at startup).
# Used by the latency watchdog for out-of-band access.
GLOBAL_STATE_REF: Optional["BotState"] = None  # type: ignore[name-defined]

# Latency watchdog shared state dict (deque of lag samples + trip timestamp)
LAT_WATCH: dict = {"samples": deque(maxlen=240), "tripped_until": 0.0}
