"""回测风险收益指标：夏普比率、最大回撤、年化收益、资金时间利用率等。"""

import math

import pandas as pd

from config import (
    BACKTEST_RISK_FREE_RATE,
    BACKTEST_TRADING_DAYS_PER_YEAR,
)


class CapitalTracker:
    """跟踪每日持仓市值与投资者现金流（买入为负、卖出/终值为正）。"""

    def __init__(self):
        self.deployed_daily: list[float] = []
        self._cashflows: list[tuple[object, float]] = []

    def record_buy(self, dt, amount: float):
        if amount > 0:
            self._cashflows.append((dt, -float(amount)))

    def record_sell(self, dt, proceeds: float):
        if proceeds > 0:
            self._cashflows.append((dt, float(proceeds)))

    def record_day(self, position_value: float):
        self.deployed_daily.append(max(float(position_value), 0.0))

    def cashflows(self) -> list[tuple[object, float]]:
        return list(self._cashflows)

    def finalize(
        self,
        end_dt,
        terminal_value: float,
        trading_days: int,
        profit: float,
        total_bought: float,
    ) -> dict:
        # XIRR 仅计买入注资与期末一次性变现；卖出为组合内股转现金，不重复计入。
        return compute_capital_efficiency_metrics(
            self.deployed_daily,
            self._cashflows,
            end_dt,
            terminal_value,
            trading_days,
            profit,
            total_bought,
            include_sell_flows=False,
        )


def xirr_annual_pct(amounts: list[float], dates: list) -> float | None:
    """货币加权年化收益率 XIRR（%）；dates 与 amounts 一一对应。"""
    if len(amounts) < 2 or len(amounts) != len(dates):
        return None
    if not any(a > 0 for a in amounts) or not any(a < 0 for a in amounts):
        return None

    t0 = pd.Timestamp(dates[0])
    years = [(pd.Timestamp(d) - t0).days / 365.25 for d in dates]

    def npv(rate: float) -> float:
        if rate <= -1:
            return float("inf")
        return sum(a / (1 + rate) ** y for a, y in zip(amounts, years))

    lo, hi = -0.999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-8:
            return float(mid * 100)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return float(((lo + hi) / 2) * 100)


def compute_capital_efficiency_metrics(
    deployed_daily: list[float],
    investor_cashflows: list[tuple[object, float]],
    end_dt,
    terminal_value: float,
    trading_days: int,
    profit: float,
    total_bought: float,
    *,
    include_sell_flows: bool = False,
) -> dict:
    """资金时间利用效率：XIRR、平均占用、持仓年化、在仓占比。"""
    periods = BACKTEST_TRADING_DAYS_PER_YEAR
    years = trading_days / periods if trading_days > 0 else 0.0

    avg_deployed = (
        float(sum(deployed_daily) / len(deployed_daily)) if deployed_daily else 0.0
    )
    peak_deployed = float(max(deployed_daily)) if deployed_daily else 0.0
    days_in_market = sum(1 for v in deployed_daily if v > 0)
    time_in_market_pct = (
        days_in_market / len(deployed_daily) * 100 if deployed_daily else None
    )

    capital_utilization_pct = None
    if total_bought > 0 and avg_deployed > 0:
        capital_utilization_pct = avg_deployed / total_bought * 100

    deployed_return_pct = None
    deployed_annualized_return_pct = None
    if avg_deployed > 0 and profit is not None:
        deployed_return_pct = profit / avg_deployed * 100
        if years > 0:
            total_mult = 1 + deployed_return_pct / 100
            if total_mult > 0:
                deployed_annualized_return_pct = (
                    (total_mult ** (1 / years) - 1) * 100
                )

    calendar_total_return_pct = (
        profit / total_bought * 100 if total_bought > 0 and profit is not None else None
    )
    calendar_annualized_return_pct = annualized_return_pct(
        calendar_total_return_pct, trading_days
    )

    cf_dates = [dt for dt, amt in investor_cashflows if include_sell_flows or amt < 0]
    cf_amounts = [amt for _, amt in investor_cashflows if include_sell_flows or amt < 0]
    if terminal_value and terminal_value > 0:
        cf_dates = [*cf_dates, end_dt]
        cf_amounts = [*cf_amounts, float(terminal_value)]
    xirr_pct = xirr_annual_pct(cf_amounts, cf_dates)

    return {
        "xirr_pct": xirr_pct,
        "avg_deployed_capital": avg_deployed,
        "peak_deployed_capital": peak_deployed,
        "capital_utilization_pct": capital_utilization_pct,
        "time_in_market_pct": time_in_market_pct,
        "deployed_return_pct": deployed_return_pct,
        "deployed_annualized_return_pct": deployed_annualized_return_pct,
        "calendar_annualized_return_pct": calendar_annualized_return_pct,
    }


def merge_capital_metrics_pair(trade_metrics: dict, buy_only_metrics: dict) -> dict:
    """合并波段与仅买入两套资金效率指标。"""
    out = {}
    for key, value in trade_metrics.items():
        out[key] = value
    for key, value in buy_only_metrics.items():
        out[f"buy_only_{key}"] = value
    return out


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
