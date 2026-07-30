"""按当前买入/卖出标准回测指定区间内的波段交易收益。"""

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

from backtest_metrics import compute_strategy_metrics
from backtest_buy_signals import (
    BACKTEST_RETURN_FOOTNOTE,
    CN_BROAD_BACKTEST_INDICES,
    BacktestPanels,
    US_INDEX_META,
    US_INDEX_NOTES,
    _cn_broad_buy_snapshot,
    _us_buy_snapshot,
)
from cn_broad_signal import evaluate_cn_broad_buy
from config import (
    BACKTEST_RISK_FREE_RATE,
    CYB_BUY_MAX_ABOVE_LOW_PCT,
    CYB_BUY_MAX_YEAR_RANGE_PCT,
    CYB_BUY_PB_PERCENTILE_MAX,
    CYB_BUY_PE_PERCENTILE_MAX,
    CYB_BUY_PEG_HIST_MAX,
    CYB_INDEX,
    CYB_SELL_ENABLED,
    cn_broad_sell_enabled,
    format_backtest_amount_note,
    get_backtest_buy_amount,
    get_cn_broad_signal_config,
    HSTECH_INDEX,
    HSTECH_SELL_ENABLED,
    INDICES,
    BACKTEST_OUTPUT_DIR,
    PORTFOLIO_EXCLUDED_CODES,
    PORTFOLIO_GROUP_WEIGHTS,
    PORTFOLIO_INDEX_GROUPS,
    PORTFOLIO_TOTAL_BUDGET,
    PROJECT_DIR,
    resolve_backtest_amounts,
    US_INDEX_KEYS,
)
from buy_amount_config import resolve_simulate_amount
from cyb_signal import evaluate_cyb_signal
from hstech_signal import evaluate_hstech_signal
from dividend_data import is_buy_signal_row
from market_data import configure_stdout_utf8

BACKTEST_DIR = BACKTEST_OUTPUT_DIR
DEFAULT_START = "2015-01-01"


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
    sharpe_ratio: float | None = None
    benchmark_sharpe: float | None = None
    max_drawdown_pct: float | None = None
    annualized_return_pct: float | None = None
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
            "year_range_position": row.get("year_range_position"),
            "ma_slope_pct": row.get("ma_slope_pct"),
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
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
            "ma_slope_pct": row.get("ma_slope_pct"),
        }
    )
    return ev["is_buy"], ev.get("is_sell", False)


def _hstech_row_snapshot(row):
    return {
        "pe": row["pe"],
        "pe_percentile": row.get("pe_percentile"),
        "dividend_percentile": row.get("dividend_percentile"),
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
        "close": row.get("close"),
    }


def _hstech_signals(row):
    ev = evaluate_hstech_signal(_hstech_row_snapshot(row))
    return ev["is_buy"], ev.get("is_sell", False)


def _attach_risk_metrics(
    panel,
    start_date,
    end_date,
    amount,
    buy_fn,
    sell_fn,
    has_sell,
    total_return_pct,
    date_col="date",
    valuation_price_col=None,
):
    """在已有收益结果上附加夏普、最大回撤等指标。"""
    val_col = valuation_price_col or "close"
    sample = _filter_panel(panel, start_date, end_date, date_col=date_col)
    if sample.empty or val_col not in sample.columns:
        val_col = "close"
    if sample.empty:
        return {}
    return compute_strategy_metrics(
        sample,
        amount,
        val_col,
        buy_fn,
        sell_fn=sell_fn,
        has_sell=has_sell,
        total_return_pct=total_return_pct,
    )


def _resolve_buy_amount(amount, row):
    if callable(amount):
        return float(amount(row))
    return float(amount)


