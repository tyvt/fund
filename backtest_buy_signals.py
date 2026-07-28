"""按当前买入标准回测指定年份各指数/模块的买入天数与定投收益。"""

import argparse
import sys

import pandas as pd

from cn_broad_data import (
    attach_cn_broad_percentiles,
    build_cn_broad_valuation_history,
)
from cn_broad_signal import evaluate_cn_broad_buy
from config import (
    A500_INDEX,
    A500_MARKET_DATA_START,
    BUY_RANGE_LOOKBACK_DAYS,
    BUY_TREND_MA_DAYS,
    BUY_TREND_SLOPE_LOOKBACK_DAYS,
    CN_BROAD_INDICES,
    CYB_BUY_HIGH_LOOKBACK_DAYS,
    CYB_BUY_LOW_LOOKBACK_DAYS,
    CYB_INDEX,
    DIVIDEND_SIGNAL_HISTORY_START,
    HSTECH_BUY_HIGH_LOOKBACK_DAYS,
    HSTECH_BUY_LOW_LOOKBACK_DAYS,
    HSTECH_INDEX,
    INDICES,
    NDX_INDEX,
    BACKTEST_OUTPUT_DIR,
    PROJECT_DIR,
    SPX_INDEX,
    US_INDEX_KEYS,
    format_backtest_amount_note,
    resolve_backtest_amounts,
)
from cyb_data import attach_percentiles as attach_cyb_percentiles
from cyb_data import build_cyb_valuation_panel, fetch_cyb_price_history
from cyb_signal import evaluate_cyb_signal
from hstech_data import attach_percentiles as attach_hstech_percentiles
from hstech_data import build_hstech_valuation_panel, fetch_hstech_price_history
from hstech_signal import evaluate_hstech_signal
from dividend_data import build_signal_history, is_buy_signal_row
from market_data import configure_stdout_utf8, get_gov_bond_yield_history
from us_index_data import (
    build_daily_valuation_panel,
    build_valuation_panel,
    compute_historical_earnings_growth,
)
from us_index_signal import is_buy as is_us_index_buy
from us_index_signal import resolve_expected_growth
from price_position import attach_ma_trend, attach_pct_above_low, attach_pct_below_high, attach_year_range_position

CN_BROAD_BACKTEST_INDICES = CN_BROAD_INDICES

BACKTEST_RETURN_FOOTNOTE = (
    "红利指数（930955/H30269）收益率按中证全收益指数（H20955/H20269）估算，含分红再投资；"
    "其他指数为价格指数、未含分红。未计手续费；纳指为美元计价指数点位。"
)


class BacktestPanels:
    """一次拉取全部估值面板，供多年份回测/列日期复用。"""

    def __init__(self):
        self.bond_history = get_gov_bond_yield_history()
        self._dividend = {}
        self._cn_broad = {}
        self._cyb = None
        self._hstech = None
        self._us = {}

    def dividend_panel(self, index_code):
        if index_code not in self._dividend:
            self._dividend[index_code] = build_signal_history(
                index_code,
                start_date=DIVIDEND_SIGNAL_HISTORY_START,
                bond_history=self.bond_history,
            )
        return self._dividend[index_code]

    def cn_broad_panel(self, index_code):
        if index_code not in self._cn_broad:
            panel = build_cn_broad_valuation_history(
                index_code,
                start_date="20150101",
                bond_history=self.bond_history,
            )
            self._cn_broad[index_code] = attach_cn_broad_percentiles(
                panel, index_code
            )
        return self._cn_broad[index_code]

    def us_index_panel(self, key):
        if key not in self._us:
            daily, _ = build_daily_valuation_panel(key)
            month_panel, _ = build_valuation_panel(key)
            growth = compute_historical_earnings_growth(month_panel, key)
            self._us[key] = (daily, growth)
        return self._us[key]

    def cyb_panel(self):
        if self._cyb is None:
            panel = build_cyb_valuation_panel()
            prices = fetch_cyb_price_history()
            prices["date_only"] = pd.to_datetime(prices["date"]).dt.date
            panel = panel.merge(
                prices[["date_only", "close"]],
                on="date_only",
                how="left",
            )
            self._cyb = attach_cyb_percentiles(panel)
            self._cyb = attach_pct_above_low(
                self._cyb, lookback_days=CYB_BUY_LOW_LOOKBACK_DAYS
            )
            self._cyb = attach_pct_below_high(
                self._cyb, lookback_days=CYB_BUY_HIGH_LOOKBACK_DAYS
            )
            self._cyb = attach_year_range_position(
                self._cyb, lookback_days=BUY_RANGE_LOOKBACK_DAYS, date_col="date"
            )
            self._cyb = attach_ma_trend(
                self._cyb,
                ma_days=BUY_TREND_MA_DAYS,
                slope_lookback=BUY_TREND_SLOPE_LOOKBACK_DAYS,
            )
        return self._cyb

    def hstech_panel(self):
        if self._hstech is None:
            panel = build_hstech_valuation_panel()
            prices = fetch_hstech_price_history()
            prices["date_only"] = pd.to_datetime(prices["date"]).dt.date
            panel = panel.merge(
                prices[["date_only", "close"]],
                on="date_only",
                how="left",
            )
            self._hstech = attach_hstech_percentiles(panel)
            self._hstech = attach_pct_above_low(
                self._hstech, lookback_days=HSTECH_BUY_LOW_LOOKBACK_DAYS
            )
            self._hstech = attach_pct_below_high(
                self._hstech, lookback_days=HSTECH_BUY_HIGH_LOOKBACK_DAYS
            )
            self._hstech = attach_year_range_position(
                self._hstech, lookback_days=BUY_RANGE_LOOKBACK_DAYS, date_col="date"
            )
            self._hstech = attach_ma_trend(
                self._hstech,
                ma_days=BUY_TREND_MA_DAYS,
                slope_lookback=BUY_TREND_SLOPE_LOOKBACK_DAYS,
            )
        return self._hstech


