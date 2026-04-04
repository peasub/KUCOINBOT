#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client_patch.py — V7.1.0 KuCoinClient method injection (margin + orders).

CHANGE LOG:
  MOVED    : All _kc_* free functions (lines 6696–6997) — PRESERVED exactly.
  MOVED    : setattr binding block (lines 6974–7000) — PRESERVED exactly.
  PRESERVED: Every function body, every setattr call, every comment.

⚠️  IMPORTANT — WHY THIS FILE EXISTS:
  In earlier versions, methods like accounts_any / place_limit_any were not
  attached to KuCoinClient due to an indentation slip. This patch block defines
  canonical implementations and binds them onto KuCoinClient non-invasively.

  In the refactored project, the bug is fixed in client.py (methods are now
  properly inside the class). However, this patch block is STILL imported and
  run at startup as a live safety net:
    - It force-binds improved versions of fill/open-order helpers even when
      stale class definitions already exist.
    - The line `if (not hasattr(KuCoinClient, _name)) or _name in {...}:` means
      it will override specific methods unconditionally. This is intentional.
    - If you deploy a partial script or a version mismatch, the patch saves you.

  DO NOT REMOVE this file or skip its import.
  Import it in main.py AFTER importing client.py.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from client import KuCoinClient
from config import CFG
from utils import D0


# ----------------------------
# Trade-type candidate helpers
# ----------------------------
def _kc_margin_trade_type_candidates(self) -> List[str]:
    pref = str(
        getattr(self, "_margin_trade_type_working",
                getattr(CFG, "margin_trade_type", "MARGIN_TRADE")) or "MARGIN_TRADE"
    )
    fb = str(getattr(CFG, "margin_trade_type_fallback", "MARGIN_TRADE") or "MARGIN_TRADE")
    out: List[str] = []
    for cand in (pref, fb, "MARGIN_TRADE"):
        cand = str(cand or "").strip()
        if cand and cand not in out:
            out.append(cand)
    return out


def _kc_trade_type_param_error(err: Exception) -> bool:
    s = str(err)
    return ("400400" in s) or ("Only Support margin trade order" in s)


# ----------------------------
# Margin account helpers (improved versions with candidate retry)
# ----------------------------
def _kc_margin_accounts(self) -> List[dict]:
    """Cross margin balances (assets + liabilities). GET /api/v3/margin/accounts"""
    j = self._request("GET", "/api/v3/margin/accounts")
    data = j.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        maybe = data.get("accounts") or data.get("items") or data.get("list")
        if isinstance(maybe, list):
            return maybe
        out: List[dict] = []
        for _k, _v in data.items():
            if isinstance(_v, dict):
                out.append(_v)
        return out
    return []


def _kc_isolated_accounts(self, symbol: str, quote_currency: str) -> dict:
    """Isolated margin account info. GET /api/v3/isolated/accounts"""
    now = time.time()
    cache_ts = getattr(self, "_iso_cache_ts", 0.0)
    cache_key = getattr(self, "_iso_cache_key", "")
    if (now - cache_ts) < 0.8 and cache_key == f"{symbol}|{quote_currency}":
        return getattr(self, "_iso_cache_data", {}) or {}

    params = {"symbol": symbol, "quoteCurrency": quote_currency, "queryType": "ISOLATED"}
    j = self._request("GET", "/api/v3/isolated/accounts", params=params)
    data = j.get("data", {}) or {}

    self._iso_cache_ts = now
    self._iso_cache_key = f"{symbol}|{quote_currency}"
    self._iso_cache_data = data
    return data


def _kc_accounts_any(self, currency: str) -> Tuple[Decimal, Decimal, Decimal]:
    """Unified balance accessor: (available, total, liability)."""
    if str(getattr(CFG, "account_mode", "spot")).lower() != "margin":
        free, total = self.accounts(currency)
        return free, total, D0

    base, quote = CFG.symbol.split("-")

    if bool(getattr(CFG, "margin_isolated", False)):
        data = self.isolated_accounts(CFG.symbol, quote)
        assets = data.get("assets", []) or []
        rec = None
        for it in assets:
            if isinstance(it, dict) and str(it.get("symbol")) == CFG.symbol:
                rec = it
                break
        if rec is None and assets and isinstance(assets[0], dict):
            rec = assets[0]
        if not isinstance(rec, dict):
            return D0, D0, D0

        if currency == base:
            a = rec.get("baseAsset", {}) or {}
        elif currency == quote:
            a = rec.get("quoteAsset", {}) or {}
        else:
            a = {}

        free = Decimal(str(a.get("available") or a.get("availableBalance") or "0"))
        total = Decimal(str(a.get("total") or a.get("totalBalance") or a.get("balance") or "0"))
        liab = Decimal(str(a.get("liability") or a.get("borrowed") or "0"))
        return free, total, liab

    free = D0
    total = D0
    liab = D0
    for a in self.margin_accounts():
        if not isinstance(a, dict):
            continue
        if str(a.get("currency")) != currency:
            continue
        free = Decimal(str(a.get("available") or a.get("availableBalance") or "0"))
        total = Decimal(str(a.get("total") or a.get("totalBalance") or a.get("balance") or "0"))
        liab = Decimal(str(a.get("liability") or a.get("borrowed") or "0"))
        break
    return free, total, liab


