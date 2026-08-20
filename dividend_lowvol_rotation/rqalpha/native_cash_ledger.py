# -*- coding: utf-8 -*-
"""原生口径现金台账：与 backtest._credit_dividends_on_date 一致，供调仓模拟使用。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from dividend_lowvol_rotation.corporate_actions import apply_splits_to_holdings
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date


def _round_cash(cash: float) -> float:
    """仅用于 NAV 输出展示，与 backtest nav_rows 的 round(cash, 2) 一致。"""
    return round(float(cash), 2)


def init_native_cash(context, initial_capital: float) -> None:
    context.dlv_native_cash = float(initial_capital)
    context.dlv_native_cash_date = None


def get_native_cash(context) -> float:
    return float(getattr(context, "dlv_native_cash", 0.0) or 0.0)


def set_native_cash(context, cash: float, *, as_of: pd.Timestamp | None = None) -> None:
    context.dlv_native_cash = float(cash)
    if as_of is not None:
        context.dlv_native_cash_date = pd.Timestamp(as_of).normalize()


def _lots_from_shares(
    share_map: dict[str, int],
    buy_dates: dict,
    as_of: pd.Timestamp,
) -> dict[str, SimpleNamespace]:
    """由成交台账股数构建 lots（与 backtest.lots 同口径，不依赖 RQ 持仓）。"""
    lots: dict[str, SimpleNamespace] = {}
    today = pd.Timestamp(as_of).normalize()
    for code, shares in share_map.items():
        qty = int(shares)
        if qty <= 0:
            continue
        code = str(code)
        buy = buy_dates.get(code, today)
        lots[code] = SimpleNamespace(
            code=code,
            shares=qty,
            buy_date=pd.Timestamp(buy).normalize(),
            name="",
        )
    return lots


def _lots_from_positions(
    positions,
    buy_dates: dict,
    code_from_obid,
    as_of: pd.Timestamp,
    *,
    share_override: dict[str, int] | None = None,
):
    lots: dict[str, SimpleNamespace] = {}
    override = share_override or {}
    for pos in positions:
        code = code_from_obid(pos.order_book_id)
        qty = int(override.get(code, getattr(pos, "quantity", 0) or 0))
        if qty <= 0:
            continue
        buy = buy_dates.get(code, as_of)
        lots[code] = SimpleNamespace(
            code=code,
            shares=qty,
            buy_date=pd.Timestamp(buy).normalize(),
            name="",
        )
    return lots


def debit_native_cash(context, amount: float) -> None:
    if amount <= 0:
        return
    context.dlv_native_cash = get_native_cash(context) - float(amount)


def credit_payable_dividend(
    context,
    *,
    positions,
    buy_dates: dict,
    code_from_obid,
    as_of: pd.Timestamp,
    share_override: dict[str, int] | None = None,
) -> float:
    """按送股后股数全额入账派息现金（与 backtest._credit_dividends_on_date 一致）。"""
    div_index = getattr(context, "dlv_div_cash_index", None) or {}
    if not div_index:
        return 0.0

    today = pd.Timestamp(as_of).normalize()
    if getattr(context, "dlv_native_cash_date", None) == today:
        return 0.0

    share_map = {
        str(code): int(shares)
        for code, shares in (share_override or {}).items()
        if shares and int(shares) > 0
    }
    if not share_map:
        context.dlv_native_cash_date = today
        return 0.0

    lots_adj = _lots_from_shares(share_map, buy_dates, today)

    _, gross_adj, rows_adj = accrue_dividend_cash_on_date(
        lots_adj,
        div_index,
        today,
        dividend_cash=True,
        apply_tax=False,
        use_payable_date=True,
    )
    if not rows_adj:
        context.dlv_native_cash_date = today
        return 0.0

    gross = float(gross_adj)
    if gross > 0:
        context.dlv_native_cash = get_native_cash(context) + gross
    context.dlv_native_cash_date = today
    return gross


def rebalance_cash(context) -> float:
    """调仓可用现金：与 backtest.cash 同口径（全精度 float）。"""
    return get_native_cash(context)


def init_rebalance_anchor(context, cash: float, as_of: pd.Timestamp) -> None:
    context.dlv_rb_anchor_date = pd.Timestamp(as_of).normalize()
    context.dlv_rb_anchor_cash = float(cash)
    context.dlv_rb_anchor_shares = {}
    context.dlv_rb_anchor_buy_dates = {}
    _set_anchor_cal_idx(context, context.dlv_rb_anchor_date)


def _set_anchor_cal_idx(context, anchor: pd.Timestamp) -> None:
    cal = getattr(getattr(context, "dlv_bt_ctx", None), "calendar", None) or []
    anchor = pd.Timestamp(anchor).normalize()
    try:
        context.dlv_rb_anchor_cal_idx = next(
            i for i, d in enumerate(cal) if pd.Timestamp(d).normalize() == anchor
        )
    except StopIteration:
        context.dlv_rb_anchor_cal_idx = 0


def save_rebalance_anchor(
    context,
    cash: float,
    lots: dict,
    *,
    as_of: pd.Timestamp,
) -> None:
    """调仓后保存锚点：下次 refresh 仅重放锚点次日至今。"""
    context.dlv_rb_anchor_date = pd.Timestamp(as_of).normalize()
    context.dlv_rb_anchor_cash = float(cash)
    context.dlv_rb_anchor_shares = {
        str(code): int(getattr(lot, "shares", 0) or 0)
        for code, lot in (lots or {}).items()
        if int(getattr(lot, "shares", 0) or 0) > 0
    }
    context.dlv_rb_anchor_buy_dates = {
        str(code): pd.Timestamp(getattr(lot, "buy_date", as_of)).normalize()
        for code, lot in (lots or {}).items()
        if int(getattr(lot, "shares", 0) or 0) > 0
    }
    _set_anchor_cal_idx(context, context.dlv_rb_anchor_date)


def roll_rebalance_anchor(
    context,
    as_of: pd.Timestamp,
    *,
    cash: float | None = None,
    lots: dict | None = None,
) -> None:
    """日终滚动锚点（调仓日用 sim_lots，其余日用台账）。"""
    if lots:
        save_rebalance_anchor(context, cash if cash is not None else get_native_cash(context), lots, as_of=as_of)
        return
    shares = {
        str(c): int(s)
        for c, s in (_trade_share_base_from_context(context)).items()
        if int(s) > 0
    }
    context.dlv_rb_anchor_date = pd.Timestamp(as_of).normalize()
    context.dlv_rb_anchor_cash = float(cash if cash is not None else get_native_cash(context))
    context.dlv_rb_anchor_shares = shares
    context.dlv_rb_anchor_buy_dates = {
        str(k): pd.Timestamp(v).normalize()
        for k, v in (getattr(context, "dlv_buy_dates", None) or {}).items()
        if str(k) in shares
    }
    _set_anchor_cal_idx(context, context.dlv_rb_anchor_date)


def _trade_share_base_from_context(context) -> dict[str, int]:
    return {
        str(code): int(shares)
        for code, shares in (getattr(context, "dlv_trade_shares", None) or {}).items()
        if shares and int(shares) > 0
    }


def refresh_cash_to_rebalance(context, as_of: pd.Timestamp) -> float:
    """调仓日开盘：自上次锚点重放派息/送股/扣税，与 backtest 区间累计一致。"""
    anchor_date = getattr(context, "dlv_rb_anchor_date", None)
    anchor_cash = getattr(context, "dlv_rb_anchor_cash", None)
    if anchor_date is None or anchor_cash is None:
        return rebalance_cash(context)

    shares = {
        str(c): int(s)
        for c, s in (getattr(context, "dlv_rb_anchor_shares", None) or {}).items()
        if int(s) > 0
    }
    buy_dates = {
        str(c): pd.Timestamp(d).normalize()
        for c, d in (getattr(context, "dlv_rb_anchor_buy_dates", None) or {}).items()
    }
    div_index = getattr(context, "dlv_div_cash_index", None) or {}
    split_index = getattr(context, "dlv_split_index", None) or {}
    cal = getattr(getattr(context, "dlv_bt_ctx", None), "calendar", None) or []

    cash = float(anchor_cash)
    end = pd.Timestamp(as_of).normalize()
    start = pd.Timestamp(anchor_date).normalize()
    start_idx = int(getattr(context, "dlv_rb_anchor_cal_idx", 0) or 0)
    if start_idx < len(cal) and pd.Timestamp(cal[start_idx]).normalize() != start:
        start_idx = next(
            (i for i, d in enumerate(cal) if pd.Timestamp(d).normalize() == start),
            0,
        )

    for day in cal[start_idx + 1 :]:
        day = pd.Timestamp(day).normalize()
        if day > end:
            break
        if shares:
            pre_lots = _lots_from_shares(shares, buy_dates, day)
            _, gross, _ = accrue_dividend_cash_on_date(
                pre_lots,
                div_index,
                day,
                dividend_cash=True,
                apply_tax=False,
                use_payable_date=True,
            )
            if gross:
                cash += float(gross)
            apply_splits_to_holdings(shares, split_index, day)
            post_lots = _lots_from_shares(shares, buy_dates, day)
            tax, _, _ = accrue_dividend_cash_on_date(
                post_lots,
                div_index,
                day,
                dividend_cash=True,
                apply_tax=True,
                use_payable_date=True,
            )
            if tax:
                cash -= float(tax)

    context.dlv_native_cash = cash
    context.dlv_native_cash_date = end
    context.dlv_trade_shares = dict(shares)
    if shares:
        context.dlv_buy_dates = {
            str(k): pd.Timestamp(v).normalize()
            for k, v in buy_dates.items()
            if str(k) in shares
        }
    return float(cash)