_PANELS = None
BACKTEST_DIR = BACKTEST_OUTPUT_DIR


def get_panels():
    global _PANELS
    if _PANELS is None:
        _PANELS = BacktestPanels()
    return _PANELS
def _year_mask(series, year):
    dates = pd.to_datetime(series)
    return (dates.dt.year == year).values


def _year_price_stats(panel, year, buy_fn, date_col="date", price_col="close"):
    """统计某年指数收盘价最高/最低，及买入信号日的均价（等额投入加权）。"""
    if panel is None or panel.empty or price_col not in panel.columns:
        return None

    work = panel.copy()
    if date_col == "date_only":
        work["_dt"] = pd.to_datetime(work["date_only"])
    else:
        work["_dt"] = pd.to_datetime(work[date_col])

    sample = work.dropna(subset=[price_col])
    sample = sample[sample["_dt"].dt.year == year]
    if sample.empty:
        return None

    prices = sample[price_col].astype(float)
    buy_prices = [
        float(row[price_col])
        for _, row in sample.iterrows()
        if buy_fn(row)
    ]
    if buy_prices:
        avg_buy_price = sum(buy_prices) / len(buy_prices)
    else:
        avg_buy_price = None

    return {
        "year_high": float(prices.max()),
        "year_low": float(prices.min()),
        "avg_buy_price": avg_buy_price,
    }


def _simulate_dca_returns(
    panel, year, buy_fn, amount=300.0, date_col="date", price_col="close",
    valuation_price_col=None,
):
    """每个买入日投入固定金额，按最新收盘价估算持仓市值与收益率。"""
    val_col = valuation_price_col or price_col
    price_stats = _year_price_stats(panel, year, buy_fn, date_col, price_col)
    if price_stats is None:
        return None

    if panel is None or panel.empty or price_col not in panel.columns:
        return None

    work = panel.copy()
    if date_col == "date_only":
        work["_dt"] = pd.to_datetime(work["date_only"])
    else:
        work["_dt"] = pd.to_datetime(work[date_col])

    priced = work.dropna(subset=[price_col, val_col])
    if priced.empty:
        return None

    latest = priced.iloc[-1]
    latest_price = float(latest[val_col])
    latest_date = latest["_dt"]

    sample = priced[priced["_dt"].dt.year == year]
    if sample.empty:
        return None

    buy_prices = []
    for _, row in sample.iterrows():
        if buy_fn(row):
            buy_prices.append(float(row[val_col]))

    buy_days = len(buy_prices)
    total_days = len(sample)
    base_stats = {
        "year_high": price_stats["year_high"],
        "year_low": price_stats["year_low"],
        "avg_buy_price": price_stats["avg_buy_price"],
    }
    if buy_days == 0:
        return {
            "buy_days": 0,
            "total_days": total_days,
            "buy_pct": 0.0,
            "invested": 0.0,
            "market_value": 0.0,
            "profit": 0.0,
            "return_pct": None,
            "latest_date": latest_date,
            "latest_price": latest_price,
            **base_stats,
        }

    total_units = sum(amount / price for price in buy_prices)
    invested = buy_days * amount
    market_value = total_units * latest_price
    profit = market_value - invested
    return {
        "buy_days": buy_days,
        "total_days": total_days,
        "buy_pct": buy_days / total_days * 100 if total_days else 0.0,
        "invested": invested,
        "market_value": market_value,
        "profit": profit,
        "return_pct": profit / invested * 100 if invested else None,
        "latest_date": latest_date,
        "latest_price": latest_price,
        **base_stats,
    }