# ----------------------------
# Spot order helpers (with candidate retry for tradeType errors)
# ----------------------------
def _kc_place_limit(self, symbol: str, side: str, price: str, size: str, client_oid: str, post_only: bool) -> str:
    payload = {"clientOid": client_oid, "side": side, "symbol": symbol, "type": "limit",
               "price": price, "size": size}
    if post_only:
        payload["postOnly"] = True
    j = self._request("POST", "/api/v1/orders", data=payload)
    return j["data"]["orderId"]


def _kc_place_market(self, symbol: str, side: str, size: str, client_oid: str) -> str:
    payload = {"clientOid": client_oid, "side": side, "symbol": symbol, "type": "market", "size": size}
    j = self._request("POST", "/api/v1/orders", data=payload)
    return j["data"]["orderId"]


def _kc_cancel(self, order_id: str):
    self._request("DELETE", f"/api/v1/orders/{order_id}")


def _kc_get_order(self, order_id: str) -> dict:
    j = self._request("GET", f"/api/v1/orders/{order_id}")
    return j.get("data", {}) or {}


def _kc_list_open_orders(self, symbol: str) -> List[dict]:
    j = self._request("GET", "/api/v1/orders", params={"status": "active", "symbol": symbol})
    return (j.get("data", {}) or {}).get("items", []) or []


def _kc_list_fills(self, symbol: str, side: Optional[str] = None,
                   page_size: int = 50, order_id: Optional[str] = None) -> List[dict]:
    params: Dict[str, Any] = {"symbol": symbol, "limit": page_size}
    if side:
        params["side"] = side
    if order_id:
        params["orderId"] = order_id

    if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
        last_exc: Optional[Exception] = None
        for _tt in _kc_margin_trade_type_candidates(self):
            try:
                params["tradeType"] = _tt
                j = self._request("GET", "/api/v3/hf/margin/fills", params=params)
                self._margin_trade_type_working = _tt
                return (j.get("data", {}) or {}).get("items", []) or []
            except Exception as e:
                last_exc = e
                if not _kc_trade_type_param_error(e):
                    raise
        if last_exc is not None:
            raise last_exc
        return []
    else:
        j = self._request("GET", "/api/v1/hf/fills", params=params)
    return (j.get("data", {}) or {}).get("items", []) or []


# ----------------------------
# Margin HF order helpers (with candidate retry)
# ----------------------------
def _kc_place_margin_limit(self, symbol: str, side: str, price: str, size: str, client_oid: str,
                           post_only: bool, auto_borrow: bool = False, auto_repay: bool = False) -> str:
    payload = {"clientOid": client_oid, "side": side, "symbol": symbol, "type": "limit",
               "price": price, "size": size}
    if bool(getattr(CFG, "margin_isolated", False)):
        payload["isIsolated"] = True
    if post_only:
        payload["postOnly"] = True
    if auto_borrow:
        payload["autoBorrow"] = True
    if auto_repay:
        payload["autoRepay"] = True
    j = self._request("POST", "/api/v3/hf/margin/order", data=payload)
    return j["data"]["orderId"]


def _kc_place_margin_market(self, symbol: str, side: str, size: str, client_oid: str,
                            auto_borrow: bool = False, auto_repay: bool = False) -> str:
    payload = {"clientOid": client_oid, "side": side, "symbol": symbol, "type": "market", "size": size}
    if bool(getattr(CFG, "margin_isolated", False)):
        payload["isIsolated"] = True
    if auto_borrow:
        payload["autoBorrow"] = True
    if auto_repay:
        payload["autoRepay"] = True
    j = self._request("POST", "/api/v3/hf/margin/order", data=payload)
    return j["data"]["orderId"]


def _kc_cancel_margin(self, symbol: str, order_id: str):
    self._request("DELETE", f"/api/v3/hf/margin/orders/{order_id}", params={"symbol": symbol})


def _kc_get_margin_order(self, symbol: str, order_id: str) -> dict:
    j = self._request("GET", f"/api/v3/hf/margin/orders/{order_id}", params={"symbol": symbol})
    return j.get("data", {}) or {}


def _kc_list_open_margin_orders(self, symbol: str) -> List[dict]:
    last_exc: Optional[Exception] = None
    for _tt in _kc_margin_trade_type_candidates(self):
        try:
            j = self._request("GET", "/api/v3/hf/margin/orders/active",
                               params={"symbol": symbol, "tradeType": _tt})
            self._margin_trade_type_working = _tt
            data = j.get("data", []) or []
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                items = data.get("items") or data.get("list") or data.get("orders") or []
                if isinstance(items, list):
                    return items
            return []
        except Exception as e:
            last_exc = e
            if not _kc_trade_type_param_error(e):
                raise
    if last_exc is not None:
        raise last_exc
    return []


