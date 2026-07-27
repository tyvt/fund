"""中证 A500 数据拉取与估值序列构建。"""

from cn_broad_data import (
    attach_cn_broad_percentiles,
    build_cn_broad_valuation_history,
    fetch_cn_broad_snapshot,
    read_cn_broad_indicator as read_a500_indicator,
)
from config import (
    A500_INDEX,
    A500_PERCENTILE_MIN_DAYS,
    A500_PERCENTILE_WINDOW,
)

A500_CODE = A500_INDEX["code"]
A500_NAME = A500_INDEX["name"]


def build_a500_valuation_history(start_date="20150101", end_date=None, bond_history=None):
    return build_cn_broad_valuation_history(
        A500_CODE, start_date=start_date, end_date=end_date, bond_history=bond_history
    )


def attach_percentiles(
    panel, window=A500_PERCENTILE_WINDOW, min_days=A500_PERCENTILE_MIN_DAYS
):
    return attach_cn_broad_percentiles(
        panel, A500_CODE, window=window, min_days=min_days
    )


def calibrate_dividend_ratio(indicator):
    from cn_broad_data import calibrate_dividend_ratio as _calibrate

    return _calibrate(indicator)


def get_a500_latest():
    """获取最新 PE、股息率与数据日期（与估值面板末行一致）。"""
    panel = build_a500_valuation_history()
    if panel is None or panel.empty:
        return None
    latest = panel.iloc[-1]
    return {
        "pe": float(latest["pe"]),
        "dividend_yield": float(latest["dividend_yield"]),
        "date": latest["date"],
    }


def fetch_a500_snapshot(bond_history=None):
    return fetch_cn_broad_snapshot(A500_CODE, bond_history=bond_history)