def _count_buy_days(panel, year, buy_fn, date_col="date", price_col="close"):
    """统计某年买入信号天数及有效样本天数。"""
    price_stats = _year_price_stats(panel, year, buy_fn, date_col, price_col)
    if price_stats is None:
        return None

    work = panel.copy()
    if date_col == "date_only":
        work["_dt"] = pd.to_datetime(work["date_only"])
    else:
        work["_dt"] = pd.to_datetime(work[date_col])

    mask = work["_dt"].dt.year == year
    sample = work.loc[mask]
    if sample.empty:
        return None

    buy_days = 0
    for _, row in sample.iterrows():
        if buy_fn(row):
            buy_days += 1

    total = len(sample)
    return {
        "buy_days": buy_days,
        "total_days": total,
        "buy_pct": buy_days / total * 100 if total else 0,
        "year_high": price_stats["year_high"],
        "year_low": price_stats["year_low"],
        "avg_buy_price": price_stats["avg_buy_price"],
    }


def _cn_broad_buy_snapshot(row, index_code):
    return evaluate_cn_broad_buy(
        {
            "code": index_code,
            "pe_percentile": row.get("pe_percentile"),
            "pb_percentile": row.get("pb_percentile"),
            "dividend_percentile": row.get("dividend_percentile"),
            "spread_percentile": row.get("spread_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
            "ma_slope_pct": row.get("ma_slope_pct"),
        }
    )["is_buy"]


def _us_buy_snapshot(key, row, historical_growth=None):
    snapshot = {
        "forward_pe": row.get("forward_pe"),
        "trailing_pe": row.get("trailing_pe"),
        "forward_pe_percentile": row.get("forward_pe_percentile"),
        "trailing_pe_percentile": row.get("trailing_pe_percentile"),
        "us10y_percentile": row.get("us10y_percentile"),
        "implied_growth": row.get("implied_growth"),
        "historical_growth": historical_growth,
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
    }
    snapshot["expected_growth"] = resolve_expected_growth(key, snapshot)
    return is_us_index_buy(key, snapshot)


def backtest_dividend(year, bond_history=None, amount=None, panels=None):
    panels = panels or get_panels()
    rows = []
    for item in INDICES:
        panel = panels.dividend_panel(item["code"])
        buy_fn = lambda r, code=item["code"]: is_buy_signal_row(r, code)  # noqa: E731
        if amount is not None:
            result = _simulate_dca_returns(
                panel, year, buy_fn, amount=amount,
                valuation_price_col="total_return_close",
            )
        else:
            result = _count_buy_days(panel, year, buy_fn)
        if result:
            rows.append({"code": item["code"], "name": item["name"], **result})
    return rows


def _cn_broad_no_data_row(index_meta, amount=None):
    """当年无行情样本时仍输出一行，避免报告中指数「消失」。"""
    if index_meta["code"] == A500_INDEX["code"]:
        note = f"当年无行情（中证A500自{A500_MARKET_DATA_START[:7]}发布，与中证500不同）"
    else:
        note = "当年无指数行情数据"
    row = {
        "buy_days": 0,
        "total_days": 0,
        "buy_pct": 0.0,
        "year_high": None,
        "year_low": None,
        "avg_buy_price": None,
        "note": note,
    }
    if amount is not None:
        row.update(
            {
                "invested": 0.0,
                "market_value": 0.0,
                "profit": 0.0,
                "return_pct": None,
                "latest_date": None,
                "latest_price": None,
            }
        )
    return row


def backtest_cn_broad(year, index_meta, amount=None, panels=None):
    panels = panels or get_panels()
    code = index_meta["code"]
    panel = panels.cn_broad_panel(code)
    buy_fn = lambda r, c=code: _cn_broad_buy_snapshot(r, c)  # noqa: E731
    if amount is not None:
        result = _simulate_dca_returns(panel, year, buy_fn, amount=amount)
    else:
        result = _count_buy_days(panel, year, buy_fn)
    if not result:
        return [{"code": code, "name": index_meta["name"], **_cn_broad_no_data_row(index_meta, amount)}]
    return [{"code": code, "name": index_meta["name"], **result}]


US_INDEX_META = {"ndx": NDX_INDEX, "spx": SPX_INDEX}
US_INDEX_NOTES = {
    "ndx": "日频（10Y日更，Forward PE按月对齐）",
    "spx": "日频（10Y日更，Forward PE按季对齐）",
}


def backtest_us_index(year, key, amount=None, panels=None):
    panels = panels or get_panels()
    daily, historical_growth = panels.us_index_panel(key)
    buy_fn = lambda r, k=key, g=historical_growth: _us_buy_snapshot(k, r, g)  # noqa: E731
    if amount is not None:
        result = _simulate_dca_returns(
            daily, year, buy_fn, amount=amount, date_col="date"
        )
    else:
        result = _count_buy_days(daily, year, buy_fn, date_col="date")
    if not result:
        return []
    result["note"] = US_INDEX_NOTES[key]
    meta = US_INDEX_META[key]
    return [{"code": meta["code"], "name": meta["name"], **result}]


def backtest_cyb(year, amount=None, panels=None):
    panels = panels or get_panels()
    panel = panels.cyb_panel()
    buy_fn = lambda r: evaluate_cyb_signal(  # noqa: E731
        {
            "pe": r["pe"],
            "pb": r["pb"],
            "pe_percentile": r.get("pe_percentile"),
            "pb_percentile": r.get("pb_percentile"),
            "pct_above_low": r.get("pct_above_low"),
            "pct_below_high": r.get("pct_below_high"),
            "year_range_position": r.get("year_range_position"),
            "ma_slope_pct": r.get("ma_slope_pct"),
        }
    )["is_buy"]
    if amount is not None:
        result = _simulate_dca_returns(
            panel, year, buy_fn, amount=amount, date_col="date"
        )
    else:
        result = _count_buy_days(
            panel, year, buy_fn, date_col="date"
        )
    if not result:
        return []
    return [{"code": CYB_INDEX["code"], "name": CYB_INDEX["name"], **result}]


def backtest_hstech(year, amount=None, panels=None):
    panels = panels or get_panels()
    panel = panels.hstech_panel()
    buy_fn = lambda r: evaluate_hstech_signal(  # noqa: E731
        {
            "pe": r["pe"],
            "pe_percentile": r.get("pe_percentile"),
            "dividend_percentile": r.get("dividend_percentile"),
            "pct_above_low": r.get("pct_above_low"),
            "pct_below_high": r.get("pct_below_high"),
            "year_range_position": r.get("year_range_position"),
            "ma_slope_pct": r.get("ma_slope_pct"),
        }
    )["is_buy"]
    if amount is not None:
        result = _simulate_dca_returns(
            panel, year, buy_fn, amount=amount, date_col="date"
        )
    else:
        result = _count_buy_days(
            panel, year, buy_fn, date_col="date"
        )
    if not result:
        return []
    return [{"code": HSTECH_INDEX["code"], "name": HSTECH_INDEX["name"], **result}]


def _collect_buy_dates(panel, year, buy_fn, date_col="date"):
    if panel is None or panel.empty:
        return []
    work = panel.copy()
    if date_col == "date_only":
        work["_dt"] = pd.to_datetime(work["date_only"])
    else:
        work["_dt"] = pd.to_datetime(work[date_col])
    days = []
    for _, row in work[work["_dt"].dt.year == year].iterrows():
        if buy_fn(row):
            days.append(pd.Timestamp(row[date_col] if date_col != "date_only" else row["date_only"]).strftime("%Y-%m-%d"))
    return days


def _iter_backtest_configs(panels):
    """各指数回测配置：panel、buy_fn、日期/价格列（与 report 判定一致）。"""
    for item in INDICES:
        code = item["code"]
        yield {
            "code": code,
            "name": item["name"],
            "panel": panels.dividend_panel(code),
            "buy_fn": lambda r, c=code: is_buy_signal_row(r, c),
            "date_col": "date",
            "price_col": "close",
            "note": "日频，每交易日评估",
        }

    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        yield {
            "code": code,
            "name": item["name"],
            "panel": panels.cn_broad_panel(code),
            "buy_fn": lambda r, c=code: _cn_broad_buy_snapshot(r, c),
            "date_col": "date",
            "price_col": "close",
            "note": "日频，每交易日评估",
        }

    cyb = panels.cyb_panel()
    yield {
        "code": CYB_INDEX["code"],
        "name": CYB_INDEX["name"],
        "panel": cyb,
        "buy_fn": lambda r: evaluate_cyb_signal(
            {
                "pe": r["pe"],
                "pb": r["pb"],
                "pe_percentile": r.get("pe_percentile"),
                "pb_percentile": r.get("pb_percentile"),
                "pct_above_low": r.get("pct_above_low"),
                "pct_below_high": r.get("pct_below_high"),
                "year_range_position": r.get("year_range_position"),
                "ma_slope_pct": r.get("ma_slope_pct"),
            }
        )["is_buy"],
        "date_col": "date",
        "price_col": "close",
        "note": "日频，每交易日评估",
    }

    hstech = panels.hstech_panel()
    yield {
        "code": HSTECH_INDEX["code"],
        "name": HSTECH_INDEX["name"],
        "panel": hstech,
        "buy_fn": lambda r: evaluate_hstech_signal(
            {
                "pe": r["pe"],
                "pe_percentile": r.get("pe_percentile"),
                "dividend_percentile": r.get("dividend_percentile"),
                "pct_above_low": r.get("pct_above_low"),
                "pct_below_high": r.get("pct_below_high"),
                "year_range_position": r.get("year_range_position"),
                "ma_slope_pct": r.get("ma_slope_pct"),
            }
        )["is_buy"],
        "date_col": "date",
        "price_col": "close",
        "note": "日频，每交易日评估",
    }

    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        meta = US_INDEX_META[key]
        yield {
            "code": meta["code"],
            "name": meta["name"],
            "panel": daily,
            "buy_fn": lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g),
            "date_col": "date",
            "price_col": "close",
            "note": US_INDEX_NOTES[key],
        }