def simulate_trades(
    panel,
    start_date,
    end_date=None,
    amount=100.0,
    date_col="date",
    buy_fn=None,
    sell_fn=None,
    has_sell=False,
    valuation_price_col=None,
):
    """按日模拟买入/卖出；has_sell=False 时仅买入持有。

    amount 可为固定金额（float），或 amount_fn(row) 按行计算分档金额。
    """
    val_col = valuation_price_col or "close"
    sample = _filter_panel(panel, start_date, end_date, date_col=date_col)
    if sample.empty:
        return None

    if val_col not in sample.columns:
        val_col = "close"

    latest = sample.iloc[-1]
    latest_price = float(latest[val_col])
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
        price = float(row[val_col])
        day = row["_dt"].strftime("%Y-%m-%d")
        is_buy = buy_fn(row) if buy_fn else False
        is_sell = sell_fn(row) if sell_fn and has_sell else False

        buy_amount = _resolve_buy_amount(amount, row) if is_buy else 0.0
        if is_buy and buy_amount > 0:
            units += buy_amount / price
            buy_only_units += buy_amount / price
            total_bought += buy_amount
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


def _resolve_trade_amount(
    code,
    base_amt,
    amounts,
    panel,
    start_date,
    end_date,
    buy_fn,
    date_col="date",
):
    return resolve_simulate_amount(
        code, base_amt, amounts, panel, start_date, end_date, buy_fn, date_col
    )


def backtest_all(
    start_date=DEFAULT_START,
    end_date=None,
    amounts=None,
    panels=None,
):
    if amounts is None:
        amounts = resolve_backtest_amounts()
    panels = panels or BacktestPanels()
    results = []

    for item in INDICES:
        panel = panels.dividend_panel(item["code"])
        code = item["code"]
        amt = get_backtest_buy_amount(code, amounts)
        if amt <= 0:
            continue
        buy_fn = lambda r, c=code: is_buy_signal_row(r, c)
        sim_amt = _resolve_trade_amount(
            code, amt, amounts, panel, start_date, end_date, buy_fn
        )
        stats = simulate_trades(
            panel,
            start_date,
            end_date,
            amount=sim_amt,
            buy_fn=buy_fn,
            has_sell=False,
            valuation_price_col="total_return_close",
        )
        if stats:
            ret = stats.get("buy_only_return_pct")
            metrics = _attach_risk_metrics(
                panel,
                start_date,
                end_date,
                amt,
                buy_fn,
                None,
                False,
                ret,
                valuation_price_col="total_return_close",
            )
            metrics.pop("trading_days", None)
            stats.update(metrics)
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
        amt = get_backtest_buy_amount(code, amounts)
        if amt <= 0:
            continue
        sell_on = cn_broad_sell_enabled(code)
        buy_fn = lambda r, c=code: _cn_broad_signals(r, c)[0]
        sell_fn = lambda r, c=code: _cn_broad_signals(r, c)[1]
        sim_amt = _resolve_trade_amount(
            code, amt, amounts, panel, start_date, end_date, buy_fn
        )
        stats = simulate_trades(
            panel,
            start_date,
            end_date,
            amount=sim_amt,
            buy_fn=buy_fn,
            sell_fn=sell_fn,
            has_sell=sell_on,
        )
        if stats:
            ret = stats.get("return_pct") if sell_on else stats.get("buy_only_return_pct")
            metrics = _attach_risk_metrics(
                panel,
                start_date,
                end_date,
                amt,
                buy_fn,
                sell_fn,
                sell_on,
                ret,
            )
            metrics.pop("trading_days", None)
            stats.update(metrics)
            results.append(
                TradeResult(
                    code=code,
                    name=item["name"],
                    has_sell=sell_on,
                    **stats,
                )
            )

    cyb_panel = panels.cyb_panel()
    cyb_code = CYB_INDEX["code"]
    cyb_amt = get_backtest_buy_amount(cyb_code, amounts)
    cyb_buy = lambda r: _cyb_signals(r)[0]
    cyb_sell = lambda r: _cyb_signals(r)[1]
    if cyb_amt > 0:
        sim_amt = _resolve_trade_amount(
            cyb_code, cyb_amt, amounts, cyb_panel, start_date, end_date, cyb_buy
        )
        stats = simulate_trades(
            cyb_panel,
            start_date,
            end_date,
            amount=sim_amt,
            buy_fn=cyb_buy,
            sell_fn=cyb_sell,
            has_sell=CYB_SELL_ENABLED,
        )
        if stats:
            ret = stats.get("buy_only_return_pct")
            metrics = _attach_risk_metrics(
                cyb_panel,
                start_date,
                end_date,
                cyb_amt,
                cyb_buy,
                cyb_sell,
                CYB_SELL_ENABLED,
                ret,
            )
            metrics.pop("trading_days", None)
            stats.update(metrics)
            results.append(
                TradeResult(
                    code=CYB_INDEX["code"],
                    name=CYB_INDEX["name"],
                    has_sell=CYB_SELL_ENABLED,
                    **stats,
                )
            )

    hstech_panel = panels.hstech_panel()
    hs_code = HSTECH_INDEX["code"]
    hs_amt = get_backtest_buy_amount(hs_code, amounts)
    if hs_amt > 0:
        hs_buy = lambda r: _hstech_signals(r)[0]
        hs_sell = lambda r: _hstech_signals(r)[1]
        sim_amt = _resolve_trade_amount(
            hs_code, hs_amt, amounts, hstech_panel, start_date, end_date, hs_buy
        )
        stats = simulate_trades(
            hstech_panel,
            start_date,
            end_date,
            amount=sim_amt,
            buy_fn=hs_buy,
            sell_fn=hs_sell,
            has_sell=HSTECH_SELL_ENABLED,
            date_col="date",
        )
        if stats:
            ret = stats.get("buy_only_return_pct")
            metrics = _attach_risk_metrics(
                hstech_panel,
                start_date,
                end_date,
                hs_amt,
                hs_buy,
                hs_sell,
                HSTECH_SELL_ENABLED,
                ret,
                date_col="date",
            )
            metrics.pop("trading_days", None)
            stats.update(metrics)
            results.append(
                TradeResult(
                    code=HSTECH_INDEX["code"],
                    name=HSTECH_INDEX["name"],
                    has_sell=HSTECH_SELL_ENABLED,
                    **stats,
                )
            )

    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        meta = US_INDEX_META[key]
        us_amt = get_backtest_buy_amount(meta["code"], amounts)
        if us_amt <= 0:
            continue
        us_buy = lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g)
        sim_amt = _resolve_trade_amount(
            meta["code"], us_amt, amounts, daily, start_date, end_date, us_buy
        )
        stats = simulate_trades(
            daily,
            start_date,
            end_date,
            amount=sim_amt,
            buy_fn=us_buy,
            has_sell=False,
        )
        if stats:
            ret = stats.get("buy_only_return_pct")
            metrics = _attach_risk_metrics(
                daily,
                start_date,
                end_date,
                us_amt,
                us_buy,
                None,
                False,
                ret,
            )
            metrics.pop("trading_days", None)
            stats.update(metrics)
            results.append(
                TradeResult(
                    code=meta["code"],
                    name=meta["name"],
                    has_sell=False,
                    note=US_INDEX_NOTES[key],
                    **stats,
                )
            )

    return results


