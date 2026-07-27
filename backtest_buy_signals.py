"""按当前买入标准回测指定年份各指数/模块的买入天数与定投收益。"""

import argparse
import sys

import pandas as pd

from a500_data import build_a500_valuation_history
from cn_broad_data import (
    attach_cn_broad_percentiles,
    build_cn_broad_valuation_history,
)
from cn_broad_signal import evaluate_cn_broad_buy
from config import (
    A500_INDEX,
    A500_MARKET_DATA_START,
    CYB_BUY_LOW_LOOKBACK_DAYS,
    CYB_INDEX,
    DIVIDEND_SIGNAL_HISTORY_START,
    HS300_INDEX,
    INDICES,
    KC50_INDEX,
    NDX_INDEX,
    PROJECT_DIR,
    SPX_INDEX,
    ZZ1000_INDEX,
    ZZ500_INDEX,
)
from cyb_data import attach_percentiles as attach_cyb_percentiles
from cyb_data import build_cyb_valuation_panel, fetch_cyb_price_history
from cyb_signal import evaluate_cyb_signal
from dividend_data import build_signal_history, is_buy_signal_row
from market_data import configure_stdout_utf8, get_gov_bond_yield_history
from ndx_data import build_ndx_daily_valuation_panel, build_ndx_valuation_panel
from ndx_data import compute_historical_earnings_growth
from ndx_signal import is_ndx_buy, resolve_ndx_expected_growth
from spx_data import build_spx_daily_valuation_panel, build_spx_valuation_panel
from spx_data import compute_historical_earnings_growth as compute_spx_historical_growth
from spx_signal import is_spx_buy, resolve_spx_expected_growth
from price_position import attach_pct_above_low

CN_BROAD_BACKTEST_INDICES = [
    A500_INDEX,
    HS300_INDEX,
    ZZ500_INDEX,
    ZZ1000_INDEX,
    KC50_INDEX,
]


class BacktestPanels:
    """一次拉取全部估值面板，供多年份回测/列日期复用。"""

    def __init__(self):
        self.bond_history = get_gov_bond_yield_history()
        self._dividend = {}
        self._a500 = None
        self._cn_broad = {}
        self._cyb = None
        self._ndx = None
        self._ndx_growth = None
        self._spx = None
        self._spx_growth = None

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

    def a500_panel(self):
        return self.cn_broad_panel(A500_INDEX["code"])

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
        return self._cyb

    def ndx_panel(self):
        if self._ndx is None:
            daily, _ = build_ndx_daily_valuation_panel()
            self._ndx = daily
            month_panel, _ = build_ndx_valuation_panel()
            self._ndx_growth = compute_historical_earnings_growth(month_panel)
        return self._ndx, self._ndx_growth

    def spx_panel(self):
        if self._spx is None:
            daily, _ = build_spx_daily_valuation_panel()
            self._spx = daily
            month_panel, _ = build_spx_valuation_panel()
            self._spx_growth = compute_spx_historical_growth(month_panel)
        return self._spx, self._spx_growth


_PANELS = None
BACKTEST_DIR = PROJECT_DIR / "logs" / "backtest"


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
    panel, year, buy_fn, amount=300.0, date_col="date", price_col="close"
):
    """每个买入日投入固定金额，按最新收盘价估算持仓市值与收益率。"""
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

    priced = work.dropna(subset=[price_col])
    if priced.empty:
        return None

    latest = priced.iloc[-1]
    latest_price = float(latest[price_col])
    latest_date = latest["_dt"]

    sample = priced[priced["_dt"].dt.year == year]
    if sample.empty:
        return None

    buy_prices = []
    for _, row in sample.iterrows():
        if buy_fn(row):
            buy_prices.append(float(row[price_col]))

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
        }
    )["is_buy"]


def _ndx_buy_snapshot(row, historical_growth=None):
    snapshot = {
        "forward_pe": row.get("forward_pe"),
        "trailing_pe": row.get("trailing_pe"),
        "forward_pe_percentile": row.get("forward_pe_percentile"),
        "trailing_pe_percentile": row.get("trailing_pe_percentile"),
        "us10y_percentile": row.get("us10y_percentile"),
        "implied_growth": row.get("implied_growth"),
        "historical_growth": historical_growth,
        "pct_above_low": row.get("pct_above_low"),
    }
    snapshot["expected_growth"] = resolve_ndx_expected_growth(snapshot)
    return is_ndx_buy(snapshot)