def _resolve_date_value(row, date_col):
    if date_col == "date_only":
        return row["date_only"]
    return row[date_col]


def build_daily_table(
    panel, year, buy_fn, date_col="date", price_col="close"
):
    """构建某年逐交易日表：日期、收盘价、是否买入。"""
    if panel is None or panel.empty:
        return pd.DataFrame(columns=["date", "close", "buy"])

    work = panel.copy()
    if date_col == "date_only":
        work["_dt"] = pd.to_datetime(work["date_only"])
    else:
        work["_dt"] = pd.to_datetime(work[date_col])

    sample = work[work["_dt"].dt.year == year].sort_values("_dt")
    if sample.empty:
        return pd.DataFrame(columns=["date", "close", "buy"])

    rows = []
    for _, row in sample.iterrows():
        raw_date = _resolve_date_value(row, date_col)
        close_val = row.get(price_col)
        rows.append(
            {
                "date": pd.Timestamp(raw_date).strftime("%Y-%m-%d"),
                "close": float(close_val) if pd.notna(close_val) else None,
                "buy": "买入" if buy_fn(row) else "",
            }
        )
    return pd.DataFrame(rows)


def collect_daily_tables(year, panels=None, index_codes=None):
    """收集各指数全年逐交易日表。"""
    panels = panels or get_panels()
    codes = {c.upper() for c in index_codes} if index_codes else None
    tables = []

    for cfg in _iter_backtest_configs(panels):
        if codes and cfg["code"].upper() not in codes:
            continue
        table = build_daily_table(
            cfg["panel"],
            year,
            cfg["buy_fn"],
            date_col=cfg["date_col"],
            price_col=cfg["price_col"],
        )
        if table.empty:
            continue
        tables.append(
            {
                "name": cfg["name"],
                "code": cfg["code"],
                "table": table,
            }
        )
    return tables


