"""按当前买入/卖出标准回测指定区间内的波段交易收益。"""

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

from backtest_buy_signals import (
    CN_BROAD_BACKTEST_INDICES,
    BacktestPanels,
    _cn_broad_buy_snapshot,
    _ndx_buy_snapshot,
    _spx_buy_snapshot,
)
from cn_broad_signal import evaluate_cn_broad_buy
from config import CYB_INDEX, HSTECH_INDEX, INDICES, NDX_INDEX, PROJECT_DIR, SPX_INDEX
from cyb_signal import evaluate_cyb_signal
from hstech_signal import evaluate_hstech_signal
from dividend_data import is_buy_signal_row
from market_data import configure_stdout_utf8

BACKTEST_DIR = PROJECT_DIR / "logs" / "backtest"
DEFAULT_START = "2021-01-01"
DEFAULT_AMOUNT = 300.0


@dataclass
class TradeResult:
    code: str
    name: str
    has_sell: bool
    buy_count: int = 0
    sell_count: int = 0
    total_bought: float = 0.0
    total_sold: float = 0.0
    final_units: float = 0.0
    final_price: float = 0.0
    final_date: object = None
    final_value: float = 0.0
    profit: float = 0.0
    return_pct: float | None = None
    buy_only_value: float = 0.0
    buy_only_profit: float = 0.0
    buy_only_return_pct: float | None = None
    note: str = "日频，每交易日评估"
    buy_dates: list = field(default_factory=list)
    sell_dates: list = field(default_factory=list)


def _row_date(row, date_col="date"):
    if date_col == "date_only":
        return pd.Timestamp(row["date_only"])
    return pd.Timestamp(row[date_col])


def _filter_panel(panel, start_date, end_date, date_col="date"):
    if panel is None or panel.empty:
        return panel.copy() if panel is not None else pd.DataFrame()
    work = panel.copy()
    if date_col == "date_only":
        work["_dt"] = pd.to_datetime(work["date_only"])
    else:
        work["_dt"] = pd.to_datetime(work[date_col])
    mask = work["_dt"] >= pd.Timestamp(start_date)
    if end_date:
        mask &= work["_dt"] <= pd.Timestamp(end_date)
    return work.loc[mask].dropna(subset=["close"])


def _cn_broad_signals(row, index_code):
    ev = evaluate_cn_broad_buy(
        {
            "code": index_code,
            "pe_percentile": row.get("pe_percentile"),
            "pb_percentile": row.get("pb_percentile"),
            "dividend_percentile": row.get("dividend_percentile"),
            "spread_percentile": row.get("spread_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
        }
    )
    return ev["is_buy"], ev.get("is_sell", False)


def _cyb_signals(row):
    ev = evaluate_cyb_signal(
        {
            "pe": row["pe"],
            "pb": row["pb"],
            "pe_percentile": row.get("pe_percentile"),
            "pb_percentile": row.get("pb_percentile"),
            "pct_above_low": row.get("pct_above_low"),
        }
    )
    return ev["is_buy"], ev.get("is_sell", False)


def _hstech_signals(row):
    ev = evaluate_hstech_signal(
        {
            "pe": row["pe"],
            "pe_percentile": row.get("pe_percentile"),
            "dividend_percentile": row.get("dividend_percentile"),
            "pct_above_low": row.get("pct_above_low"),
        }
    )
    return ev["is_buy"], ev.get("is_sell", False)


def simulate_trades(
    panel,
    start_date,
    end_date=None,
    amount=DEFAULT_AMOUNT,
    date_col="date",
    buy_fn=None,
    sell_fn=None,
    has_sell=False,
):
    """按日模拟买入/卖出；has_sell=False 时仅买入持有。"""
    sample = _filter_panel(panel, start_date, end_date, date_col=date_col)
    if sample.empty:
        return None

    latest = sample.iloc[-1]
    latest_price = float(latest["close"])
    latest_date = latest["_dt"]

    units = 0.0
    buy_only_units = 0.0
    total_bought = 0.0
    total_sold = 0.0
    buy_count = 0
    sell_count = 0
    buy_dates = []
    sell_dates = []

    for _, row in sample.iterrows():
        price = float(row["close"])
        day = row["_dt"].strftime("%Y-%m-%d")
        is_buy = buy_fn(row) if buy_fn else False
        is_sell = sell_fn(row) if sell_fn and has_sell else False

        if is_buy:
            units += amount / price
            buy_only_units += amount / price
            total_bought += amount
            buy_count += 1
            buy_dates.append(day)
        elif is_sell and units > 0:
            total_sold += units * price
            units = 0.0
            sell_count += 1
            sell_dates.append(day)

    final_value = total_sold + units * latest_price
    profit = final_value - total_bought
    return_pct = profit / total_bought * 100 if total_bought > 0 else None

    buy_only_value = buy_only_units * latest_price
    buy_only_profit = buy_only_value - total_bought
    buy_only_return_pct = (
        buy_only_profit / total_bought * 100 if total_bought > 0 else None
    )

    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_bought": total_bought,
        "total_sold": total_sold,
        "final_units": units,
        "final_price": latest_price,
        "final_date": latest_date,
        "final_value": final_value,
        "profit": profit,
        "return_pct": return_pct,
        "buy_only_value": buy_only_value,
        "buy_only_profit": buy_only_profit,
        "buy_only_return_pct": buy_only_return_pct,
        "buy_dates": buy_dates,
        "sell_dates": sell_dates,
    }


