# -*- coding: utf-8 -*-
"""用 RQAlpha 行情价覆盖 KlineStore，使模拟与成交同源。"""

from __future__ import annotations


class BarPriceStore:
    """在调仓日将 store 行情替换为 RQAlpha bar.close。"""

    def __init__(self, base_store, price_map: dict[str, float]):
        self._base = base_store
        self._prices = {str(k): float(v) for k, v in price_map.items() if v and v > 0}

    def price_at(self, code: str, as_of) -> float | None:
        key = str(code)
        if key in self._prices:
            return self._prices[key]
        return self._base.price_at(code, as_of)

    def metrics_at(self, code: str, as_of) -> dict:
        metrics = dict(self._base.metrics_at(code, as_of) or {})
        key = str(code)
        if key in self._prices:
            metrics["price"] = self._prices[key]
        return metrics

    def ensure(self, codes):
        return self._base.ensure(codes)

    def __getattr__(self, name):
        return getattr(self._base, name)


def price_map_from_bar_dict(bar_dict, codes: list[str], to_rqalpha_id) -> dict[str, float]:
    out: dict[str, float] = {}
    for code in codes:
        obid = to_rqalpha_id(code)
        if not obid or obid not in bar_dict:
            continue
        px = float(bar_dict[obid].close)
        if px > 0:
            out[str(code)] = px
    return out


def build_rebalance_price_map(
    bar_dict,
    codes: list[str],
    store,
    as_of,
    to_rqalpha_id,
) -> dict[str, float]:
    """调仓价：优先 bar.close，缺失时回退 store（避免漏单）。"""
    out = price_map_from_bar_dict(bar_dict, codes, to_rqalpha_id)
    for code in codes:
        key = str(code)
        if key in out:
            continue
        px = store.price_at(key, as_of) if store is not None else None
        if px and px > 0:
            out[key] = float(px)
    return out