def _format_calendar_cell(close, is_buy):
    """日历格：收盘价；买入日加 ★ 并加粗。"""
    if close is None:
        return "—"
    text = _md_price(close)
    if is_buy:
        return f"**★{text}**"
    return text


def _format_index_calendar_markdown(table):
    """按月×日网格输出收盘价（纵轴月份，横轴日期）。"""
    if table is None or table.empty:
        return []

    work = table.copy()
    work["_dt"] = pd.to_datetime(work["date"])
    work["month"] = work["_dt"].dt.month
    work["day"] = work["_dt"].dt.day
    work["is_buy"] = work["buy"] == "买入"

    days = list(range(1, 32))
    header = "| 月 | " + " | ".join(str(d) for d in days) + " |"
    sep = "| --- | " + " | ".join(["---:"] * len(days)) + " |"
    lines = [header, sep]

    for month in range(1, 13):
        month_data = work[work["month"] == month]
        if month_data.empty:
            continue
        by_day = {
            int(row["day"]): row for _, row in month_data.iterrows()
        }
        cells = [f"{month}月"]
        for day in days:
            if day in by_day:
                row = by_day[day]
                cells.append(
                    _format_calendar_cell(row["close"], row["is_buy"])
                )
            else:
                cells.append("—")
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("说明：格内为收盘价；**★** 表示买入信号日；非交易日显示 —。")
    return lines


def _format_daily_tables_markdown(daily_tables):
    """将逐交易日表格式化为 Markdown 章节（月×日日历网格）。"""
    if not daily_tables:
        return []

    lines = ["", "## 逐交易日明细", ""]
    for item in daily_tables:
        table = item["table"]
        buy_count = int((table["buy"] == "买入").sum())
        lines.extend([
            f"### {item['name']}（{item['code']}）",
            "",
            f"共 {len(table)} 个交易日，买入 {buy_count} 天。",
            "",
        ])
        lines.extend(_format_index_calendar_markdown(table))
        lines.append("")
    return lines