def _spx_buy_snapshot(row, historical_growth=None):
    snapshot = {
        "forward_pe": row.get("forward_pe"),
        "trailing_pe": row.get("trailing_pe"),
        "forward_pe_percentile": row.get("forward_pe_percentile"),
        "trailing_pe_percentile": row.get("trailing_pe_percentile"),
        "us10y_percentile": row.get("us10y_percentile"),
        "implied_growth": row.get("implied_growth"),
        "historical_growth": historical_growth,
        "pct_above_low": row.get("pct_above_low"),
    }
    snapshot["expected_growth"] = resolve_spx_expected_growth(snapshot)
    return is_spx_buy(snapshot)


def backtest_dividend(year, bond_history=None, amount=None, panels=None):
    panels = panels or get_panels()
    rows = []
    for item in INDICES:
        panel = panels.dividend_panel(item["code"])
        buy_fn = lambda r, code=item["code"]: is_buy_signal_row(r, code)  # noqa: E731
        if amount is not None:
            result = _simulate_dca_returns(panel, year, buy_fn, amount=amount)
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


def backtest_a500(year, bond_history=None, amount=None, panels=None):
    return backtest_cn_broad(year, A500_INDEX, amount=amount, panels=panels)


def backtest_hs300(year, amount=None, panels=None):
    return backtest_cn_broad(year, HS300_INDEX, amount=amount, panels=panels)


def backtest_zz1000(year, amount=None, panels=None):
    return backtest_cn_broad(year, ZZ1000_INDEX, amount=amount, panels=panels)


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


def backtest_ndx(year, amount=None, panels=None):
    """纳指日频信号（10Y 日更，Forward PE 按月对齐，TTM PE 有数据日起日更）。"""
    panels = panels or get_panels()
    daily, historical_growth = panels.ndx_panel()

    buy_fn = lambda r: _ndx_buy_snapshot(r, historical_growth)  # noqa: E731
    if amount is not None:
        result = _simulate_dca_returns(
            daily, year, buy_fn, amount=amount, date_col="date"
        )
    else:
        result = _count_buy_days(daily, year, buy_fn, date_col="date")
    if not result:
        return []
    result["note"] = "日频（10Y日更，Forward PE按月对齐）"
    return [{"code": NDX_INDEX["code"], "name": NDX_INDEX["name"], **result}]


def backtest_spx(year, amount=None, panels=None):
    panels = panels or get_panels()
    daily, historical_growth = panels.spx_panel()

    buy_fn = lambda r: _spx_buy_snapshot(r, historical_growth)  # noqa: E731
    if amount is not None:
        result = _simulate_dca_returns(
            daily, year, buy_fn, amount=amount, date_col="date"
        )
    else:
        result = _count_buy_days(daily, year, buy_fn, date_col="date")
    if not result:
        return []
    result["note"] = "日频（10Y日更，Forward PE按季对齐）"
    return [{"code": SPX_INDEX["code"], "name": SPX_INDEX["name"], **result}]


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