def _append_sell_column(table, panel, start_date, end_date, sell_fn, date_col="date"):
    if sell_fn is None or table.empty:
        return table
    work = _filter_panel(panel, start_date, end_date, date_col=date_col)
    sell_by_date = {}
    for _, row in work.iterrows():
        if sell_fn(row):
            day = row["_dt"].strftime("%Y-%m-%d")
            sell_by_date[day] = "卖出"
    if not sell_by_date:
        return table
    out = table.copy()
    out["sell"] = out["date"].map(lambda d: sell_by_date.get(d, ""))
    return out


def collect_trade_chart_tables(
    start_date,
    end_date=None,
    amounts=None,
    panels=None,
):
    """收集波段回测各指数图表数据（含买卖标记）。"""
    from backtest_buy_signals import (
        build_daily_table_range,
        CYB_INDEX,
        HSTECH_INDEX,
        INDICES,
    )

    if amounts is None:
        amounts = resolve_backtest_amounts()
    panels = panels or BacktestPanels()
    tables = []

    def _maybe_add(panel, code, name, buy_fn, sell_fn=None, date_col="date", price_col="close"):
        from backtest_buy_signals import BacktestRange, _index_simulate_amount

        if amounts is not None:
            date_range = BacktestRange(
                start=start_date,
                end=end_date,
                label=f"{start_date}_{end_date or 'present'}",
            )
            sim_amt = _index_simulate_amount(
                code, amounts, panel, date_range, buy_fn, date_col
            )
            if sim_amt == 0:
                return
        else:
            base = get_backtest_buy_amount(code, amounts)
            if base <= 0:
                return
            sim_amt = base
        table = build_daily_table_range(
            panel,
            start_date,
            end_date,
            buy_fn,
            date_col=date_col,
            price_col=price_col,
            amount=sim_amt,
        )
        if table.empty:
            return
        table = _append_sell_column(
            table, panel, start_date, end_date, sell_fn, date_col=date_col
        )
        tables.append({"name": name, "code": code, "table": table})

    for item in INDICES:
        code = item["code"]
        panel = panels.dividend_panel(code)
        buy_fn = lambda r, c=code: is_buy_signal_row(r, c)
        _maybe_add(panel, code, item["name"], buy_fn)

    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        panel = panels.cn_broad_panel(code)
        sell_on = cn_broad_sell_enabled(code)
        buy_fn = lambda r, c=code: _cn_broad_signals(r, c)[0]
        sell_fn = (
            (lambda r, c=code: _cn_broad_signals(r, c)[1]) if sell_on else None
        )
        _maybe_add(panel, code, item["name"], buy_fn, sell_fn)

    cyb_panel = panels.cyb_panel()
    cyb_buy = lambda r: _cyb_signals(r)[0]
    cyb_sell = lambda r: _cyb_signals(r)[1] if CYB_SELL_ENABLED else None
    _maybe_add(
        cyb_panel, CYB_INDEX["code"], CYB_INDEX["name"], cyb_buy, cyb_sell
    )

    hstech_panel = panels.hstech_panel()
    hs_buy = lambda r: _hstech_signals(r)[0]
    hs_sell = lambda r: _hstech_signals(r)[1] if HSTECH_SELL_ENABLED else None
    _maybe_add(
        hstech_panel,
        HSTECH_INDEX["code"],
        HSTECH_INDEX["name"],
        hs_buy,
        hs_sell,
        date_col="date",
    )

    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        meta = US_INDEX_META[key]
        us_buy = lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g)
        _maybe_add(daily, meta["code"], meta["name"], us_buy)

    return tables