def print_daily_table(year, index_code, panels=None):
    """在控制台打印指定指数全年逐交易日表。"""
    panels = panels or get_panels()
    code_key = index_code.upper()
    for cfg in _iter_backtest_configs(panels):
        if cfg["code"].upper() != code_key:
            continue
        table = build_daily_table(
            cfg["panel"],
            year,
            cfg["buy_fn"],
            date_col=cfg["date_col"],
            price_col=cfg["price_col"],
        )
        if table.empty:
            print(f"{cfg['name']} ({cfg['code']}) 在 {year} 年无交易日数据")
            return

        buy_count = int((table["buy"] == "买入").sum())
        print(
            f"\n=== {year} 年 {cfg['name']} ({cfg['code']}) 逐交易日 ==="
            f"（共 {len(table)} 天，买入 {buy_count} 天；★=买入）"
        )
        for line in _format_index_calendar_markdown(table):
            print(line)
        return

    print(f"未找到指数代码: {index_code}")


def list_buy_dates(year, panels=None):
    """列出指定年份各指数买入日期（与 report 共用判定逻辑）。"""
    panels = panels or get_panels()
    out = {}
    for cfg in _iter_backtest_configs(panels):
        key = f"{cfg['name']} ({cfg['code']})"
        out[key] = _collect_buy_dates(
            cfg["panel"],
            year,
            cfg["buy_fn"],
            date_col=cfg["date_col"],
        )
    return out


def print_buy_dates(years):
    panels = get_panels()
    print("正在加载数据（仅首次较慢，后续年份复用缓存）...")
    for year in years:
        print(f"\n=== {year} 年买入日期 ===")
        dates_by_name = list_buy_dates(year, panels=panels)
        for name, days in dates_by_name.items():
            print(f"\n{name}: {len(days)}天")
            print("  " + (", ".join(days) if days else "—"))


def run_backtest(year, amounts=None, panels=None):
    print(f"正在回测 {year} 年买入信号（使用当前 config 阈值）...")
    panels = panels or get_panels()
    div_amt = amounts["dividend"] if amounts else None
    broad_amt = amounts["cn_broad"] if amounts else None
    other_amt = amounts["other"] if amounts else None

    rows = []
    rows.extend(backtest_dividend(year, panels=panels, amount=div_amt))
    for item in CN_BROAD_BACKTEST_INDICES:
        rows.extend(backtest_cn_broad(year, item, panels=panels, amount=broad_amt))
    rows.extend(backtest_cyb(year, panels=panels, amount=other_amt))
    rows.extend(backtest_hstech(year, panels=panels, amount=other_amt))
    for key in US_INDEX_KEYS:
        rows.extend(backtest_us_index(year, key, panels=panels, amount=other_amt))
    return rows


def _backtest_result_path(year):
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    return BACKTEST_DIR / f"{year}.md"


def _md_price(value):
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value:,.2f}"
    return f"{value:.2f}"


def _sum_int(rows, key):
    return sum(int(row.get(key, 0) or 0) for row in rows)


def _sum_float(rows, key):
    return sum(float(row.get(key, 0) or 0) for row in rows)


def _agg_return_pct(profit, invested):
    if invested > 0:
        return profit / invested * 100
    return None


def _buy_signal_totals(rows):
    invested = _sum_float(rows, "invested")
    profit = _sum_float(rows, "profit")
    market_value = _sum_float(rows, "market_value")
    return {
        "buy_days": _sum_int(rows, "buy_days"),
        "invested": invested,
        "market_value": market_value,
        "profit": profit,
        "return_pct": _agg_return_pct(profit, invested),
    }


