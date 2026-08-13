# -*- coding: utf-8 -*-
"""调仓日程：指数年度 / 自然月 / 季报披露截止日 / 固定步长。"""

from __future__ import annotations

import pandas as pd

# H30269：每年 12 月第二个星期五的下一交易日
CSI_INDEX_ANNUAL_MONTH = 12

# 证监会定期报告披露截止日（月, 日）
QUARTERLY_REPORT_DEADLINES: tuple[tuple[int, int], ...] = (
    (4, 30),
    (8, 31),
    (10, 31),
)


def first_trading_day_after(
    calendar: list[pd.Timestamp],
    deadline: pd.Timestamp,
) -> pd.Timestamp | None:
    """严格晚于截止日（日历日）的首个交易日。"""
    for d in calendar:
        if d > deadline:
            return d
    return None


def quarterly_report_rebalance_dates(calendar: list[pd.Timestamp]) -> list[pd.Timestamp]:
    """季报截止后首个交易日调仓；含回测起点建仓与期末净值。"""
    if not calendar:
        return []
    cal = sorted(calendar)
    start, end = cal[0], cal[-1]
    out: list[pd.Timestamp] = [start]

    for year in range(start.year - 1, end.year + 2):
        for month, day in QUARTERLY_REPORT_DEADLINES:
            deadline = pd.Timestamp(year=year, month=month, day=day)
            td = first_trading_day_after(cal, deadline)
            if td is not None and start <= td <= end:
                out.append(td)

    out = sorted(set(out))
    if out[-1] != end:
        out.append(end)
    return out


def monthly_rebalance_dates(calendar: list[pd.Timestamp]) -> list[pd.Timestamp]:
    """每个自然月首个交易日调仓；含回测起点建仓与期末净值。"""
    if not calendar:
        return []
    cal = sorted(calendar)
    start, end = cal[0], cal[-1]
    out: list[pd.Timestamp] = [start]

    month_first: dict[tuple[int, int], pd.Timestamp] = {}
    for d in cal:
        key = (d.year, d.month)
        if key not in month_first:
            month_first[key] = d

    start_key = (start.year, start.month)
    for key in sorted(month_first.keys()):
        if key <= start_key:
            continue
        td = month_first[key]
        if start <= td <= end:
            out.append(td)

    out = sorted(set(out))
    if out[-1] != end:
        out.append(end)
    return out


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> pd.Timestamp:
    """month 内第 n 个 weekday（Monday=0 … Friday=4）。"""
    first = pd.Timestamp(year=year, month=month, day=1)
    offset = (weekday - first.weekday()) % 7
    candidate = first + pd.Timedelta(days=offset)
    if candidate.month != month:
        candidate += pd.Timedelta(days=7)
    return candidate + pd.Timedelta(days=7 * (n - 1))


def index_annual_december_rebalance_dates(calendar: list[pd.Timestamp]) -> list[pd.Timestamp]:
    """H30269 官方调样日：12 月第二个星期五的下一交易日。"""
    if not calendar:
        return []
    cal = sorted(calendar)
    start, end = cal[0], cal[-1]
    out: list[pd.Timestamp] = [start]

    for year in range(start.year, end.year + 2):
        second_friday = _nth_weekday_of_month(year, CSI_INDEX_ANNUAL_MONTH, 4, 2)
        td = first_trading_day_after(cal, second_friday)
        if td is not None and start < td <= end:
            out.append(td)

    out = sorted(set(out))
    if out[-1] != end:
        out.append(end)
    return out


def index_annual_january_rebalance_dates(
    calendar: list[pd.Timestamp],
    *,
    january_day: int = 15,
) -> list[pd.Timestamp]:
    """1 月中旬后首个交易日调仓，避开 12 月除权密集期。"""
    if not calendar:
        return []
    cal = sorted(calendar)
    start, end = cal[0], cal[-1]
    out: list[pd.Timestamp] = [start]

    for year in range(start.year, end.year + 2):
        deadline = pd.Timestamp(year=year, month=1, day=january_day)
        td = first_trading_day_after(cal, deadline)
        if td is not None and start < td <= end:
            out.append(td)

    out = sorted(set(out))
    if out[-1] != end:
        out.append(end)
    return out


def index_annual_rebalance_dates(calendar: list[pd.Timestamp]) -> list[pd.Timestamp]:
    from dividend_lowvol_rotation.config import (
        INDEX_ANNUAL_REBALANCE_TIMING,
        INDEX_JANUARY_REBALANCE_DAY,
    )

    if INDEX_ANNUAL_REBALANCE_TIMING == "january":
        return index_annual_january_rebalance_dates(
            calendar, january_day=INDEX_JANUARY_REBALANCE_DAY
        )
    return index_annual_december_rebalance_dates(calendar)


def fixed_step_rebalance_dates(calendar: list[pd.Timestamp], step: int) -> list[pd.Timestamp]:
    if not calendar or step <= 0:
        return []
    out: list[pd.Timestamp] = []
    i = 0
    while i < len(calendar):
        out.append(calendar[i])
        i += step
    if out[-1] != calendar[-1]:
        out.append(calendar[-1])
    return out


def resolve_rebalance_dates(
    calendar: list[pd.Timestamp],
    *,
    mode: str = "monthly",
    rebalance_days: int = 30,
) -> list[pd.Timestamp]:
    if mode == "monthly":
        return monthly_rebalance_dates(calendar)
    if mode == "index_annual":
        return index_annual_rebalance_dates(calendar)
    if mode == "quarterly_report":
        return quarterly_report_rebalance_dates(calendar)
    return fixed_step_rebalance_dates(calendar, rebalance_days)