def _kc_list_open_margin_order_symbols(self) -> List[str]:
    last_exc: Optional[Exception] = None
    for _tt in _kc_margin_trade_type_candidates(self):
        try:
            j = self._request("GET", "/api/v3/hf/margin/order/active/symbols",
                               params={"tradeType": _tt})
            self._margin_trade_type_working = _tt
            data = j.get("data", []) or []
            out: List[str] = []
            if isinstance(data, list):
                for it in data:
                    if isinstance(it, str):
                        out.append(it)
                    elif isinstance(it, dict):
                        sym = it.get("symbol") or it.get("symbolName")
                        if sym:
                            out.append(str(sym))
            elif isinstance(data, dict):
                items = data.get("items") or data.get("list") or data.get("symbols") or []
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, str):
                            out.append(it)
                        elif isinstance(it, dict):
                            sym = it.get("symbol") or it.get("symbolName")
                            if sym:
                                out.append(str(sym))
            return out
        except Exception as e:
            last_exc = e
            if not _kc_trade_type_param_error(e):
                raise
    if last_exc is not None:
        raise last_exc
    return []


# ----------------------------
# Unified wrapper helpers
# ----------------------------
def _kc_place_limit_any(self, symbol: str, side: str, price: str, size: str, client_oid: str,
                        post_only: bool, auto_borrow: bool = False, auto_repay: bool = False) -> str:
    if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
        return self.place_margin_limit(symbol, side, price, size, client_oid, post_only, auto_borrow, auto_repay)
    return self.place_limit(symbol, side, price, size, client_oid, post_only)


def _kc_place_market_any(self, symbol: str, side: str, size: str, client_oid: str,
                         auto_borrow: bool = False, auto_repay: bool = False) -> str:
    if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
        return self.place_margin_market(symbol, side, size, client_oid, auto_borrow, auto_repay)
    return self.place_market(symbol, side, size, client_oid)


def _kc_cancel_any(self, symbol: str, order_id: str):
    if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
        try:
            return self.cancel_margin(symbol, order_id)
        except Exception as e:
            if "Only Support margin trade order" in str(e):
                return self.cancel(order_id)
            raise
    return self.cancel(order_id)


def _kc_get_order_any(self, symbol: str, order_id: str) -> dict:
    if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
        try:
            return self.get_margin_order(symbol, order_id)
        except Exception as e:
            if "Only Support margin trade order" in str(e):
                return self.get_order(order_id)
            raise
    return self.get_order(order_id)


def _kc_list_open_orders_any(self, symbol: str) -> List[dict]:
    if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
        return self.list_open_margin_orders(symbol)
    return self.list_open_orders(symbol)


def _kc_list_open_margin_order_symbols_any(self) -> List[str]:
    if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
        return self.list_open_margin_order_symbols()
    return []


# ----------------------------
# Bind missing methods only (fail-safe, non-destructive).
#
# [DAILY AUDIT FIX] force-bind repaired fill/open-order helpers even when
# stale class definitions already exist (list_fills, list_open_margin_orders,
# list_open_orders_any). This is intentional — it ensures the patched versions
# with tradeType candidate retry are always active at runtime.
# ----------------------------
_bindings = {
    "margin_accounts":                  _kc_margin_accounts,
    "isolated_accounts":                _kc_isolated_accounts,
    "accounts_any":                     _kc_accounts_any,
    "place_limit":                      _kc_place_limit,
    "place_market":                     _kc_place_market,
    "cancel":                           _kc_cancel,
    "get_order":                        _kc_get_order,
    "list_open_orders":                 _kc_list_open_orders,
    "list_fills":                       _kc_list_fills,
    "place_margin_limit":               _kc_place_margin_limit,
    "place_margin_market":              _kc_place_margin_market,
    "cancel_margin":                    _kc_cancel_margin,
    "get_margin_order":                 _kc_get_margin_order,
    "list_open_margin_orders":          _kc_list_open_margin_orders,
    "list_open_margin_order_symbols":   _kc_list_open_margin_order_symbols,
    "place_limit_any":                  _kc_place_limit_any,
    "place_market_any":                 _kc_place_market_any,
    "cancel_any":                       _kc_cancel_any,
    "get_order_any":                    _kc_get_order_any,
    "list_open_orders_any":             _kc_list_open_orders_any,
    "list_open_margin_order_symbols_any": _kc_list_open_margin_order_symbols_any,
}

# Force-bind the methods that MUST use the patched (tradeType-retry) versions.
_force_bind = {"list_fills", "list_open_margin_orders", "list_open_orders_any"}

for _name, _fn in _bindings.items():
    if (not hasattr(KuCoinClient, _name)) or _name in _force_bind:
        setattr(KuCoinClient, _name, _fn)