def _format_backtest_summary_markdown(rows, year, amounts=None):
    """逐年回测汇总表（合并信号统计、价格与收益）。"""
    val_text = "—"
    if amounts is not None:
        latest_dates = [row.get("latest_date") for row in rows if row.get("latest_date")]
        if latest_dates:
            val_text = pd.Timestamp(max(latest_dates)).strftime("%Y-%m-%d")

    lines = ["## 回测汇总", ""]
    if amounts is not None:
        lines.append(
            f"{format_backtest_amount_note(amounts)}；持仓市值估值截至 **{val_text}**。"
        )
        lines.append("")
        header = (
            "| 指数 | 代码 | 买入次 | 样本 | 占比 | 年内高 | 年内低 | 买入均价 | "
            "投入 | 市值 | 盈亏 | 收益率 | 备注 |"
        )
        sep = (
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | "
            "---: | ---: | ---: | ---: | --- |"
        )
    else:
        lines.append("仅统计买入信号次数与价格位置，未计算定投收益。")
        lines.append("")
        header = (
            "| 指数 | 代码 | 买入次 | 样本 | 占比 | 年内高 | 年内低 | 买入均价 | 备注 |"
        )
        sep = (
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"
        )

    lines.extend([header, sep])

    for row in rows:
        note = row.get("note", "日频，每交易日评估")
        base = (
            f"| {row['name']} | {row['code']} | {row['buy_days']} | "
            f"{row['total_days']} | {row['buy_pct']:.1f}% | "
            f"{_md_price(row.get('year_high'))} | {_md_price(row.get('year_low'))} | "
            f"{_md_price(row.get('avg_buy_price'))}"
        )
        if amounts is not None:
            ret = row.get("return_pct")
            ret_text = f"{ret:.1f}%" if ret is not None else "—"
            invested = row.get("invested", 0)
            market_value = row.get("market_value", 0)
            profit = row.get("profit", 0)
            lines.append(
                f"{base} | {invested:.0f} | {market_value:.0f} | "
                f"{profit:+.0f} | {ret_text} | {note} |"
            )
        else:
            lines.append(f"{base} | {note} |")

    totals = _buy_signal_totals(rows)
    if amounts is not None:
        total_ret = totals["return_pct"]
        total_ret_text = f"{total_ret:.1f}%" if total_ret is not None else "—"
        lines.append(
            f"| **合计** | — | {totals['buy_days']} | — | — | — | — | — | "
            f"{totals['invested']:.0f} | {totals['market_value']:.0f} | "
            f"{totals['profit']:+.0f} | {total_ret_text} | — |"
        )
    else:
        lines.append(
            f"| **合计** | — | {totals['buy_days']} | — | — | — | — | — | 买入次数合计 |"
        )

    footnotes = [
        "",
        "买入次/样本/占比：按交易日计次；纳指/标普 10Y 与价格日更，Forward PE 按月/按季对齐。",
        "年内高/低为价格指数收盘极值；买入均价为信号日收盘价算术平均（无买入为 —）；纳指美元计价。",
        f"中证A500（000510）与中证500（000905）不同；A500 行情自 {A500_MARKET_DATA_START[:7]} 起。",
    ]
    if amounts is not None:
        footnotes.append(
            f"投入/市值/盈亏/收益率：按指数类别使用固定单次买入金额，持仓按估值日收盘价（或红利全收益指数）估算；"
            f"{BACKTEST_RETURN_FOOTNOTE}"
        )
    lines.extend(footnotes)
    return lines


def _format_backtest_markdown(year, rows, amounts=None, daily_tables=None):
    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {year} 年买入信号回测",
        "",
        f"> 生成时间：{generated_at}  ",
        "> 买入标准：当前 config 阈值  ",
    ]
    if amounts is not None:
        lines.append(f"> 每次买入金额：{format_backtest_amount_note(amounts)}")
    else:
        lines.append("> 每次买入金额：仅统计次数")
    lines.append("")

    lines.extend(_format_backtest_summary_markdown(rows, year, amounts=amounts))

    if daily_tables:
        lines.extend(_format_daily_tables_markdown(daily_tables))

    return "\n".join(lines).rstrip() + "\n"


def save_backtest_result(year, rows, amounts=None, panels=None, index_codes=None):
    """保存回测结果到本地 Markdown，每年一份，重新运行会覆盖。"""
    daily_tables = collect_daily_tables(year, panels=panels, index_codes=index_codes)
    path = _backtest_result_path(year)
    path.write_text(
        _format_backtest_markdown(
            year,
            rows,
            amounts=amounts,
            daily_tables=daily_tables,
        ),
        encoding="utf-8",
    )
    return path


def _format_price(value):
    if value is None:
        return "     —"
    if value >= 1000:
        return f"{value:>10,.2f}"
    return f"{value:>10.2f}"