def _md_money(value):
    if value is None:
        return "—"
    return f"{value:,.0f}"


def _md_pct(value):
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def _md_sharpe(value):
    if value is None:
        return "—"
    return f"{value:.2f}"


def _effective_return(r: TradeResult):
    if r.has_sell:
        return r.return_pct
    return r.buy_only_return_pct


def _trade_totals(results):
    total_bought = sum(r.total_bought for r in results)
    trade_value = 0.0
    trade_profit = 0.0
    buy_only_value = 0.0
    buy_only_profit = 0.0
    for r in results:
        if r.has_sell:
            trade_value += r.final_value
            trade_profit += r.profit
        else:
            trade_value += r.buy_only_value
            trade_profit += r.buy_only_profit
        buy_only_value += r.buy_only_value
        buy_only_profit += r.buy_only_profit
    trade_ret = trade_profit / total_bought * 100 if total_bought > 0 else None
    buy_only_ret = buy_only_profit / total_bought * 100 if total_bought > 0 else None
    return {
        "buy_count": sum(r.buy_count for r in results),
        "sell_count": sum(r.sell_count for r in results),
        "total_bought": total_bought,
        "trade_value": trade_value,
        "trade_profit": trade_profit,
        "trade_ret": trade_ret,
        "buy_only_value": buy_only_value,
        "buy_only_profit": buy_only_profit,
        "buy_only_ret": buy_only_ret,
    }


