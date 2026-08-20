# -*- coding: utf-8 -*-
"""RQAlpha 股息税：派息日到账时预扣（与原生 backtest / A 股券商一致）。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from dividend_lowvol_rotation.config import DIVIDEND_TAX_ENABLED
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date, build_dividend_index
from dividend_lowvol_rotation.rqalpha.native_cash_ledger import _lots_from_shares


def init_dividend_cash_index(records) -> dict:
    if records is None:
        return {}
    try:
        empty = records.empty
    except AttributeError:
        empty = not records
    if empty:
        return {}
    return build_dividend_index(records)


def pay_dividend_tax_on_date(
    context,
    *,
    positions,
    buy_dates: dict[str, pd.Timestamp],
    code_from_obid,
    as_of: pd.Timestamp | None = None,
    share_override: dict[str, int] | None = None,
) -> float:
    """在派息日扣除红利税（引擎已入账税前分红后调用）。"""
    if not DIVIDEND_TAX_ENABLED:
        return 0.0
    div_index = getattr(context, "dlv_div_cash_index", None) or {}
    if not div_index:
        return 0.0

    today = pd.Timestamp(as_of or context.now).normalize()
    share_map = {
        str(code): int(shares)
        for code, shares in (share_override or {}).items()
        if shares and int(shares) > 0
    }
    if not share_map:
        return 0.0

    lots = _lots_from_shares(share_map, buy_dates, today)

    tax, gross, _rows = accrue_dividend_cash_on_date(
        lots,
        div_index,
        today,
        dividend_cash=True,
        apply_tax=True,
        use_payable_date=True,
    )
    if tax <= 0 or gross <= 0:
        return 0.0

    from rqalpha.const import TAX_TYPE
    from rqalpha.core.events import EVENT, Event
    from rqalpha.environment import Environment

    Environment.get_instance().event_bus.publish_event(
        Event(
            EVENT.PAY_TAXES,
            order_book_id=None,
            delta_amount=float(tax),
            trading_dt=Environment.get_instance().trading_dt,
            tax_type=TAX_TYPE.DIVIDEND,
        )
    )
    return float(tax)