def backtest_all(
    start_date=DEFAULT_START,
    end_date=None,
    amount=DEFAULT_AMOUNT,
    panels=None,
):
    panels = panels or BacktestPanels()
    results = []

    for item in INDICES:
        panel = panels.dividend_panel(item["code"])
        code = item["code"]
        stats = simulate_trades(
            panel,
            start_date,
            end_date,
            amount=amount,
            buy_fn=lambda r, c=code: is_buy_signal_row(r, c),
            has_sell=False,
        )
        if stats:
            results.append(
                TradeResult(
                    code=code,
                    name=item["name"],
                    has_sell=False,
                    **stats,
                )
            )

    for item in CN_BROAD_BACKTEST_INDICES:
        panel = panels.cn_broad_panel(item["code"])
        code = item["code"]
        stats = simulate_trades(
            panel,
            start_date,
            end_date,
            amount=amount,
            buy_fn=lambda r, c=code: _cn_broad_signals(r, c)[0],
            sell_fn=lambda r, c=code: _cn_broad_signals(r, c)[1],
            has_sell=True,
        )
        if stats:
            results.append(
                TradeResult(
                    code=code,
                    name=item["name"],
                    has_sell=True,
                    **stats,
                )
            )

    cyb_panel = panels.cyb_panel()
    stats = simulate_trades(
        cyb_panel,
        start_date,
        end_date,
        amount=amount,
        buy_fn=lambda r: _cyb_signals(r)[0],
        sell_fn=lambda r: _cyb_signals(r)[1],
        has_sell=True,
    )
    if stats:
        results.append(
            TradeResult(
                code=CYB_INDEX["code"],
                name=CYB_INDEX["name"],
                has_sell=True,
                **stats,
            )
        )

    hstech_panel = panels.hstech_panel()
    stats = simulate_trades(
        hstech_panel,
        start_date,
        end_date,
        amount=amount,
        buy_fn=lambda r: _hstech_signals(r)[0],
        sell_fn=lambda r: _hstech_signals(r)[1],
        has_sell=True,
    )
    if stats:
        results.append(
            TradeResult(
                code=HSTECH_INDEX["code"],
                name=HSTECH_INDEX["name"],
                has_sell=True,
                **stats,
            )
        )

    ndx_daily, ndx_growth = panels.ndx_panel()
    stats = simulate_trades(
        ndx_daily,
        start_date,
        end_date,
        amount=amount,
        buy_fn=lambda r: _ndx_buy_snapshot(r, ndx_growth),
        has_sell=False,
    )
    if stats:
        results.append(
            TradeResult(
                code=NDX_INDEX["code"],
                name=NDX_INDEX["name"],
                has_sell=False,
                note="日频（10Y日更，Forward PE按月对齐）",
                **stats,
            )
        )

    spx_daily, spx_growth = panels.spx_panel()
    stats = simulate_trades(
        spx_daily,
        start_date,
        end_date,
        amount=amount,
        buy_fn=lambda r: _spx_buy_snapshot(r, spx_growth),
        has_sell=False,
    )
    if stats:
        results.append(
            TradeResult(
                code=SPX_INDEX["code"],
                name=SPX_INDEX["name"],
                has_sell=False,
                note="日频（10Y日更，Forward PE按季对齐）",
                **stats,
            )
        )

    return results


def _md_money(value):
    if value is None:
        return "—"
    return f"{value:,.0f}"


