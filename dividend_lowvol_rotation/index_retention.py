# -*- coding: utf-8 -*-
"""H30269 风格持仓保留规则：不达标才调出（非排名缓冲带）。"""

from __future__ import annotations

import pandas as pd

from dividend_lowvol_rotation.config import (
    INDEX_RETENTION_MIN_DIVIDEND_YIELD_PCT,
    RECENT_DIVIDEND_MAX_YEARS,
    RISK_FILTER_ENABLED,
)
from dividend_lowvol_rotation.dividend import build_dividend_panel
from dividend_lowvol_rotation.risk_screening import recent_dividend_mask, risk_filter_mask
from dividend_lowvol_rotation.scoring import dynamic_dividend_yield_pct


def index_retention_fail_reason(
    row: pd.Series | dict | None,
    *,
    min_yield_pct: float = INDEX_RETENTION_MIN_DIVIDEND_YIELD_PCT,
    as_of: pd.Timestamp | None = None,
) -> str | None:
    if row is None:
        return "无行情数据"

    data = row.to_dict() if isinstance(row, pd.Series) else dict(row)
    price = data.get("price")
    if price is None or float(price) <= 0:
        return "无有效价格"

    yld = data.get("dividend_yield_pct")
    if yld is None and data.get("cash_per_share") and price:
        yld = dynamic_dividend_yield_pct(float(data["cash_per_share"]), float(price))
    if yld is None or float(yld) < min_yield_pct:
        return f"股息率<{min_yield_pct:.1f}%"

    if not recent_dividend_mask(pd.DataFrame([data]), as_of=as_of).iloc[0]:
        return f"近{RECENT_DIVIDEND_MAX_YEARS}年无分红"

    if RISK_FILTER_ENABLED:
        one = pd.DataFrame([data])
        risk_ok, _ = risk_filter_mask(one)
        if not risk_ok.iloc[0]:
            return "排雷不达标"

    return None


def should_sell_index_rules(
    code: str,
    panel: pd.DataFrame,
) -> tuple[bool, str]:
    if panel.empty:
        return True, "指数规则:候选池为空"
    sub = panel[panel["code"].astype(str) == str(code)]
    row = sub.iloc[0] if not sub.empty else None
    reason = index_retention_fail_reason(row)
    if reason:
        return True, f"指数规则:{reason}"
    return False, ""


def enrich_panel_with_holdings(
    panel: pd.DataFrame,
    lots: dict,
    *,
    store,
    records: pd.DataFrame,
    as_of: pd.Timestamp,
    risk_hist: pd.DataFrame | None = None,
    div_index=None,
) -> pd.DataFrame:
    if not lots:
        return panel

    from dividend_lowvol_rotation.risk_screening import attach_risk_from_records, merge_risk_history

    held = set(panel["code"].astype(str)) if not panel.empty and "code" in panel.columns else set()
    missing = [c for c in lots if str(c) not in held]
    if not missing and not panel.empty:
        return panel

    div = build_dividend_panel(records=records, as_of=as_of)
    div_idx = div.drop_duplicates(subset=["code"], keep="first").set_index("code") if not div.empty else None
    extra_rows: list[dict] = []
    for code in missing:
        m = store.metrics_at(code, as_of)
        if m.get("price") is None:
            continue
        base: dict = {"code": str(code), "name": getattr(lots[code], "name", "")}
        if div_idx is not None and code in div_idx.index:
            base.update(div_idx.loc[code].to_dict())
        base.update(m)
        base["dividend_yield_pct"] = dynamic_dividend_yield_pct(
            base.get("cash_per_share"), base.get("price")
        )
        extra_rows.append(base)

    if not extra_rows:
        return panel
    extra = pd.DataFrame(extra_rows)
    extra = attach_risk_from_records(extra, records, as_of, div_index=div_index)
    if risk_hist is not None and not risk_hist.empty:
        extra = merge_risk_history(extra, risk_hist, as_of)
    if panel.empty:
        return extra
    return pd.concat([panel, extra], ignore_index=True)