def list_buy_dates(year, panels=None):
    """列出指定年份各指数买入日期（与 report 共用判定逻辑）。"""
    panels = panels or get_panels()
    out = {}

    for item in INDICES:
        panel = panels.dividend_panel(item["code"])
        code = item["code"]
        out[f"{item['name']} ({code})"] = _collect_buy_dates(
            panel,
            year,
            lambda r, c=code: is_buy_signal_row(r, c),
        )

    for item in CN_BROAD_BACKTEST_INDICES:
        panel = panels.cn_broad_panel(item["code"])
        out[f"{item['name']} ({item['code']})"] = _collect_buy_dates(
            panel,
            year,
            lambda r, c=item["code"]: _cn_broad_buy_snapshot(r, c),
        )

    cyb = panels.cyb_panel()
    out[f"{CYB_INDEX['name']} ({CYB_INDEX['code']})"] = _collect_buy_dates(
        cyb,
        year,
        lambda r: evaluate_cyb_signal(
            {
                "pe": r["pe"],
                "pb": r["pb"],
                "pe_percentile": r.get("pe_percentile"),
                "pb_percentile": r.get("pb_percentile"),
                "pct_above_low": r.get("pct_above_low"),
            }
        )["is_buy"],
        date_col="date",
    )

    daily, hg = panels.ndx_panel()
    out[f"{NDX_INDEX['name']} ({NDX_INDEX['code']})"] = _collect_buy_dates(
        daily,
        year,
        lambda r: _ndx_buy_snapshot(r, hg),
        date_col="date",
    )

    spx_daily, spx_growth = panels.spx_panel()
    out[f"{SPX_INDEX['name']} ({SPX_INDEX['code']})"] = _collect_buy_dates(
        spx_daily,
        year,
        lambda r: _spx_buy_snapshot(r, spx_growth),
        date_col="date",
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


def run_backtest(year, amount=None, panels=None):
    print(f"正在回测 {year} 年买入信号（使用当前 config 阈值）...")
    panels = panels or get_panels()

    rows = []
    rows.extend(backtest_dividend(year, panels=panels, amount=amount))
    for item in CN_BROAD_BACKTEST_INDICES:
        rows.extend(backtest_cn_broad(year, item, panels=panels, amount=amount))
    rows.extend(backtest_cyb(year, panels=panels, amount=amount))
    rows.extend(backtest_ndx(year, panels=panels, amount=amount))
    rows.extend(backtest_spx(year, panels=panels, amount=amount))
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


def _format_backtest_markdown(year, rows, amount=None, buy_dates=None):
    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {year} 年买入信号回测",
        "",
        f"> 生成时间：{generated_at}  ",
        "> 买入标准：当前 config 阈值  ",
    ]
    if amount is not None:
        lines.append(f"> 每次买入金额：{amount:.0f} 元")
    else:
        lines.append("> 每次买入金额：仅统计次数")
    lines.append("")

    lines.extend(["## 买入信号统计", ""])
    lines.append("| 指数 | 代码 | 买入次数 | 样本数 | 占比 | 备注 |")
    lines.append("| --- | --- | ---: | ---: | ---: | --- |")
    for row in rows:
        note = row.get("note", "日频，每交易日评估")
        lines.append(
            f"| {row['name']} | {row['code']} | {row['buy_days']} | "
            f"{row['total_days']} | {row['buy_pct']:.1f}% | {note} |"
        )
    lines.extend([
        "",
        "各指数均按交易日/信号日计次；纳指/标普 10Y 利率与价格日更，"
        "Forward PE 按月/按季发布并对齐到每个交易日。",
        f"中证A500（000510）与中证500（000905）为不同指数；"
        f"中证A500 行情自 {A500_MARKET_DATA_START[:7]} 起，更早年份样本数为 0。",
        "",
        "## 指数价格与买入均价（收盘价）",
        "",
        "| 指数 | 代码 | 年内最高 | 年内最低 | 买入均价 | 备注 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ])
    for row in rows:
        note = row.get("note", "日频，每交易日评估")
        lines.append(
            f"| {row['name']} | {row['code']} | "
            f"{_md_price(row.get('year_high'))} | {_md_price(row.get('year_low'))} | "
            f"{_md_price(row.get('avg_buy_price'))} | {note} |"
        )
    lines.extend([
        "",
        "年内最高/最低取该年交易日收盘价极值；"
        "买入均价为各买入信号日收盘价的算术平均（无买入时显示 —）；纳指为美元计价。",
    ])

    if amount is not None:
        latest_dates = [
            row.get("latest_date") for row in rows if row.get("latest_date")
        ]
        val_date = max(latest_dates) if latest_dates else None
        val_text = (
            pd.Timestamp(val_date).strftime("%Y-%m-%d")
            if val_date is not None
            else "—"
        )
        lines.extend([
            "",
            f"## 信号按次买入收益（每次 {amount:.0f} 元，估值截至 {val_text}）",
            "",
            "| 指数 | 代码 | 买入次 | 投入 | 市值 | 盈亏 | 收益率 | 备注 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ])
        for row in rows:
            note = row.get("note", "日频，每交易日评估")
            ret = row.get("return_pct")
            ret_text = f"{ret:.1f}%" if ret is not None else "—"
            invested = row.get("invested", 0)
            market_value = row.get("market_value", 0)
            profit = row.get("profit", 0)
            lines.append(
                f"| {row['name']} | {row['code']} | {row['buy_days']} | "
                f"{invested:.0f} | {market_value:.0f} | {profit:+.0f} | "
                f"{ret_text} | {note} |"
            )
        lines.extend([
            "",
            "每个买入信号触发时买入固定金额，持仓按最新收盘价市值估算；"
            "未计手续费、分红再投资；纳指为美元计价指数点位。",
        ])

    if buy_dates:
        lines.extend(["", "## 买入日期", ""])
        for name, days in buy_dates.items():
            lines.append(f"### {name}（{len(days)} 天）")
            lines.append("")
            lines.append(", ".join(days) if days else "—")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_backtest_result(year, rows, amount=None, buy_dates=None):
    """保存回测结果到本地 Markdown，每年一份，重新运行会覆盖。"""
    path = _backtest_result_path(year)
    path.write_text(
        _format_backtest_markdown(year, rows, amount=amount, buy_dates=buy_dates),
        encoding="utf-8",
    )
    return path


def _format_price(value):
    if value is None:
        return "     —"
    if value >= 1000:
        return f"{value:>10,.2f}"
    return f"{value:>10.2f}"


def print_price_stats_table(rows, year):
    if not rows:
        return

    print(f"\n=== {year} 年指数价格与买入均价（收盘价） ===")
    print(
        f"{'指数':<16} {'代码':<10} {'年内最高':>12} {'年内最低':>12} {'买入均价':>12}  备注"
    )
    print("-" * 80)
    for row in rows:
        note = row.get("note", "日频，每交易日评估")
        print(
            f"{row['name']:<16} {row['code']:<10} "
            f"{_format_price(row.get('year_high'))} "
            f"{_format_price(row.get('year_low'))} "
            f"{_format_price(row.get('avg_buy_price'))}  {note}"
        )
    print("-" * 80)
    print(
        "说明: 年内最高/最低取该年交易日收盘价极值；"
        "买入均价为各买入信号日收盘价的算术平均（无买入时显示 —）；纳指为美元计价。"
    )


def print_returns_table(rows, year, amount):
    if not rows:
        print("无有效回测结果")
        return

    latest_dates = [row.get("latest_date") for row in rows if row.get("latest_date")]
    val_date = max(latest_dates) if latest_dates else None
    val_text = pd.Timestamp(val_date).strftime("%Y-%m-%d") if val_date is not None else "—"
    print(
        f"\n=== {year} 年信号按次买入收益（每次信号 {amount:.0f} 元，估值截至 {val_text}） ==="
    )
    print(
        f"{'指数':<16} {'代码':<10} {'买入次':>6} {'投入':>8} "
        f"{'市值':>8} {'盈亏':>8} {'收益率':>8}  备注"
    )
    print("-" * 88)
    for row in rows:
        note = row.get("note", "日频，每交易日评估")
        ret = row.get("return_pct")
        ret_text = f"{ret:>7.1f}%" if ret is not None else "     —"
        invested = row.get("invested", 0)
        market_value = row.get("market_value", 0)
        profit = row.get("profit", 0)
        print(
            f"{row['name']:<16} {row['code']:<10} "
            f"{row['buy_days']:>6} {invested:>8.0f} "
            f"{market_value:>8.0f} {profit:>+8.0f} {ret_text}  {note}"
        )
    print("-" * 88)
    print(
        "说明: 每个买入信号触发时买入固定金额，持仓按最新收盘价市值估算；"
        "未计手续费、分红再投资；纳指为美元计价指数点位。"
    )


def print_table(rows, year, amount=None):
    if not rows:
        print("无有效回测结果")
        return

    print(f"\n=== {year} 年买入信号回测（当前买入标准） ===")
    print(
        f"{'指数':<16} {'代码':<10} {'买入次数':>8} {'样本数':>8} "
        f"{'占比':>8}  备注"
    )
    print("-" * 80)
    for row in rows:
        note = row.get("note", "日频，每交易日评估")
        print(
            f"{row['name']:<16} {row['code']:<10} "
            f"{row['buy_days']:>8} {row['total_days']:>8} "
            f"{row['buy_pct']:>7.1f}%  {note}"
        )
    print("-" * 80)
    print(
        "说明: 各指数均按交易日/信号日计次；纳指 10Y 利率与价格日更，"
        "Forward PE 按月发布并对齐到每个交易日。"
    )
    print_price_stats_table(rows, year)
    if amount is not None:
        print_returns_table(rows, year, amount)


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
        "--amount",
        type=float,
        default=300.0,
        help="每个买入信号投入金额（元，默认 300；设为 0 则只统计次数）",
    )
    args = parser.parse_args(argv)
    years = args.year or [2025]
    amount = args.amount if args.amount > 0 else None

    try:
        if args.list_dates:
            print_buy_dates(years)
            return 0

        panels = get_panels()
        for year in years:
            rows = run_backtest(year, amount=amount, panels=panels)
            print_table(rows, year, amount=amount)
            buy_dates = list_buy_dates(year, panels=panels)
            saved = save_backtest_result(
                year, rows, amount=amount, buy_dates=buy_dates
            )
            print(f"\n回测结果已保存: {saved}")
    except Exception as exc:
        print(f"回测失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
