# -*- coding: utf-8 -*-
"""RQAlpha 交易成本与 config.py（DLV_*）对齐。

成交价：
- ``DLV_EXECUTION_AT_CLOSE=true``（默认）：模拟与 RQ 引擎均按 bar 收盘价，无滑点。
- ``DLV_EXECUTION_AT_CLOSE=false``：仅在 ``trade_execution_price`` 中按 ``DLV_SLIPPAGE_RATE`` 调整；
  RQ 引擎层滑点恒为 0，避免重复计入。

佣金由 RQ ``sys_transaction_cost`` 在成交时扣除；卖侧印花税与 RQ 一致（``settle_sell`` / ``DLV_BACKTEST_PRICE_SOURCE=rqalpha``）。
"""

from __future__ import annotations

import pandas as pd

from dividend_lowvol_rotation.config import (
    COMMISSION_RATE,
    EXECUTION_AT_CLOSE,
    MIN_COMMISSION_CNY,
    SLIPPAGE_RATE,
    execution_slippage_enabled,
)
from dividend_lowvol_rotation.costs import stamp_tax_rate, uses_live_settlement

# RQAlpha 内置股票佣金基准（万八）
_RQALPHA_BASE_COMMISSION_RATE = 0.0008


def rqalpha_commission_multiplier() -> float:
    """将 RQAlpha 默认万八佣金缩放为 ``DLV_COMMISSION_RATE``。"""
    return COMMISSION_RATE / _RQALPHA_BASE_COMMISSION_RATE


def rqalpha_engine_slippage_rate() -> float:
    """引擎层滑点恒为 0；滑点（若有）只在 ``trade_execution_price`` 中处理。"""
    return 0.0


def sim_includes_slippage() -> bool:
    """整手模拟是否在成交价中计入滑点。"""
    return execution_slippage_enabled()


def execution_cost_summary() -> str:
    stamp = ""
    if uses_live_settlement():
        rate = stamp_tax_rate(pd.Timestamp.today()) * 100
        stamp = f"，卖侧印花税 {rate:.2f}%"
    if EXECUTION_AT_CLOSE:
        return (
            f"佣金 {COMMISSION_RATE * 10000:.4f}‱（最低 {MIN_COMMISSION_CNY:.0f} 元）"
            f"{stamp}，成交价：收盘价（无滑点）"
        )
    return (
        f"佣金 {COMMISSION_RATE * 10000:.4f}‱（最低 {MIN_COMMISSION_CNY:.0f} 元），"
        f"滑点 {SLIPPAGE_RATE * 100:.2f}%（模拟计入，引擎层 0%）"
    )