def _format_config_snapshot():
    """报告内嵌当前买入阈值摘要，便于核对是否与 config 一致。"""
    lines = [
        "## 当前买入阈值（本报告依据）",
        "",
        "| 指数 | 代码 | 利差分位 | PE | PB | 距低点 | 年区间 | 卖出 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in CN_BROAD_BACKTEST_INDICES:
        cfg = get_cn_broad_signal_config(item["code"])
        sell = "科创50卖出" if cn_broad_sell_enabled(item["code"]) else "无"
        lines.append(
            f"| {item['name']} | {item['code']} | "
            f"≥{cfg['buy_spread_percentile_min']:.0f}% | "
            f"≤{cfg['buy_pe_percentile_max']:.0f}% | "
            f"≤{cfg['buy_pb_percentile_max']:.0f}% | "
            f"≤{cfg['buy_max_above_low_pct'] * 100:.0f}% | "
            f"≤{cfg['buy_max_year_range_pct'] * 100:.0f}% | {sell} |"
        )
    lines.extend([
        f"| 创业板指 | {CYB_INDEX['code']} | — | "
        f"≤{CYB_BUY_PE_PERCENTILE_MAX:.0f}% | ≤{CYB_BUY_PB_PERCENTILE_MAX:.0f}% | "
        f"≤{CYB_BUY_MAX_ABOVE_LOW_PCT * 100:.0f}% | "
        f"≤{CYB_BUY_MAX_YEAR_RANGE_PCT * 100:.0f}% | 无 |",
        "",
        f"创业板 PEG(5年) ≤ {CYB_BUY_PEG_HIST_MAX}；完整阈值见 `config.py` / `README.md`。",
        "",
    ])
    return lines


def _format_portfolio_snapshot(amounts):
    if not amounts.get("portfolio"):
        return []
    weights = amounts.get("group_weights") or PORTFOLIO_GROUP_WEIGHTS
    by_code = amounts.get("by_code") or {}
    lines = [
        "## 组合仓位与单次买入金额",
        "",
        f"> 总预算 **{amounts.get('total_budget', PORTFOLIO_TOTAL_BUDGET):,.0f}** 元；"
        f"核心 {weights.get('core', 0):.0%} / 美股 {weights.get('us', 0):.0%} / "
        f"科创50 {weights.get('kc50', 0):.0%} / 卫星 {weights.get('satellite', 0):.0%}；"
        f"沪深300·中证500·恒科不买入",
        "",
        "| 组别 | 指数 | 代码 | 单次买入 |",
        "| --- | --- | --- | ---: |",
    ]
    group_names = {
        "core": "核心（红利+A500）",
        "us": "美股",
        "kc50": "科创50",
        "satellite": "卫星（创业板+1000）",
    }
    name_map = {i["code"]: i["name"] for i in INDICES}
    name_map.update({i["code"]: i["name"] for i in CN_BROAD_BACKTEST_INDICES})
    name_map.update({CYB_INDEX["code"]: CYB_INDEX["name"], HSTECH_INDEX["code"]: HSTECH_INDEX["name"]})
    name_map.update({"NDX": "纳斯达克100", "SPX": "标普500"})
    for group, gname in group_names.items():
        for code, grp in PORTFOLIO_INDEX_GROUPS.items():
            if grp != group:
                continue
            amt = by_code.get(code, 0)
            lines.append(
                f"| {gname} | {name_map.get(code, code)} | {code} | "
                f"{'—' if not amt else f'{amt:.0f}'} |"
            )
    lines.append("")
    return lines


def _format_amount_snapshot(amounts):
    if amounts.get("return_max") and amounts.get("by_code"):
        by_code = amounts.get("by_code") or {}
        tier = amounts.get("tier_scheme")
        lines = [
            "## 买入金额配置（收益最大化）",
            "",
            f"> 总预算 **{amounts.get('total_budget', PORTFOLIO_TOTAL_BUDGET):,.0f}** 元"
            + (f"；分档 **{tier}**" if tier else ""),
            "",
            "| 代码 | 基准单次（元） |",
            "| --- | ---: |",
        ]
        for code in sorted(by_code.keys()):
            amt = by_code.get(code, 0)
            if amt > 0:
                lines.append(f"| {code} | {amt:.0f} |")
        lines.append("")
        return lines
    return _format_portfolio_snapshot(amounts)


def format_markdown(results, start_date, end_date, amounts):
    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    val_dates = [r.final_date for r in results if r.final_date is not None]
    val_date = max(val_dates).strftime("%Y-%m-%d") if val_dates else "—"
    end_label = end_date or "最新"

    lines = [
        f"# {start_date} 至 {end_label} 买卖信号回测",
        "",
        f"> 生成时间：{generated_at}  ",
        "> 买入/卖出标准：当前 config 阈值（与 `backtest_buy_signals.py` 一致）  ",
        f"> 每次买入金额：{format_backtest_amount_note(amounts)}  ",
        "> 卖出规则：仅科创50 触发卖点时清仓；其余模块只买不卖  ",
        "> 红利/美股：仅买入持有，不设卖点",
        "",
        f"估值截至 **{val_date}**。{BACKTEST_RETURN_FOOTNOTE}",
        "",
        f"> **与按年回测的关系**：本报告为 **{start_date} 至 {end_label} 连续区间** 的买卖模拟；"
        "各年 `YYYY.md` 为 **按自然年切片** 的买入统计与定投收益。"
        "同一指数的「买入次数」应等于各年买入次数之和（区间相同、阈值相同）。",
        "",
        *_format_config_snapshot(),
        *_format_amount_snapshot(amounts),
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
    totals = _trade_totals(results)
    lines.append(
        f"| **合计** | — | — | {totals['buy_count']} | "
        f"{totals['sell_count']} | {_md_money(totals['total_bought'])} | "
        f"总收益 {totals['trade_profit']:+.0f} |"
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
    totals = _trade_totals(results)
    lines.append(
        f"| **合计** | — | {_md_money(totals['trade_value'])} | "
        f"{totals['trade_profit']:+.0f} | {_md_pct(totals['trade_ret'])} | "
        f"{_md_money(totals['buy_only_value'])} | {totals['buy_only_profit']:+.0f} | "
        f"{_md_pct(totals['buy_only_ret'])} | — |"
    )

    lines.extend([
        "",
        "**说明**",
        "",
        "- **买卖波段**：有卖点的指数按指标买入、触发卖点时全部卖出；无卖点指数等同仅买入。",
        "- **仅买入持有**：同期所有买入信号均执行，不因卖点卖出（对照组）。",
        "- 有卖点指数的「买卖收益率」与「仅买收益率」差异反映卖点策略效果。",
        "",
        "## 风险调整收益（夏普比率）",
        "",
        f"> 无风险利率年化 **{BACKTEST_RISK_FREE_RATE * 100:.1f}%**（`BACKTEST_RISK_FREE_RATE`）；"
        f"策略日收益已剔除买入注资；基准为同期指数价格日收益。",
        "",
        "| 指数 | 代码 | 策略收益 | 年化收益 | 夏普 | 基准夏普 | 最大回撤 | 性价比 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    ranked = sorted(
        results,
        key=lambda r: r.sharpe_ratio if r.sharpe_ratio is not None else -999,
        reverse=True,
    )
    for r in ranked:
        strat_ret = _effective_return(r)
        sharpe = r.sharpe_ratio
        bench = r.benchmark_sharpe
        if sharpe is None:
            tier = "—"
        elif sharpe >= 1.0:
            tier = "高"
        elif sharpe >= 0.5:
            tier = "中"
        elif sharpe >= 0:
            tier = "偏低"
        else:
            tier = "差"
        lines.append(
            f"| {r.name} | {r.code} | {_md_pct(strat_ret)} | "
            f"{_md_pct(r.annualized_return_pct)} | {_md_sharpe(sharpe)} | "
            f"{_md_sharpe(bench)} | {_md_pct(r.max_drawdown_pct)} | {tier} |"
        )
    lines.extend([
        "",
        "**解读**：夏普越高，单位风险带来的超额收益越好；"
        "最大回撤为策略持仓市值相对峰值的最大跌幅。",
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


def print_table(results, start_date, end_date, amounts):
    end_label = end_date or "最新"
    val_dates = [r.final_date for r in results if r.final_date is not None]
    val_date = max(val_dates).strftime("%Y-%m-%d") if val_dates else "—"
    print(
        f"\n=== {start_date} 至 {end_label} 买卖信号回测 "
        f"（{format_backtest_amount_note(amounts)}，截至 {val_date}） ==="
    )
    print(
        f"{'指数':<14} {'代码':<8} {'策略':<8} {'买入':>5} {'卖出':>5} "
        f"{'投入':>8} {'收益':>8} {'夏普':>6} {'回撤':>8}"
    )
    print("-" * 88)
    for r in results:
        strategy = "买卖" if r.has_sell else "仅买"
        trade_ret = _effective_return(r)
        trade_text = f"{trade_ret:+.1f}%" if trade_ret is not None else "   —"
        sharpe_text = f"{r.sharpe_ratio:.2f}" if r.sharpe_ratio is not None else "   —"
        mdd_text = (
            f"{r.max_drawdown_pct:.1f}%"
            if r.max_drawdown_pct is not None
            else "   —"
        )
        print(
            f"{r.name:<14} {r.code:<8} {strategy:<8} "
            f"{r.buy_count:>5} {r.sell_count:>5} "
            f"{r.total_bought:>8.0f} {trade_text:>8} {sharpe_text:>6} {mdd_text:>8}"
        )
    totals = _trade_totals(results)
    trade_ret = totals["trade_ret"]
    trade_text = f"{trade_ret:+.1f}%" if trade_ret is not None else "   —"
    print("-" * 88)
    print(
        f"{'合计':<14} {'—':<8} {'—':<8} "
        f"{totals['buy_count']:>5} {totals['sell_count']:>5} "
        f"{totals['total_bought']:>8.0f} {trade_text:>8} {'—':>6} {'—':>8}"
    )
    print("-" * 88)


def save_result(markdown, filename="trade_2015_present.md", write_html=True, **html_kwargs):
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKTEST_DIR / filename
    path.write_text(markdown, encoding="utf-8")
    html_path = None
    if write_html and html_kwargs.get("daily_tables"):
        from backtest_html import save_backtest_html

        stem = path.stem
        html_path = save_backtest_html(
            BACKTEST_DIR / f"{stem}.html",
            html_kwargs.get("title", "买卖信号回测"),
            html_kwargs["daily_tables"],
            start_date=html_kwargs.get("start_date"),
            end_date=html_kwargs.get("end_date"),
            subtitle=html_kwargs.get("subtitle", ""),
        )
    return path, html_path


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
        default=None,
        help="统一覆盖所有指数单次买入金额（元；默认红利300、宽基100、其他300）",
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="使用组合仓位分指数买入金额（默认：收益最大化分指数+分档）",
    )
    parser.add_argument(
        "--no-tier",
        action="store_true",
        help="禁用价格分档，仅使用基准单次金额",
    )
    parser.add_argument(
        "--output",
        default="trade_2015_present.md",
        help="输出文件名（保存在 output/backtest/）",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="不生成 HTML 折线图",
    )
    args = parser.parse_args(argv)

    try:
        tier_enabled = not args.no_tier
        if args.amount is not None and args.amount > 0:
            amounts = resolve_backtest_amounts(args.amount, tier_enabled=tier_enabled)
        elif args.portfolio:
            amounts = resolve_backtest_amounts(
                portfolio_mode=True, tier_enabled=tier_enabled
            )
        else:
            amounts = resolve_backtest_amounts(tier_enabled=tier_enabled)
        print(
            f"正在回测 {args.start} 至 {args.end or '最新'} "
            f"（买入/卖出按当前 config 阈值）..."
        )
        panels = BacktestPanels()
        results = backtest_all(
            start_date=args.start,
            end_date=args.end,
            amounts=amounts,
            panels=panels,
        )
        print_table(results, args.start, args.end, amounts)
        markdown = format_markdown(
            results, args.start, args.end, amounts
        )
        daily_tables = collect_trade_chart_tables(
            args.start, args.end, amounts=amounts, panels=panels
        )
        end_label = args.end or "最新"
        path, html_path = save_result(
            markdown,
            filename=args.output,
            write_html=not args.no_html,
            daily_tables=daily_tables,
            title=f"{args.start} 至 {end_label} 买卖信号回测",
            start_date=args.start,
            end_date=args.end,
            subtitle=(
                f"区间 {args.start} 至 {end_label}；"
                f"{format_backtest_amount_note(amounts)}"
            ),
        )
        print(f"\n回测结果已保存: {path}")
        if html_path:
            print(f"折线图已保存: {html_path}")
    except Exception as exc:
        print(f"回测失败: {exc}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
