"""回测风险收益指标：夏普比率、最大回撤、年化收益等。"""

import math

import pandas as pd

from config import (
    BACKTEST_RISK_FREE_RATE,
    BACKTEST_TRADING_DAYS_PER_YEAR,
)


def _daily_returns_with_cash_flows(nav: pd.Series, cash_flows: pd.Series) -> pd.Series:
    """剔除外部注资/提现后的日收益率（用于定投类策略）。"""
    if nav.empty or len(nav) < 2:
        return pd.Series(dtype=float)
    prev = nav.shift(1)
    cf = cash_flows.reindex(nav.index).fillna(0.0)
    valid = prev > 0
    returns = (nav - cf - prev) / prev
    return returns.loc[valid].dropna()


def sharpe_ratio(
    daily_returns: pd.Series,
    risk_free_rate: float | None = None,
    periods_per_year: int | None = None,
) -> float | None:
    """年化夏普比率；日收益率序列须已剔除现金流。"""
    if daily_returns is None or len(daily_returns) < 20:
        return None
    rf = BACKTEST_RISK_FREE_RATE if risk_free_rate is None else risk_free_rate
    periods = (
        BACKTEST_TRADING_DAYS_PER_YEAR
        if periods_per_year is None
        else periods_per_year
    )
    rf_daily = (1 + rf) ** (1 / periods) - 1
    excess = daily_returns - rf_daily
    std = excess.std()
    if std is None or std == 0 or math.isnan(std):
        return None
    return float(excess.mean() / std * math.sqrt(periods))


def max_drawdown_pct(nav: pd.Series) -> float | None:
    """最大回撤（%，负值表示回撤）。"""
    if nav is None or nav.empty:
        return None
    peak = nav.cummax()
    dd = (nav - peak) / peak
    if dd.empty:
        return None
    return float(dd.min() * 100)


def annualized_return_pct(
    total_return_pct: float | None,
    trading_days: int,
    periods_per_year: int | None = None,
) -> float | None:
    """由区间总收益率折算年化收益率（%）。"""
    if total_return_pct is None or trading_days <= 0:
        return None
    periods = (
        BACKTEST_TRADING_DAYS_PER_YEAR
        if periods_per_year is None
        else periods_per_year
    )
    total_mult = 1 + total_return_pct / 100
    if total_mult <= 0:
        return None
    years = trading_days / periods
    if years <= 0:
        return None
    return float((total_mult ** (1 / years) - 1) * 100)


def index_daily_returns(prices: pd.Series) -> pd.Series:
    """指数价格日收益率。"""
    if prices is None or prices.empty:
        return pd.Series(dtype=float)
    return prices.pct_change().dropna()


def simulate_nav_series(
    sample: pd.DataFrame,
    amount: float,
    val_col: str,
    buy_fn,
    sell_fn=None,
    has_sell: bool = False,
):
    """模拟策略每日市值与现金流，返回 (nav, cash_flows, trading_days)。"""
    units = 0.0
    cash = 0.0
    navs = []
    flows = []
    prices = []

    for tup in sample.itertuples(index=False, name=None):
        row = dict(zip(sample.columns, tup))
        price = float(row[val_col])
        cf = 0.0
        is_buy = buy_fn(row) if buy_fn else False
        is_sell = sell_fn(row) if sell_fn and has_sell else False

        if is_buy:
            units += amount / price
            cf = amount
        elif is_sell and units > 0:
            cash += units * price
            units = 0.0

        navs.append(cash + units * price)
        flows.append(cf)
        prices.append(price)

    index = pd.DatetimeIndex(sample["_dt"].values)
    return (
        pd.Series(navs, index=index, dtype=float),
        pd.Series(flows, index=index, dtype=float),
        pd.Series(prices, index=index, dtype=float),
        len(sample),
    )


def compute_strategy_metrics(
    sample: pd.DataFrame,
    amount: float,
    val_col: str,
    buy_fn,
    sell_fn=None,
    has_sell: bool = False,
    total_return_pct: float | None = None,
):
    """计算策略夏普、最大回撤、年化收益及指数基准夏普。"""
    nav, flows, prices, trading_days = simulate_nav_series(
        sample,
        amount,
        val_col,
        buy_fn,
        sell_fn=sell_fn,
        has_sell=has_sell,
    )
    if nav.empty:
        return {}

    strat_returns = _daily_returns_with_cash_flows(nav, flows)
    bench_returns = index_daily_returns(prices)

    strat_sharpe = sharpe_ratio(strat_returns)
    bench_sharpe = sharpe_ratio(bench_returns)
    mdd = max_drawdown_pct(nav)
    ann_ret = annualized_return_pct(total_return_pct, trading_days)

    return {
        "sharpe_ratio": strat_sharpe,
        "benchmark_sharpe": bench_sharpe,
        "max_drawdown_pct": mdd,
        "annualized_return_pct": ann_ret,
        "trading_days": trading_days,
    }
