"""A 股主要宽基指数（非策略）对比：基日以来收益、年化收益、最大回撤。"""

import argparse
import sys
from datetime import date, datetime

import akshare as ak
import pandas as pd

from market_data import configure_stdout_utf8, get_index_perf_history

# 宽基指数清单（规模/综合类，排除红利、低波、行业、主题等策略指数）
BROAD_INDICES = [
    {"code": "000001", "name": "上证指数", "source": "csindex", "base_date": "1991-07-15"},
    {"code": "000016", "name": "上证50", "source": "csindex", "base_date": "2004-01-02"},
    {"code": "000010", "name": "上证180", "source": "csindex", "base_date": "2002-07-01"},
    {"code": "000300", "name": "沪深300", "source": "csindex", "base_date": "2005-04-08"},
    {"code": "000510", "name": "中证A500", "source": "csindex", "base_date": "2005-01-04"},
    {"code": "000905", "name": "中证500", "source": "csindex", "base_date": "2007-01-15"},
    {"code": "000852", "name": "中证1000", "source": "csindex", "base_date": "2014-10-17"},
    {"code": "932000", "name": "中证2000", "source": "csindex", "base_date": "2023-08-31"},
    {"code": "000906", "name": "中证800", "source": "csindex", "base_date": "2007-01-15"},
    {"code": "000985", "name": "中证全指", "source": "csindex", "base_date": "2011-08-02"},
    {"code": "000688", "name": "科创50", "source": "csindex", "base_date": "2020-07-23"},
    {"code": "399001", "name": "深证成指", "source": "sina", "base_date": "1995-01-23"},
    {"code": "399006", "name": "创业板指", "source": "sina", "base_date": "2010-06-01"},
    {"code": "399330", "name": "深证100", "source": "sina", "base_date": "2006-01-24"},
    {"code": "399303", "name": "国证2000", "source": "sina", "base_date": "2014-03-28"},
]


def fetch_csindex_history(code, start_date):
    end_date = date.today().strftime("%Y%m%d")
    start = start_date.replace("-", "")
    history = get_index_perf_history(code, start, end_date)
    if history is None or history.empty:
        return None
    return history[["date", "close"]].dropna()


def fetch_sina_history(code, start_date):
    symbol = f"sz{code}"
    try:
        history = ak.stock_zh_index_daily(symbol=symbol)
    except Exception:
        return None
    if history is None or history.empty:
        return None
    history = history.rename(columns={"date": "date", "close": "close"})
    base = pd.to_datetime(start_date).date()
    history = history[history["date"] >= base]
    return history[["date", "close"]].dropna()


def fetch_history(item):
    if item["source"] == "csindex":
        return fetch_csindex_history(item["code"], item["base_date"])
    return fetch_sina_history(item["code"], item["base_date"])


def compute_max_drawdown(close_series):
    prices = pd.Series(close_series).dropna()
    if prices.empty:
        return None
    return float((prices / prices.cummax() - 1).min())


def compute_metrics(item, history):
    if history is None or len(history) < 2:
        return None

    history = history.sort_values("date").reset_index(drop=True)
    base_target = pd.to_datetime(item["base_date"]).date()
    on_or_after = history[history["date"] >= base_target]
    if on_or_after.empty:
        on_or_after = history

    first = on_or_after.iloc[0]
    last = history.iloc[-1]
    total_return = last["close"] / first["close"] - 1
    years = (pd.Timestamp(last["date"]) - pd.Timestamp(first["date"])).days / 365.25
    annualized = (1 + total_return) ** (1 / years) - 1 if years > 0 else None
    max_drawdown = compute_max_drawdown(history["close"])

    return {
        "code": item["code"],
        "name": item["name"],
        "base_date": first["date"],
        "last_date": last["date"],
        "years": years,
        "total_return": total_return,
        "annualized_return": annualized,
        "max_drawdown": max_drawdown,
        "trading_days": len(history),
    }


def format_percent(value):
    return f"{value * 100:+.2f}%" if value is not None else "—"


def run_comparison(end_date=None):
    rows = []
    for item in BROAD_INDICES:
        history = fetch_history(item)
        metrics = compute_metrics(item, history)
        if metrics:
            rows.append(metrics)

    if not rows:
        raise RuntimeError("未能获取任何宽基指数数据")

    df = pd.DataFrame(rows)
    df = df.sort_values("annualized_return", ascending=False).reset_index(drop=True)
    return df


def print_table(df):
    print("\n=== A股主要宽基指数对比（非策略） ===")
    print(f"行情截止: {df['last_date'].max()} | 收益为基日以来价格指数（不复权、不含分红）")
    print(
        f"{'指数':<12} {'代码':<8} {'基日':<12} {'基日以来':>10} "
        f"{'年化收益':>10} {'最大回撤':>10} {'样本(年)':>8}"
    )
    print("-" * 82)
    for row in df.itertuples():
        print(
            f"{row.name:<12} {row.code:<8} {row.base_date!s:<12} "
            f"{format_percent(row.total_return):>10} "
            f"{format_percent(row.annualized_return):>10} "
            f"{format_percent(row.max_drawdown):>10} "
            f"{row.years:>8.1f}"
        )
    print("-" * 82)
    print("说明: 行情来自中证指数 API / 新浪；最大回撤基于收盘价全样本计算。")


def main(argv=None):
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="A股宽基指数收益与回撤对比")
    parser.parse_args(argv)
    try:
        df = run_comparison()
    except RuntimeError as exc:
        print(exc)
        return 1
    print_table(df)
    return 0


if __name__ == "__main__":
    sys.exit(main())