def print_summary_table(rows, year, amounts=None):
    """控制台打印合并后的逐年回测汇总表。"""
    if not rows:
        print("无有效回测结果")
        return

    val_text = "—"
    if amounts is not None:
        latest_dates = [row.get("latest_date") for row in rows if row.get("latest_date")]
        if latest_dates:
            val_text = pd.Timestamp(max(latest_dates)).strftime("%Y-%m-%d")
        print(
            f"\n=== {year} 年买入信号回测（{format_backtest_amount_note(amounts)}，"
            f"估值截至 {val_text}） ==="
        )
        print(
            f"{'指数':<14} {'代码':<8} {'买入':>5} {'样本':>5} {'占比':>6} "
            f"{'年内高':>10} {'年内低':>10} {'均价':>10} "
            f"{'投入':>7} {'市值':>7} {'盈亏':>7} {'收益':>7}"
        )
        print("-" * 110)
        for row in rows:
            ret = row.get("return_pct")
            ret_text = f"{ret:>6.1f}%" if ret is not None else "    —"
            print(
                f"{row['name']:<14} {row['code']:<8} "
                f"{row['buy_days']:>5} {row['total_days']:>5} "
                f"{row['buy_pct']:>5.1f}% "
                f"{_format_price(row.get('year_high'))} "
                f"{_format_price(row.get('year_low'))} "
                f"{_format_price(row.get('avg_buy_price'))} "
                f"{row.get('invested', 0):>7.0f} "
                f"{row.get('market_value', 0):>7.0f} "
                f"{row.get('profit', 0):>+7.0f} {ret_text}"
            )
        totals = _buy_signal_totals(rows)
        total_ret = totals["return_pct"]
        total_ret_text = f"{total_ret:>6.1f}%" if total_ret is not None else "    —"
        print("-" * 110)
        print(
            f"{'合计':<14} {'—':<8} "
            f"{totals['buy_days']:>5} {'—':>5} {'—':>6} "
            f"{'—':>10} {'—':>10} {'—':>10} "
            f"{totals['invested']:>7.0f} "
            f"{totals['market_value']:>7.0f} "
            f"{totals['profit']:>+7.0f} {total_ret_text}"
        )
        print("-" * 110)
        print(BACKTEST_RETURN_FOOTNOTE)
    else:
        print(f"\n=== {year} 年买入信号回测（当前买入标准） ===")
        print(
            f"{'指数':<14} {'代码':<8} {'买入':>5} {'样本':>5} {'占比':>6} "
            f"{'年内高':>10} {'年内低':>10} {'均价':>10}"
        )
        print("-" * 80)
        for row in rows:
            print(
                f"{row['name']:<14} {row['code']:<8} "
                f"{row['buy_days']:>5} {row['total_days']:>5} "
                f"{row['buy_pct']:>5.1f}% "
                f"{_format_price(row.get('year_high'))} "
                f"{_format_price(row.get('year_low'))} "
                f"{_format_price(row.get('avg_buy_price'))}"
            )
        totals = _buy_signal_totals(rows)
        print("-" * 80)
        print(
            f"{'合计':<14} {'—':<8} "
            f"{totals['buy_days']:>5} {'—':>5} {'—':>6} "
            f"{'—':>10} {'—':>10} {'—':>10}"
        )
        print("-" * 80)


def print_table(rows, year, amounts=None):
    print_summary_table(rows, year, amounts=amounts)


def main(argv=None):
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="回测指定年份买入信号天数")
    parser.add_argument(
        "--year",
        type=int,
        action="append",
        help="回测年份（可多次指定，如 --year 2025 --year 2026）",
    )
    parser.add_argument(
        "--list-dates",
        action="store_true",
        help="仅列出各指数买入日期（不计算收益）",
    )
    parser.add_argument(
        "--daily-table",
        action="store_true",
        help="打印指定指数全年逐交易日表（需配合 --index）",
    )
    parser.add_argument(
        "--index",
        action="append",
        dest="index_codes",
        metavar="CODE",
        help="指数代码，可多次指定（配合 --daily-table 或仅生成指定指数逐日表）",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=None,
        help="统一覆盖所有指数单次买入金额（元；默认红利300、宽基100、其他300；设为0则只统计次数）",
    )
    args = parser.parse_args(argv)
    years = args.year or [2025]
    if args.amount is not None and args.amount <= 0:
        amounts = None
    elif args.amount is not None:
        amounts = resolve_backtest_amounts(args.amount)
    else:
        amounts = resolve_backtest_amounts()

    try:
        if args.list_dates:
            print_buy_dates(years)
            return 0

        if args.daily_table:
            if not args.index_codes:
                print("请使用 --index 指定指数代码，例如: --daily-table --index 000510")
                return 1
            panels = get_panels()
            print("正在加载数据（仅首次较慢，后续年份复用缓存）...")
            for year in years:
                for code in args.index_codes:
                    print_daily_table(year, code, panels=panels)
            return 0

        panels = get_panels()
        for year in years:
            rows = run_backtest(year, amounts=amounts, panels=panels)
            print_table(rows, year, amounts=amounts)
            saved = save_backtest_result(
                year,
                rows,
                amounts=amounts,
                panels=panels,
                index_codes=args.index_codes,
            )
            print(f"\n回测结果已保存: {saved}")
    except Exception as exc:
        print(f"回测失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
