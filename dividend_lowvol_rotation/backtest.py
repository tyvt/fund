# -*- coding: utf-8 -*-
"""红利低波轮动回测：缓冲带调仓 + 明细输出。"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from dividend_lowvol_rotation.config import (
    BACKTEST_INITIAL_CAPITAL,
    BACKTEST_OUTPUT_DIR,
    BACKTEST_PREFETCH_SIZE,
    BACKTEST_REBALANCE_DAYS,
    BACKTEST_YEARS,
    DIVIDEND_TAX_ENABLED,
    SELL_RANK_MULTIPLIER,
    TOP_N_BUY,
    resolve_sell_rank,
)
from dividend_lowvol_rotation.backtest_report import format_backtest_report, save_backtest_outputs
from dividend_lowvol_rotation.costs import max_buy_shares, single_side_commission
from dividend_lowvol_rotation.dividend import build_dividend_panel, load_fhps_all_records
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_taxes, build_dividend_index
from dividend_lowvol_rotation.dynamic_params import resolve_dynamic_params
from dividend_lowvol_rotation.industry import attach_industry
from dividend_lowvol_rotation.prices import (
    baostock_session,
    load_kline_history,
    metrics_as_of,
)
from dividend_lowvol_rotation.scoring import dynamic_dividend_yield_pct, run_screening
from dividend_lowvol_rotation.symbols import is_excluded_name
from market_data import configure_stdout_utf8


@dataclass
class PositionLot:
    code: str
    name: str
    shares: int
    buy_date: pd.Timestamp
    buy_price: float
    cost_basis: float
    buy_fee: float
    peak_price: float = 0.0
    max_drawdown_pct: float = 0.0

    def update_peak_drawdown(self, price: float) -> None:
        if price <= 0:
            return
        self.peak_price = max(self.peak_price, price)
        if self.peak_price > 0:
            dd = price / self.peak_price - 1
            self.max_drawdown_pct = min(self.max_drawdown_pct, dd)


@dataclass
class StockStats:
    code: str
    name: str = ""
    buy_count: int = 0
    sell_count: int = 0
    total_buy_amount: float = 0.0
    total_sell_amount: float = 0.0
    total_fees: float = 0.0
    realized_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    holding_days: int = 0
    closed_lots: int = 0


def default_start_years(years: int = BACKTEST_YEARS) -> str:
    return (date.today() - timedelta(days=int(365.25 * years))).isoformat()


def _trading_calendar(start: str, end: str) -> list[pd.Timestamp]:
    with baostock_session() as bs:
        rs = bs.query_history_k_data_plus(
            "sh.000001",
            "date",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="3",
        )
        dates = []
        while rs.error_code == "0" and rs.next():
            d = rs.get_row_data()[0]
            if d:
                dates.append(pd.Timestamp(d))
    return dates


def _rebalance_dates(calendar: list[pd.Timestamp], step: int) -> list[pd.Timestamp]:
    if not calendar:
        return []
    out = []
    i = 0
    while i < len(calendar):
        out.append(calendar[i])
        i += step
    if out[-1] != calendar[-1]:
        out.append(calendar[-1])
    return out


class KlineStore:
    """回测内存 K 线库：启动时一次性加载，调仓日零网络请求。"""

    def __init__(self, start: str, end: str):
        self.start = start
        self.end = end
        self._klines: dict[str, pd.DataFrame] = {}

    def preload(self, codes: list[str], *, verbose: bool = True) -> None:
        unique = [c for c in dict.fromkeys(codes) if c not in self._klines]
        if not unique:
            return
        total = len(unique)
        if verbose:
            print(f"预加载 K 线 {total} 只（{self.start} ~ {self.end}）…")
        with baostock_session() as bs:
            for i, code in enumerate(unique, 1):
                kline = load_kline_history(code, self.start, self.end, bs=bs)
                if kline is not None and not kline.empty:
                    self._klines[code] = kline
                if verbose and (i % 50 == 0 or i == total):
                    print(f"  {i}/{total}")

    def ensure(self, codes: list[str]) -> None:
        missing = [c for c in dict.fromkeys(codes) if c not in self._klines]
        if missing:
            self.preload(missing, verbose=False)

    def price_at(self, code: str, as_of: pd.Timestamp) -> float | None:
        return self.metrics_at(code, as_of).get("price")

    def metrics_at(self, code: str, as_of: pd.Timestamp) -> dict:
        kline = self._klines.get(code)
        if kline is None or kline.empty:
            return {"price": None, "ann_vol_pct": None, "low_n": None, "high_n": None}
        return metrics_as_of(kline, as_of)


@dataclass
class BacktestContext:
    """预加载数据，供多次回测复用（WFA / 蒙特卡洛）。"""

    start: str
    end: str
    records: pd.DataFrame
    calendar: list[pd.Timestamp]
    store: KlineStore
    industry_df: pd.DataFrame
    _panel_cache: dict[str, pd.DataFrame] = field(default_factory=dict)
    _dividend_cache: dict[str, pd.DataFrame] = field(default_factory=dict)

    def dividend_at(self, as_of: pd.Timestamp) -> pd.DataFrame:
        key = as_of.date().isoformat()
        if key not in self._dividend_cache:
            self._dividend_cache[key] = build_dividend_panel(records=self.records, as_of=as_of)
        return self._dividend_cache[key]

    def panel_at(self, as_of: pd.Timestamp, prefetch_size: int) -> pd.DataFrame:
        key = as_of.date().isoformat()
        if key not in self._panel_cache:
            self._panel_cache[key] = _build_panel_from_store(
                as_of,
                self.records,
                self.store,
                self.industry_df,
                prefetch_size,
                div_panel=self.dividend_at(as_of),
            )
        return self._panel_cache[key]


def prepare_backtest_context(
    start: str,
    end: str | None = None,
    *,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    rebalance_days: int = BACKTEST_REBALANCE_DAYS,
    reb_dates: list[pd.Timestamp] | None = None,
    verbose: bool = True,
) -> BacktestContext:
    end = end or date.today().isoformat()
    records = load_fhps_all_records(refresh=False, backtest_start=start)
    calendar = _trading_calendar(start, end)
    reb_dates = reb_dates or _rebalance_dates(calendar, rebalance_days)
    kline_start = (pd.Timestamp(start) - timedelta(days=200)).date().isoformat()
    store = KlineStore(kline_start, end)
    tmp_ctx = BacktestContext(
        start=start, end=end, records=records, calendar=calendar, store=store, industry_df=pd.DataFrame()
    )
    candidate_codes = _collect_candidate_codes(records, reb_dates, prefetch_size, ctx=tmp_ctx)
    store.preload(candidate_codes, verbose=verbose)
    industry_df = attach_industry(
        pd.DataFrame({"code": candidate_codes}), refresh=False
    )
    return BacktestContext(
        start=start,
        end=end,
        records=records,
        calendar=calendar,
        store=store,
        industry_df=industry_df,
    )


def _resolve_price(
    code: str, panel: pd.DataFrame, as_of: pd.Timestamp, store: KlineStore
) -> float | None:
    row = panel[panel["code"] == code]
    if not row.empty:
        return float(row["price"].iloc[0])
    return store.price_at(code, as_of)


def _collect_candidate_codes(
    records: pd.DataFrame,
    reb_dates: list[pd.Timestamp],
    prefetch_size: int,
    ctx: BacktestContext | None = None,
) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for as_of in reb_dates:
        div = ctx.dividend_at(as_of) if ctx else build_dividend_panel(records=records, as_of=as_of)
        if div.empty:
            continue
        div = div[~div["name"].map(is_excluded_name)]
        if "fhps_yield_pct" in div.columns:
            div = div.sort_values(["fhps_yield_pct", "cash_per_share"], ascending=[False, False])
        else:
            div = div.sort_values("cash_per_share", ascending=False)
        for code in div["code"].head(prefetch_size):
            c = str(code)
            if c not in seen:
                seen.add(c)
                codes.append(c)
    return codes


def _build_panel_from_store(
    as_of: pd.Timestamp,
    records: pd.DataFrame,
    store: KlineStore,
    industry_df: pd.DataFrame | None,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    *,
    div_panel: pd.DataFrame | None = None,
) -> pd.DataFrame:
    div = div_panel if div_panel is not None else build_dividend_panel(records=records, as_of=as_of)
    if div.empty:
        return pd.DataFrame()
    div = div[~div["name"].map(is_excluded_name)]
    if "fhps_yield_pct" in div.columns:
        div = div.sort_values(["fhps_yield_pct", "cash_per_share"], ascending=[False, False])
    else:
        div = div.sort_values("cash_per_share", ascending=False)
    codes = div["code"].head(prefetch_size).tolist()

    rows = []
    for code in codes:
        m = store.metrics_at(code, as_of)
        if m.get("price") is None or m.get("ann_vol_pct") is None:
            continue
        base = div[div["code"] == code].iloc[0].to_dict()
        base.update(m)
        base["dividend_yield_pct"] = dynamic_dividend_yield_pct(
            base.get("cash_per_share"), base.get("price")
        )
        if base["dividend_yield_pct"] is None:
            continue
        rows.append(base)

    if not rows:
        return pd.DataFrame()
    panel = pd.DataFrame(rows)
    if industry_df is not None and not industry_df.empty:
        panel = panel.merge(
            industry_df.drop_duplicates("code"),
            on="code",
            how="left",
            suffixes=("", "_ind"),
        )
    return panel


def _name_for(code: str, panel: pd.DataFrame, name_cache: dict[str, str]) -> str:
    row = panel[panel["code"] == code]
    if not row.empty and row["name"].iloc[0]:
        name = str(row["name"].iloc[0])
        name_cache[code] = name
        return name
    return name_cache.get(code, "")


def run_backtest(
    *,
    start: str | None = None,
    end: str | None = None,
    top_n: int = TOP_N_BUY,
    rebalance_days: int = BACKTEST_REBALANCE_DAYS,
    initial_capital: float = BACKTEST_INITIAL_CAPITAL,
    sell_rank: int | None = None,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    hold_only: bool = False,
    reb_dates_override: list[pd.Timestamp] | None = None,
    verbose: bool = True,
    ctx: BacktestContext | None = None,
    record_details: bool = True,
    apply_dividend_tax: bool | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict, pd.DataFrame]:
    start = start or default_start_years()
    end = end or date.today().isoformat()
    sell_rank = resolve_sell_rank(top_n, sell_rank)
    if apply_dividend_tax is None:
        apply_dividend_tax = DIVIDEND_TAX_ENABLED

    if ctx is None:
        ctx = prepare_backtest_context(
            start,
            end,
            prefetch_size=prefetch_size,
            rebalance_days=rebalance_days,
            reb_dates=reb_dates_override,
            verbose=verbose,
        )
    elif verbose:
        print("复用已加载 K 线缓存…")

    records = ctx.records
    calendar = ctx.calendar
    store = ctx.store
    industry_df = ctx.industry_df
    reb_dates = reb_dates_override or _rebalance_dates(calendar, rebalance_days)

    lots: dict[str, PositionLot] = {}
    cash = float(initial_capital)
    name_cache: dict[str, str] = {}
    stock_stats: dict[str, StockStats] = {}

    nav_rows: list[dict] = []
    trade_rows: list[dict] = []
    holding_rows: list[dict] = []
    dividend_tax_rows: list[dict] = []
    total_dividend_tax = 0.0
    total_gross_dividend = 0.0

    div_index = build_dividend_index(records) if apply_dividend_tax else {}
    prev_rb: pd.Timestamp | None = None

    def _stats(code: str) -> StockStats:
        if code not in stock_stats:
            stock_stats[code] = StockStats(code=code)
        return stock_stats[code]

    for rb_date in reb_dates:
        if apply_dividend_tax and lots and prev_rb is not None:
            tax, gross, rows = accrue_dividend_taxes(lots, div_index, prev_rb, rb_date)
            if rows:
                total_gross_dividend += gross
                total_dividend_tax += tax
                dividend_tax_rows.extend(rows)
                if tax > 0:
                    cash -= tax

        if lots:
            store.ensure(list(lots.keys()))
        panel = ctx.panel_at(rb_date, prefetch_size)
        if panel.empty:
            continue
        for _, r in panel.iterrows():
            name_cache[str(r["code"])] = str(r.get("name", ""))

        dynamic = resolve_dynamic_params(panel)
        ranked, buy_pool, _stats_panel = run_screening(
            panel, top_n=top_n, sell_rank=sell_rank, dynamic=dynamic, as_of=rb_date
        )
        if ranked.empty:
            continue

        rank_map = dict(zip(ranked["code"], ranked["rank"]))
        buy_codes = buy_pool["code"].tolist() if not buy_pool.empty else []

        port_value = cash

        if hold_only and lots:
            # 买入持有对照：建仓后不再调仓
            pass
        else:
            # 卖出
            for code, lot in list(lots.items()):
                rank = rank_map.get(code)
                if rank is not None and rank <= sell_rank:
                    price = _resolve_price(code, panel, rb_date, store)
                    if price:
                        lot.update_peak_drawdown(price)
                    continue
                price = _resolve_price(code, panel, rb_date, store)
                if price is None or price <= 0:
                    continue
                lot.update_peak_drawdown(price)
                proceeds = lot.shares * price
                fee = single_side_commission(proceeds)
                net = proceeds - fee
                realized = net - lot.cost_basis
                ret_pct = realized / lot.cost_basis * 100 if lot.cost_basis > 0 else None
                hold_days = (rb_date - lot.buy_date).days
                st = _stats(code)
                st.name = lot.name or st.name
                st.sell_count += 1
                st.total_sell_amount += proceeds
                st.total_fees += fee
                st.realized_pnl += realized
                st.max_drawdown_pct = min(st.max_drawdown_pct, lot.max_drawdown_pct)
                st.holding_days += hold_days
                st.closed_lots += 1
                cash += net
                reason = "跌出缓冲带" if rank is None else f"排名{rank}>{sell_rank}"
                trade_rows.append(
                    {
                        "date": rb_date.date().isoformat(),
                        "side": "卖出",
                        "code": code,
                        "name": lot.name,
                        "price": round(price, 4),
                        "shares": int(lot.shares),
                        "amount": round(proceeds, 2),
                        "fee": round(fee, 2),
                        "net_amount": round(net, 2),
                        "rank": rank,
                        "reason": reason,
                        "hold_days": hold_days,
                        "buy_price": round(lot.buy_price, 4),
                        "buy_date": lot.buy_date.date().isoformat(),
                        "realized_pnl": round(realized, 2),
                        "return_pct": round(ret_pct, 4) if ret_pct is not None else None,
                        "max_drawdown_pct": round(lot.max_drawdown_pct * 100, 4),
                    }
                )
                del lots[code]

            # 买入
            slots = top_n - len(lots)
            new_codes = [c for c in buy_codes if c not in lots][:slots]
            if new_codes and cash > 0:
                alloc = cash / len(new_codes)
                for code in new_codes:
                    price = _resolve_price(code, panel, rb_date, store)
                    if price is None or price <= 0:
                        continue
                    shares = max_buy_shares(min(alloc, cash), price)
                    if shares <= 0:
                        continue
                    gross = shares * price
                    fee = single_side_commission(gross)
                    total_cost = gross + fee
                    if total_cost > cash:
                        continue
                    name = _name_for(code, panel, name_cache)
                    lots[code] = PositionLot(
                        code=code,
                        name=name,
                        shares=shares,
                        buy_date=rb_date,
                        buy_price=price,
                        cost_basis=total_cost,
                        buy_fee=fee,
                        peak_price=price,
                    )
                    st = _stats(code)
                    st.name = name
                    st.buy_count += 1
                    st.total_buy_amount += total_cost
                    st.total_fees += fee
                    cash -= total_cost
                    trade_rows.append(
                        {
                            "date": rb_date.date().isoformat(),
                            "side": "买入",
                            "code": code,
                            "name": name,
                            "price": round(price, 4),
                            "shares": shares,
                            "amount": round(gross, 2),
                            "fee": round(fee, 2),
                            "net_amount": round(gross, 2),
                            "rank": rank_map.get(code),
                            "reason": "进入买入池" if not hold_only else "买入持有建仓",
                            "hold_days": None,
                            "buy_price": round(price, 4),
                            "buy_date": rb_date.date().isoformat(),
                            "realized_pnl": None,
                            "return_pct": None,
                            "max_drawdown_pct": None,
                        }
                    )

        # 持仓快照
        port_value = cash
        for code, lot in lots.items():
            price = _resolve_price(code, panel, rb_date, store)
            if price is None or price <= 0:
                continue
            lot.update_peak_drawdown(price)
            mv = lot.shares * price
            port_value += mv
            if record_details:
                unrealized = mv - lot.cost_basis
                ur_pct = unrealized / lot.cost_basis * 100 if lot.cost_basis > 0 else None
                holding_rows.append(
                    {
                        "date": rb_date.date().isoformat(),
                        "code": code,
                        "name": lot.name,
                        "shares": int(lot.shares),
                        "price": round(price, 4),
                        "market_value": round(mv, 2),
                        "weight_pct": None,
                        "rank": rank_map.get(code),
                        "buy_price": round(lot.buy_price, 4),
                        "buy_date": lot.buy_date.date().isoformat(),
                        "cost_basis": round(lot.cost_basis, 2),
                        "unrealized_pnl": round(unrealized, 2),
                        "unrealized_return_pct": round(ur_pct, 4) if ur_pct is not None else None,
                        "max_drawdown_pct": round(lot.max_drawdown_pct * 100, 4),
                        "hold_days": (rb_date - lot.buy_date).days,
                    }
                )

        if record_details:
            for row in holding_rows:
                if row["date"] == rb_date.date().isoformat() and row.get("weight_pct") is None:
                    if port_value > 0:
                        row["weight_pct"] = round(row["market_value"] / port_value * 100, 4)

        nav_rows.append(
            {
                "date": rb_date.date().isoformat(),
                "nav": round(port_value, 2),
                "cash": round(cash, 2),
                "holdings_count": len(lots),
                "return_pct": round((port_value / initial_capital - 1) * 100, 4),
            }
        )
        prev_rb = rb_date

    nav_df = pd.DataFrame(nav_rows)

    if apply_dividend_tax and lots and prev_rb is not None:
        end_ts = pd.Timestamp(end)
        if end_ts > prev_rb:
            tax, gross, rows = accrue_dividend_taxes(lots, div_index, prev_rb, end_ts)
            if rows:
                total_gross_dividend += gross
                total_dividend_tax += tax
                dividend_tax_rows.extend(rows)
                if tax > 0:
                    cash -= tax
                if not nav_df.empty:
                    last_rb = prev_rb
                    port_value = cash
                    for code, lot in lots.items():
                        price = store.price_at(code, last_rb)
                        if price:
                            port_value += lot.shares * price
                    nav_df.loc[nav_df.index[-1], "cash"] = round(cash, 2)
                    nav_df.loc[nav_df.index[-1], "nav"] = round(port_value, 2)
                    nav_df.loc[nav_df.index[-1], "return_pct"] = round(
                        (port_value / initial_capital - 1) * 100, 4
                    )

    trades_df = pd.DataFrame(trade_rows)
    holdings_df = pd.DataFrame(holding_rows)
    dividend_tax_df = pd.DataFrame(dividend_tax_rows)

    # 个股汇总（仅详细模式）
    summary_rows = []
    if record_details:
        for code, st in stock_stats.items():
            avg_hold = st.holding_days / st.closed_lots if st.closed_lots else None
            ret_on_cost = (
                st.realized_pnl / st.total_buy_amount * 100 if st.total_buy_amount > 0 else None
            )
            lot = lots.get(code)
            status = "持仓中" if lot else "已清仓"
            unrealized = None
            ur_pct = None
            if lot and not nav_df.empty:
                last_date = pd.Timestamp(nav_df["date"].iloc[-1])
                price = store.price_at(code, last_date)
                if price:
                    unrealized = lot.shares * price - lot.cost_basis
                    ur_pct = unrealized / lot.cost_basis * 100 if lot.cost_basis > 0 else None
            summary_rows.append(
                {
                    "code": code,
                    "name": st.name,
                    "status": status,
                    "buy_count": st.buy_count,
                    "sell_count": st.sell_count,
                    "total_buy_amount": round(st.total_buy_amount, 2),
                    "total_sell_amount": round(st.total_sell_amount, 2),
                    "total_fees": round(st.total_fees, 2),
                    "realized_pnl": round(st.realized_pnl, 2),
                    "realized_return_pct": round(ret_on_cost, 4) if ret_on_cost is not None else None,
                    "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
                    "unrealized_return_pct": round(ur_pct, 4) if ur_pct is not None else None,
                    "total_contribution_pnl": round(
                        st.realized_pnl + (unrealized or 0), 2
                    ),
                    "avg_hold_days": round(avg_hold, 1) if avg_hold is not None else None,
                    "max_drawdown_pct": round(st.max_drawdown_pct * 100, 4),
                }
            )
    summary_df = pd.DataFrame(summary_rows)
    if not summary_df.empty:
        summary_df = summary_df.sort_values("total_contribution_pnl", ascending=False)
    else:
        summary_df = pd.DataFrame()

    meta = {
        "start": start,
        "end": end,
        "rebalance_days": rebalance_days,
        "top_n": top_n,
        "sell_rank": sell_rank,
        "sell_rank_multiplier": SELL_RANK_MULTIPLIER,
        "initial_capital": initial_capital,
        "rebalance_count": len(reb_dates),
        "trade_count": len(trade_rows),
        "sell_count": int((trades_df["side"] == "卖出").sum()) if not trades_df.empty else 0,
        "buy_count": int((trades_df["side"] == "买入").sum()) if not trades_df.empty else 0,
        "prefetch_size": prefetch_size,
        "hold_only": hold_only,
        "dividend_tax_enabled": apply_dividend_tax,
        "total_gross_dividend": round(total_gross_dividend, 2),
        "total_dividend_tax": round(total_dividend_tax, 2),
        "dividend_tax_events": len(dividend_tax_rows),
    }
    if not nav_df.empty:
        final_nav = float(nav_df["nav"].iloc[-1])
        total_ret = final_nav / initial_capital - 1
        t0 = pd.Timestamp(nav_df["date"].iloc[0])
        t1 = pd.Timestamp(nav_df["date"].iloc[-1])
        years = max((t1 - t0).days / 365.25, 1 / 365)
        cagr = (1 + total_ret) ** (1 / years) - 1
        rets = nav_df["nav"].pct_change().dropna()
        sharpe = None
        if len(rets) > 2 and rets.std() > 0:
            sharpe = float(rets.mean() / rets.std() * np.sqrt(252 / rebalance_days))
        dd = (nav_df["nav"] / nav_df["nav"].cummax() - 1).min()
        meta.update(
            {
                "final_nav": final_nav,
                "total_return_pct": float(total_ret * 100),
                "cagr_pct": float(cagr * 100),
                "max_drawdown_pct": float(dd * 100),
                "sharpe": sharpe,
            }
        )
    return nav_df, trades_df, holdings_df, summary_df, meta, dividend_tax_df


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="红利低波轮动回测")
    parser.add_argument("--start", default=None, help="默认近 N 年")
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS, help="回测年数（未指定 start 时）")
    parser.add_argument("--end", default=None)
    parser.add_argument("--top", type=int, default=TOP_N_BUY)
    parser.add_argument("--sell-rank", type=int, default=None)
    parser.add_argument("--rebalance-days", type=int, default=BACKTEST_REBALANCE_DAYS)
    parser.add_argument("--capital", type=float, default=BACKTEST_INITIAL_CAPITAL)
    parser.add_argument("--prefetch", type=int, default=BACKTEST_PREFETCH_SIZE, help="候选预筛数量")
    parser.add_argument("--no-dividend-tax", action="store_true", help="不扣分红个税（对比税前收益）")
    parser.add_argument("-o", "--output-dir", default=None, help="输出目录")
    args = parser.parse_args(argv)

    start = args.start or default_start_years(args.years)
    sell_rank = resolve_sell_rank(args.top, args.sell_rank)
    print(f"回测 {start} ~ {args.end or '今'}，持仓 {args.top} 只，每 {args.rebalance_days} 日检查…")
    t0 = time.time()

    nav_df, trades_df, holdings_df, summary_df, meta, dividend_tax_df = run_backtest(
        start=start,
        end=args.end,
        top_n=args.top,
        sell_rank=sell_rank,
        rebalance_days=args.rebalance_days,
        initial_capital=args.capital,
        prefetch_size=args.prefetch,
        apply_dividend_tax=not args.no_dividend_tax,
    )
    report = format_backtest_report(nav_df, trades_df, summary_df, meta, dividend_tax_df)
    elapsed = time.time() - t0
    print(report)
    print(f"\n总耗时：**{elapsed:.0f}** 秒")

    out_dir = Path(args.output_dir) if args.output_dir else BACKTEST_OUTPUT_DIR
    paths = save_backtest_outputs(
        out_dir, nav_df, trades_df, holdings_df, summary_df, meta, dividend_tax_df
    )
    print("\n已写入：")
    for k, p in paths.items():
        if p.exists():
            print(f"  {k}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