def _md_pct(value):
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def format_markdown(results, start_date, end_date, amount):
    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    val_dates = [r.final_date for r in results if r.final_date is not None]
    val_date = max(val_dates).strftime("%Y-%m-%d") if val_dates else "—"
    end_label = end_date or "最新"

    lines = [
        f"# {start_date} 至 {end_label} 买卖信号回测",
        "",
        f"> 生成时间：{generated_at}  ",
        "> 买入/卖出标准：当前 config 阈值  ",
        f"> 每次买入金额：{amount:.0f} 元  ",
        "> 卖出规则：触发卖点时清仓（买入优先于卖出）  ",
        "> 红利/美股：仅买入持有，不设卖点",
        "",
        f"估值截至 **{val_date}**。未计手续费、分红再投资；纳指为美元计价。",
        "",
        "## 交易统计",
        "",
        "| 指数 | 代码 | 策略 | 买入次 | 卖出次 | 总投入 | 备注 |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for r in results:
        strategy = "买卖波段" if r.has_sell else "仅买入"
        lines.append(
            f"| {r.name} | {r.code} | {strategy} | {r.buy_count} | "
            f"{r.sell_count} | {_md_money(r.total_bought)} | {r.note} |"
        )

    lines.extend([
        "",
        "## 收益对比（买卖波段 vs 仅买入持有）",
        "",
        "| 指数 | 代码 | 买卖市值 | 买卖盈亏 | 买卖收益率 | "
        "仅买市值 | 仅买盈亏 | 仅买收益率 | 备注 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for r in results:
        if r.has_sell:
            trade_val = r.final_value
            trade_profit = r.profit
            trade_ret = r.return_pct
        else:
            trade_val = r.buy_only_value
            trade_profit = r.buy_only_profit
            trade_ret = r.buy_only_return_pct
        lines.append(
            f"| {r.name} | {r.code} | {_md_money(trade_val)} | "
            f"{trade_profit:+.0f} | {_md_pct(trade_ret)} | "
            f"{_md_money(r.buy_only_value)} | {r.buy_only_profit:+.0f} | "
            f"{_md_pct(r.buy_only_return_pct)} | {r.note} |"
        )

    lines.extend([
        "",
        "**说明**",
        "",
        "- **买卖波段**：有卖点的指数按指标买入、触发卖点时全部卖出；无卖点指数等同仅买入。",
        "- **仅买入持有**：同期所有买入信号均执行，不因卖点卖出（对照组）。",
        "- 有卖点指数的「买卖收益率」与「仅买收益率」差异反映卖点策略效果。",
        "",
        "## 买卖日期",
        "",
    ])

    for r in results:
        lines.append(f"### {r.name} ({r.code})")
        lines.append("")
        lines.append(f"买入 {r.buy_count} 次：")
        lines.append("")
        lines.append(", ".join(r.buy_dates) if r.buy_dates else "—")
        lines.append("")
        if r.has_sell:
            lines.append(f"卖出 {r.sell_count} 次：")
            lines.append("")
            lines.append(", ".join(r.sell_dates) if r.sell_dates else "—")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def print_table(results, start_date, end_date, amount):
    end_label = end_date or "最新"
    val_dates = [r.final_date for r in results if r.final_date is not None]
    val_date = max(val_dates).strftime("%Y-%m-%d") if val_dates else "—"
    print(
        f"\n=== {start_date} 至 {end_label} 买卖信号回测 "
        f"(每次买入 {amount:.0f} 元，截至 {val_date}) ==="
    )
    print(
        f"{'指数':<14} {'代码':<8} {'策略':<8} {'买入':>5} {'卖出':>5} "
        f"{'投入':>8} {'买卖收益':>9} {'仅买收益':>9}"
    )
    print("-" * 78)
    for r in results:
        strategy = "买卖" if r.has_sell else "仅买"
        trade_ret = r.return_pct if r.has_sell else r.buy_only_return_pct
        buy_ret = r.buy_only_return_pct
        trade_text = f"{trade_ret:+.1f}%" if trade_ret is not None else "   —"
        buy_text = f"{buy_ret:+.1f}%" if buy_ret is not None else "   —"
        print(
            f"{r.name:<14} {r.code:<8} {strategy:<8} "
            f"{r.buy_count:>5} {r.sell_count:>5} "
            f"{r.total_bought:>8.0f} {trade_text:>9} {buy_text:>9}"
        )
    print("-" * 78)


def save_result(markdown, filename="trade_2021_present.md"):
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKTEST_DIR / filename
    path.write_text(markdown, encoding="utf-8")
    return path


def main(argv=None):
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="回测指定区间内的买入/卖出信号交易收益"
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_START,
        help=f"起始日期（默认 {DEFAULT_START}）",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="结束日期（默认至最新数据）",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=DEFAULT_AMOUNT,
        help=f"每次买入金额（元，默认 {DEFAULT_AMOUNT:.0f}）",
    )
    parser.add_argument(
        "--output",
        default="trade_2021_present.md",
        help="输出文件名（保存在 logs/backtest/）",
    )
    args = parser.parse_args(argv)

    try:
        print(
            f"正在回测 {args.start} 至 {args.end or '最新'} "
            f"（买入/卖出按当前 config 阈值）..."
        )
        panels = BacktestPanels()
        results = backtest_all(
            start_date=args.start,
            end_date=args.end,
            amount=args.amount,
            panels=panels,
        )
        print_table(results, args.start, args.end, args.amount)
        markdown = format_markdown(
            results, args.start, args.end, args.amount
        )
        path = save_result(markdown, filename=args.output)
        print(f"\n回测结果已保存: {path}")
    except Exception as exc:
        print(f"回测失败: {exc}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
