"""按当日指数涨跌比例缩放买入金额：跌越多买越多，反弹则少买。"""

from __future__ import annotations

from typing import Callable

import pandas as pd


def _safe_float(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def row_daily_change_pct(row) -> float | None:
    """当日相对昨收的涨跌幅（负数=下跌）。"""
    from sell_trailing import row_field

    delta = _safe_float(row_field(row, "live_price_delta_pct"))
    if delta is not None:
        return delta
    delta = _safe_float(row_field(row, "daily_change_pct"))
    if delta is not None:
        return delta
    close = _safe_float(row_field(row, "close"))
    prev = _safe_float(row_field(row, "close_prev"))
    if close is None or prev is None or prev <= 0:
        return None
    return close / prev - 1.0


def change_multiplier(
    daily_change_pct: float | None,
    *,
    sensitivity: float | None = None,
    min_mult: float | None = None,
    max_mult: float | None = None,
) -> float:
    """
    涨跌 → 金额系数。
    amount = base * clamp(1 - sensitivity * daily_change, min, max)
    例：sensitivity=10 时，跌 3% → 1.30×，涨 3% → 0.70×。
    """
    from config import (
        BUY_AMOUNT_CHANGE_MAX_MULT,
        BUY_AMOUNT_CHANGE_MIN_MULT,
        BUY_AMOUNT_CHANGE_SENSITIVITY,
    )

    sens = BUY_AMOUNT_CHANGE_SENSITIVITY if sensitivity is None else sensitivity
    lo = BUY_AMOUNT_CHANGE_MIN_MULT if min_mult is None else min_mult
    hi = BUY_AMOUNT_CHANGE_MAX_MULT if max_mult is None else max_mult
    if daily_change_pct is None or pd.isna(daily_change_pct):
        return 1.0
    raw = 1.0 - float(sens) * float(daily_change_pct)
    return max(float(lo), min(float(hi), raw))


def resolve_change_scaled_amount(
    base_amount: float,
    row,
    *,
    min_amount: float = 10.0,
) -> float:
    """基准金额 × 涨跌系数。"""
    if base_amount <= 0:
        return 0.0
    mult = change_multiplier(row_daily_change_pct(row))
    return max(min_amount, round(base_amount * mult))


def attach_daily_change_pct(panel, close_col: str = "close") -> pd.DataFrame:
    """为面板附加 daily_change_pct（相对前一交易日收盘）。"""
    if panel is None or panel.empty or close_col not in panel.columns:
        return panel
    out = panel.copy()
    closes = pd.to_numeric(out[close_col], errors="coerce")
    out["daily_change_pct"] = closes.pct_change()
    return out


def make_change_amount_fn(
    base_amount: float,
    panel=None,
    *,
    date_col: str = "date",
    close_col: str = "close",
    scale: float = 1.0,
) -> Callable:
    """生成回测用 amount_fn(row)；优先用面板预计算的日涨跌。"""
    effective_base = base_amount * scale
    change_by_date: dict[str, float] = {}
    if panel is not None and not panel.empty and close_col in panel.columns:
        work = panel.copy()
        if date_col in work.columns:
            work = work.sort_values(date_col)
        closes = pd.to_numeric(work[close_col], errors="coerce")
        changes = closes.pct_change()
        if date_col in work.columns:
            for dt, chg in zip(work[date_col], changes):
                if pd.isna(chg):
                    continue
                key = pd.Timestamp(dt).strftime("%Y-%m-%d")
                change_by_date[key] = float(chg)

    def _fn(row):
        from sell_trailing import row_field

        chg = row_daily_change_pct(row)
        if chg is None and change_by_date:
            raw = row_field(row, date_col) or row_field(row, "date_only")
            if raw is not None:
                key = pd.Timestamp(raw).strftime("%Y-%m-%d")
                chg = change_by_date.get(key)
        mult = change_multiplier(chg)
        return max(10.0, round(effective_base * mult))

    return _fn


def format_change_amount_line(snapshot, base: float) -> str:
    """实盘/报告：展示涨跌与对应买入金额。"""
    from price_position import format_index_price

    close = snapshot.get("close")
    prev = snapshot.get("close_prev")
    chg = row_daily_change_pct(snapshot)
    amt = resolve_change_scaled_amount(base, snapshot)
    mult = change_multiplier(chg)

    price_part = f"当前 {format_index_price(close)}"
    if prev is not None and chg is not None:
        price_part += (
            f"（昨收 {format_index_price(prev)}，{chg * 100:+.2f}%）"
        )
    return (
        f"{price_part} **{amt:.0f}元**"
        f"（基准 {base:.0f} × {mult:.2f}）"
    )
