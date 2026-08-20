# -*- coding: utf-8 -*-
"""红利低波轮动回测：缓冲带调仓 + 明细输出。"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd

from data_cache import load_dataframe, merge_dataframes_by_date, save_dataframe
from dividend_lowvol_rotation.config import (
    BACKTEST_INITIAL_CAPITAL,
    BACKTEST_KLINE_FQ,
    BACKTEST_DIVIDEND_CASH,
    resolve_backtest_kline_fq,
    uses_rqalpha_price_source,
    BACKTEST_MIN_HOLD_DAYS,
    BACKTEST_OUTPUT_DIR,
    BACKTEST_PREFETCH_SIZE,
    BACKTEST_REBALANCE_DAYS,
    BACKTEST_REBALANCE_MODE,
    BACKTEST_YEARS,
    BEAR_VOL_MIN_SAMPLES,
    BEAR_VOL_PERCENTILE_LOOKBACK,
    BEAR_VOL_PERCENTILE_THRESHOLD,
    BEAR_VOL_THRESHOLD_PCT,
    BEAR_VOL_USE_PERCENTILE,
    EMERGENCY_SELL_DAILY_DROP_PCT,
    EMERGENCY_SELL_ENABLED,
    EMERGENCY_SELL_TWO_DAY_DROP_PCT,
    CONDITIONAL_REBUY_ENABLED,
    CONDITIONAL_REBUY_MIN_POSITION_SCALE,
    GRACE_EARLY_SELL_DOWN_DAYS,
    GRACE_PERIOD_DAYS_HIGH_VOL,
    GRACE_PERIOD_DAYS_LOW_VOL,
    GRACE_REBOUND_RESET_ENABLED,
    GRACE_VOL_ADAPTIVE_ENABLED,
    GRACE_VOL_HIGH_THRESHOLD_PCT,
    INDEX_DIVIDEND_WEIGHTING,
    INDEX_STYLE_RANKING,
    LOT_SIZE,
    MARKET_REGIME_ENABLED,
    MARKET_VALUATION_ENABLED,
    MAX_SINGLE_STOCK_WEIGHT,
    MOMENTUM_SELL_ENABLED,
    MOMENTUM_SELL_MA_DAYS,
    MOMENTUM_SELL_RANK_THRESHOLD,
    PRICE_HISTORY_BUFFER_DAYS,
    SELL_GRACE_PERIOD_DAYS,
    SELL_GRACE_PERIOD_ENABLED,
    SLIPPAGE_RATE,
    execution_slippage_enabled,
    STOP_LOSS_ENABLED,
    STOP_ATR_ENABLED,
    STOP_ATR_MULTIPLIER,
    TAKE_PROFIT_ENABLED,
    TAKE_PROFIT_PCT,
    TAKE_PROFIT_STATIC_ENABLED,
    TRAILING_STOP_ACTIVATION_PCT,
    TRAILING_STOP_ENABLED,
    TRAILING_STOP_EXTENDED_PCT,
    TRAILING_STOP_FROM_PEAK_PCT,
    DIVIDEND_TAX_ENABLED,
    DIVIDEND_TAX_YEAR_DAYS,
    SELL_RANK_MULTIPLIER,
    INDEX_RULES_DAILY_RISK_ENABLED,
    SELL_MODE,
    TOP_N_BUY,
    resolve_sell_rank,
)
from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates
from dividend_lowvol_rotation.risk_regime import (
    estimate_portfolio_vol_pct,
    resolve_grace_period_days,
    resolve_position_scale,
    resolve_stop_loss_pct,
)
from dividend_lowvol_rotation.backtest_report import format_backtest_report, save_backtest_outputs
from dividend_lowvol_rotation.costs import (
    max_buy_shares,
    resolve_execution_raw_price,
    settle_sell,
    single_side_commission,
    trade_execution_price,
)
from dividend_lowvol_rotation.dividend import build_dividend_panel, sort_dividend_prefetch, load_fhps_all_records
from dividend_lowvol_rotation.corporate_actions import apply_splits_on_date, build_split_index
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date, accrue_dividend_taxes, build_dividend_index
from dividend_lowvol_rotation.dynamic_params import DynamicParams, resolve_dynamic_params
from dividend_lowvol_rotation.index_retention import (
    enrich_panel_with_holdings,
    should_sell_index_rules,
)
from dividend_lowvol_rotation.index_portfolio import (
    build_index_target_codes,
    target_weights_for_portfolio,
)
from dividend_lowvol_rotation.industry import attach_industry
from dividend_lowvol_rotation.prices import (
    metrics_as_of,
    metrics_from_precomputed,
    precompute_kline_metrics,
    _batch_fetch_klines_from_stockdb,
    _batch_load_klines_from_duckdb,
    _cache_covers,
    _get_stockdb_client,
    _kline_cache_path,
)
from duckdb_market import duckdb_available, load_trade_calendar
from dividend_lowvol_rotation.risk_screening import (
    attach_risk_from_records,
    batch_load_risk_history,
    build_dividend_year_index,
    merge_risk_history,
)
from dividend_lowvol_rotation.enhanced_factors import attach_enhanced_factors
from dividend_lowvol_rotation.scoring import dynamic_dividend_yield_pct, run_screening
from dividend_lowvol_rotation.market_valuation import load_market_pe_history, valuation_regime
from dividend_lowvol_rotation.strategy_params import StrategyParams
from dividend_lowvol_rotation.symbols import is_excluded_name, normalize_stock_code
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
    prev_price: float = 0.0
    down_streak: int = 0

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
    """优先 DuckDB 交易日历，回退 stockdb / 本地缓存。"""
    dates = load_trade_calendar(start, end)
    if dates:
        return dates

    try:
        client = _get_stockdb_client()
        start_fmt = start.replace("-", "")
        end_fmt = end.replace("-", "")

        k = client.get_data(
            "000001",
            start=start_fmt,
            end=end_fmt,
            frequency="1d",
            fields="date",
            fq=None,
            as_df=True,
        )

        if k is None or k.empty:
            raise ValueError("stockdb 返回空日历")

        out: list[pd.Timestamp] = []
        for d in k["date"].astype(str):
            try:
                out.append(pd.Timestamp(d))
            except ValueError:
                pass

        return sorted(out)
    except Exception as e:
        print(f"获取交易日历失败: {e}，尝试本地缓存…")
        cached = load_dataframe(_kline_cache_path("000001"), parse_dates=["date"])
        if cached is not None and not cached.empty:
            mask = (cached["date"] >= pd.Timestamp(start)) & (cached["date"] <= pd.Timestamp(end))
            return sorted(cached.loc[mask, "date"].tolist())
        return []


def _rebalance_dates(calendar: list[pd.Timestamp], step: int) -> list[pd.Timestamp]:
    return resolve_rebalance_dates(calendar, mode="fixed_days", rebalance_days=step)


def _resolve_min_hold_days(
    mode: str,
    explicit: int | None = None,
) -> int:
    if explicit is not None:
        return max(0, int(explicit))
    if mode == "index_annual":
        return DIVIDEND_TAX_YEAR_DAYS
    return BACKTEST_MIN_HOLD_DAYS


class KlineStore:
    """回测内存 K 线库：启动时一次性加载，调仓日零网络请求。"""

    def __init__(
        self,
        start: str,
        end: str,
        *,
        backtest_start: str | None = None,
        kline_fq: str | None = None,
    ):
        self.start = start
        self.end = end
        self.backtest_start = backtest_start or start
        self.kline_fq = resolve_backtest_kline_fq() if kline_fq is None else kline_fq
        self._klines: dict[str, pd.DataFrame] = {}
        self._metrics: dict[str, pd.DataFrame] = {}
        self._total_mv: dict[str, pd.DataFrame] = {}
        self._market_fields: dict[str, pd.DataFrame] = {}

    def _store_kline(self, code: str, kline: pd.DataFrame) -> None:
        if kline is None or kline.empty:
            return
        self._klines[code] = kline
        self._metrics[code] = precompute_kline_metrics(kline)

    def preload(self, codes: list[str], *, verbose: bool = True) -> None:
        unique = [c for c in dict.fromkeys(codes) if c not in self._klines]
        if not unique:
            return
        total = len(unique)
        if verbose:
            fq_label = self.kline_fq or "none"
            src = "RQAlpha bundle" if uses_rqalpha_price_source() else "DuckDB 优先"
            print(f"预加载 K 线 {total} 只（{self.start} ~ {self.end}，{fq_label}，{src}）…")

        start_ts = pd.Timestamp(self.start)
        end_ts = pd.Timestamp(self.end)
        kline_dict: dict[str, pd.DataFrame] = {}

        if uses_rqalpha_price_source():
            t_rq = time.perf_counter()
            from dividend_lowvol_rotation.rqalpha.rqalpha_bundle_prices import (
                batch_load_klines_from_rqalpha,
            )
            from dividend_lowvol_rotation.config import RQALPHA_ADJUST_TYPE

            kline_dict = batch_load_klines_from_rqalpha(
                unique, self.start, self.end, adjust_type=RQALPHA_ADJUST_TYPE
            )
            if verbose:
                print(
                    f"  RQAlpha bundle 命中 {len(kline_dict)}/{total} 只"
                    f"（{time.perf_counter() - t_rq:.1f}s）"
                )
        elif duckdb_available():
            t_duck = time.perf_counter()
            kline_dict = _batch_load_klines_from_duckdb(
                unique, self.start, self.end, fq=self.kline_fq
            )
            if verbose:
                print(
                    f"  DuckDB 命中 {len(kline_dict)}/{total} 只"
                    f"（{time.perf_counter() - t_duck:.1f}s）"
                )

        still_missing = [
            c
            for c in unique
            if normalize_stock_code(c) not in kline_dict and c not in kline_dict
        ]

        if still_missing:
            csv_hit = 0
            for code in list(still_missing):
                path = _kline_cache_path(code, fq=self.kline_fq)
                if not path.exists():
                    continue
                try:
                    cached = pd.read_csv(path, parse_dates=["date"])
                except Exception:
                    continue
                if cached is None or cached.empty:
                    continue
                if not _cache_covers(cached, self.backtest_start, self.end, slack_days=30):
                    continue
                mask = (cached["date"] >= start_ts) & (cached["date"] <= end_ts)
                partial = cached.loc[mask].reset_index(drop=True)
                if partial.empty:
                    continue
                nc = normalize_stock_code(code)
                kline_dict[nc] = partial
                csv_hit += 1
                still_missing.remove(code)
            if verbose and csv_hit:
                print(f"  CSV 回退命中 {csv_hit} 只")

        still_missing = [
            c
            for c in unique
            if normalize_stock_code(c) not in kline_dict and c not in kline_dict
        ]
        if still_missing:
            if verbose:
                print(f"  仍缺 {len(still_missing)} 只，回退 stockdb…")
            kline_dict.update(
                _batch_fetch_klines_from_stockdb(
                    still_missing, self.start, self.end, fq=self.kline_fq
                )
            )

        loaded = 0
        for code in unique:
            nc = normalize_stock_code(code)
            kline = kline_dict.get(nc)
            if kline is None or kline.empty:
                kline = kline_dict.get(code)
            if kline is None or kline.empty:
                continue
            if "close" in kline.columns:
                kline = kline[["date", "close"]].copy()
            mask = (kline["date"] >= start_ts) & (kline["date"] <= end_ts)
            kline = kline.loc[mask].reset_index(drop=True)
            if kline.empty:
                continue
            path = _kline_cache_path(code, fq=self.kline_fq)
            if not path.exists():
                save_dataframe(path, kline)
            self._store_kline(code, kline)
            loaded += 1

        from dividend_lowvol_rotation.market_cap import batch_load_market_fields, market_fields_needed

        if market_fields_needed() and loaded:
            loaded_codes = [
                normalize_stock_code(c)
                for c in unique
                if normalize_stock_code(c) in self._klines or c in self._klines
            ]
            field_dict = batch_load_market_fields(
                loaded_codes, self.start, self.end, fields=("total_mv", "amount")
            )
            for code, fdf in field_dict.items():
                if fdf is None or fdf.empty:
                    continue
                nc = normalize_stock_code(code)
                self._market_fields[nc] = fdf
                if "total_mv" in fdf.columns:
                    self._total_mv[nc] = fdf[["date", "total_mv"]].copy()

        if verbose:
            print(f"  预加载完成，共 {loaded} 只")

    def ensure(self, codes: list[str]) -> None:
        missing = [c for c in dict.fromkeys(codes) if c not in self._klines]
        if missing:
            self.preload(missing, verbose=False)

    def price_at(self, code: str, as_of: pd.Timestamp) -> float | None:
        return self.metrics_at(code, as_of).get("price")

    def kline_df(self, code: str) -> pd.DataFrame | None:
        return self._klines.get(normalize_stock_code(code))

    def total_mv_at(self, code: str, as_of: pd.Timestamp) -> float | None:
        from dividend_lowvol_rotation.market_cap import total_mv_at_series

        nc = normalize_stock_code(code)
        mv_df = self._total_mv.get(nc)
        if mv_df is None:
            mv_df = self._total_mv.get(code)
        if mv_df is None:
            fdf = self._market_fields.get(nc)
            if fdf is None:
                fdf = self._market_fields.get(code)
            return total_mv_at_series(fdf, as_of)
        return total_mv_at_series(mv_df, as_of)

    def avg_amount_at(self, code: str, as_of: pd.Timestamp) -> float | None:
        from dividend_lowvol_rotation.market_cap import avg_amount_at_series

        nc = normalize_stock_code(code)
        fdf = self._market_fields.get(nc)
        if fdf is None:
            fdf = self._market_fields.get(code)
        return avg_amount_at_series(fdf, as_of, lookback_days=20)

    def metrics_at(self, code: str, as_of: pd.Timestamp) -> dict:
        metrics_df = self._metrics.get(code)
        if metrics_df is not None and not metrics_df.empty:
            out = metrics_from_precomputed(metrics_df, as_of)
        else:
            kline = self._klines.get(code)
            if kline is None or kline.empty:
                out = {"price": None, "ann_vol_pct": None, "low_n": None, "high_n": None}
            else:
                out = metrics_as_of(kline, as_of)
        mv = self.total_mv_at(code, as_of)
        if mv is not None:
            out["total_mv"] = mv
        amt = self.avg_amount_at(code, as_of)
        if amt is not None:
            out["avg_amount"] = amt
        return out


@dataclass
class BacktestContext:
    """预加载数据，供多次回测复用（WFA / 蒙特卡洛）。"""

    start: str
    end: str
    records: pd.DataFrame
    calendar: list[pd.Timestamp]
    store: KlineStore
    industry_df: pd.DataFrame
    risk_hist: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    dividend_year_index: object = None
    dividend_cash_records: pd.DataFrame | None = None
    split_records: pd.DataFrame | None = None
    market_pe_hist: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    _panel_cache: dict[tuple[str, int, str], pd.DataFrame] = field(default_factory=dict)
    _dividend_cache: dict[str, pd.DataFrame] = field(default_factory=dict)

    def _panel_cache_key(self, as_of: pd.Timestamp, prefetch_size: int) -> tuple[str, int, str]:
        from dividend_lowvol_rotation.config import panel_factor_cache_key

        return (as_of.date().isoformat(), int(prefetch_size), panel_factor_cache_key())

    def dividend_at(self, as_of: pd.Timestamp) -> pd.DataFrame:
        key = as_of.date().isoformat()
        if key not in self._dividend_cache:
            self._dividend_cache[key] = build_dividend_panel(records=self.records, as_of=as_of)
        return self._dividend_cache[key]

    def panel_at(self, as_of: pd.Timestamp, prefetch_size: int) -> pd.DataFrame:
        key = self._panel_cache_key(as_of, prefetch_size)
        if key not in self._panel_cache:
            self._panel_cache[key] = _build_panel_from_store(
                as_of,
                self.records,
                self.store,
                self.industry_df,
                prefetch_size,
                div_panel=self.dividend_at(as_of),
                risk_hist=self.risk_hist,
                div_index=self.dividend_year_index,
            )
        return self._panel_cache[key]

    def warm_panel_cache(
        self,
        dates: list[pd.Timestamp],
        prefetch_size: int,
        *,
        verbose: bool = False,
    ) -> None:
        need = [
            d
            for d in dates
            if self._panel_cache_key(d, prefetch_size) not in self._panel_cache
        ]
        if not need:
            return
        if verbose:
            print(f"预热候选池 panel {len(need)} 日…")
        for i, as_of in enumerate(need, 1):
            self.panel_at(as_of, prefetch_size)
            if verbose and (i == 1 or i == len(need) or i % 2 == 0):
                print(f"  panel 预热 {i}/{len(need)}：{as_of.date()}")


def prepare_backtest_context(
    start: str,
    end: str | None = None,
    *,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    rebalance_days: int = BACKTEST_REBALANCE_DAYS,
    rebalance_mode: str | None = None,
    reb_dates: list[pd.Timestamp] | None = None,
    verbose: bool = True,
) -> BacktestContext:
    from duckdb_cache import ensure_duckdb_cache_ready

    ensure_duckdb_cache_ready(verbose=verbose)
    end = end or date.today().isoformat()
    mode = rebalance_mode or BACKTEST_REBALANCE_MODE
    records = load_fhps_all_records(refresh=False, backtest_start=start)
    calendar = _trading_calendar(start, end)
    entry_anchor = pd.Timestamp(calendar[0]) if calendar else pd.Timestamp(start)
    reb_dates = reb_dates or resolve_rebalance_dates(
        calendar,
        mode=mode,
        rebalance_days=rebalance_days,
        entry_anchor=entry_anchor,
    )
    kline_start = (
        pd.Timestamp(start) - timedelta(days=max(PRICE_HISTORY_BUFFER_DAYS, 400))
    ).date().isoformat()
    store = KlineStore(kline_start, end, backtest_start=start, kline_fq=resolve_backtest_kline_fq())
    tmp_ctx = BacktestContext(
        start=start, end=end, records=records, calendar=calendar, store=store, industry_df=pd.DataFrame()
    )
    candidate_codes = _collect_candidate_codes(records, reb_dates, prefetch_size, ctx=tmp_ctx)
    from dividend_lowvol_rotation.config import BETA_BENCHMARK_CODE

    preload_codes = list(dict.fromkeys([*candidate_codes, BETA_BENCHMARK_CODE]))
    store.preload(preload_codes, verbose=verbose)
    dividend_cash_records = None
    split_records = None
    if uses_rqalpha_price_source() and BACKTEST_DIVIDEND_CASH:
        from dividend_lowvol_rotation.rqalpha.rqalpha_bundle_prices import (
            load_dividend_records_from_rqalpha,
            load_split_records_from_rqalpha,
        )

        dividend_cash_records = load_dividend_records_from_rqalpha(preload_codes)
        split_records = load_split_records_from_rqalpha(preload_codes)
        if verbose:
            n = len(dividend_cash_records) if dividend_cash_records is not None else 0
            print(f"  RQAlpha 分红记录 {n} 条（现金派息与引擎同源）")
            sn = len(split_records) if split_records is not None else 0
            if sn:
                print(f"  RQAlpha 送股记录 {sn} 条")
    industry_df = attach_industry(
        pd.DataFrame({"code": candidate_codes}), refresh=False
    )
    if verbose:
        print(f"加载排雷指标（{len(candidate_codes)} 只）…")
    t_risk = time.perf_counter()
    risk_hist = batch_load_risk_history(candidate_codes, refresh=False)
    if verbose:
        print(f"  排雷指标加载 {time.perf_counter() - t_risk:.1f}s")
    t_div = time.perf_counter()
    div_index = build_dividend_year_index(records)
    if verbose:
        print(f"  分红索引 {time.perf_counter() - t_div:.1f}s")
    market_pe_hist = pd.DataFrame()
    if MARKET_VALUATION_ENABLED:
        try:
            market_pe_hist = load_market_pe_history(start=kline_start, end=end)
            if verbose and not market_pe_hist.empty:
                print(f"  全市场 PE 序列：{len(market_pe_hist)} 日")
        except Exception as exc:
            if verbose:
                print(f"  全市场 PE 加载失败: {exc}")
    ctx = BacktestContext(
        start=start,
        end=end,
        records=records,
        calendar=calendar,
        store=store,
        industry_df=industry_df,
        risk_hist=risk_hist,
        dividend_year_index=div_index,
        market_pe_hist=market_pe_hist,
        dividend_cash_records=dividend_cash_records,
        split_records=split_records,
    )
    ctx.warm_panel_cache(reb_dates, prefetch_size, verbose=verbose)
    return ctx


def _resolve_price(
    code: str, panel: pd.DataFrame, as_of: pd.Timestamp, store: KlineStore
) -> float | None:
    if not panel.empty and "code" in panel.columns:
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
    # 日频回测候选池并集：按月采样即可覆盖分红批次变化，避免遍历全部交易日
    scan_dates = reb_dates
    if len(reb_dates) > 252:
        step = 21
        scan_dates = [reb_dates[i] for i in range(0, len(reb_dates), step)]
        if reb_dates[-1] not in scan_dates:
            scan_dates.append(reb_dates[-1])
    for as_of in scan_dates:
        div = ctx.dividend_at(as_of) if ctx else build_dividend_panel(records=records, as_of=as_of)
        if div.empty:
            continue
        div = div[~div["name"].map(is_excluded_name)]
        div = sort_dividend_prefetch(div)
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
    risk_hist: pd.DataFrame | None = None,
    div_index: object = None,
) -> pd.DataFrame:
    div = div_panel if div_panel is not None else build_dividend_panel(records=records, as_of=as_of)
    if div.empty:
        return pd.DataFrame()
    div = div[~div["name"].map(is_excluded_name)]
    div = sort_dividend_prefetch(div)
    codes = div["code"].head(prefetch_size).tolist()

    div_dedup = div.drop_duplicates(subset=["code"], keep="first").set_index("code")
    rows = []
    for code in codes:
        m = store.metrics_at(code, as_of)
        if m.get("price") is None or m.get("ann_vol_pct") is None:
            continue
        if code not in div_dedup.index:
            continue
        base = div_dedup.loc[code].to_dict()
        base["code"] = code
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
    panel = attach_risk_from_records(panel, records, as_of, div_index=div_index)
    if risk_hist is not None and not risk_hist.empty:
        panel = merge_risk_history(panel, risk_hist, as_of)
    panel = attach_enhanced_factors(
        panel,
        records=records,
        risk_hist=risk_hist,
        as_of=as_of,
        store=store,
        allow_network=False,
    )
    return panel


def _name_for(code: str, panel: pd.DataFrame, name_cache: dict[str, str]) -> str:
    row = panel[panel["code"] == code]
    if not row.empty and row["name"].iloc[0]:
        name = str(row["name"].iloc[0])
        name_cache[code] = name
        return name
    return name_cache.get(code, "")


def _execute_lot_sell(
    *,
    code: str,
    lot: PositionLot,
    as_of: pd.Timestamp,
    panel: pd.DataFrame,
    store,
    rank: int | None,
    reason: str,
    min_hold_days: int,
    cash: float,
    stock_stats: dict[str, StockStats],
    trade_rows: list[dict],
    trade_price_fn,
    lots: dict[str, PositionLot] | None = None,
) -> float:
    """全额卖出单票；若未成交则返回原 cash。"""
    if min_hold_days > 0 and (as_of - lot.buy_date).days < min_hold_days:
        return cash
    metrics = store.metrics_at(code, as_of)
    mkt_price = metrics.get("price")
    price = trade_price_fn(
        code,
        panel,
        as_of,
        "sell",
        metrics=metrics,
        trade_amount_cny=mkt_price * lot.shares if mkt_price else None,
    )
    if price is None or price <= 0:
        return cash
    proceeds = lot.shares * price
    fee, stamp, net = settle_sell(proceeds, as_of)
    realized = net - lot.cost_basis
    ret_pct = realized / lot.cost_basis * 100 if lot.cost_basis > 0 else None
    hold_days = (as_of - lot.buy_date).days
    st = stock_stats.setdefault(code, StockStats(code=code))
    st.name = lot.name or st.name
    st.sell_count += 1
    st.total_sell_amount += proceeds
    st.total_fees += fee + stamp
    st.realized_pnl += realized
    st.max_drawdown_pct = min(st.max_drawdown_pct, lot.max_drawdown_pct)
    st.holding_days += hold_days
    st.closed_lots += 1
    trade_rows.append(
        {
            "date": as_of.date().isoformat(),
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
    if lots is not None:
        lots.pop(code, None)
    return cash + net


def _record_partial_sell(
    *,
    code: str,
    lot: PositionLot,
    sell_shares: int,
    rb_date: pd.Timestamp,
    price: float,
    fee: float,
    stamp: float,
    net: float,
    realized: float,
    ret_pct: float | None,
    rank: int | None,
    reason: str,
    stock_stats: dict[str, StockStats],
    trade_rows: list[dict],
) -> None:
    hold_days = (rb_date - lot.buy_date).days
    st = stock_stats.setdefault(code, StockStats(code=code))
    st.name = lot.name or st.name
    st.sell_count += 1
    st.total_sell_amount += sell_shares * price
    st.total_fees += fee + stamp
    st.realized_pnl += realized
    st.max_drawdown_pct = min(st.max_drawdown_pct, lot.max_drawdown_pct)
    trade_rows.append(
        {
            "date": rb_date.date().isoformat(),
            "side": "卖出",
            "code": code,
            "name": lot.name,
            "price": round(price, 4),
            "shares": int(sell_shares),
            "amount": round(sell_shares * price, 2),
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


def _apply_index_dividend_rebalance(
    *,
    lots: dict[str, PositionLot],
    cash: float,
    buy_pool: pd.DataFrame,
    ranked: pd.DataFrame,
    panel: pd.DataFrame,
    store,
    rb_date: pd.Timestamp,
    top_n: int,
    position_scale: float,
    port_value: float,
    rank_map: dict,
    name_cache: dict[str, str],
    stock_stats: dict[str, StockStats],
    trade_rows: list[dict],
    min_hold_days: int,
    trade_price_fn,
    target_codes: list[str] | None = None,
) -> tuple[dict[str, PositionLot], float]:
    """年度调样：补足至 top_n + 按股息率加权再平衡。"""
    target_codes = target_codes or build_index_target_codes(
        list(lots.keys()), buy_pool, top_n, ranked=ranked
    )
    target_set = set(target_codes)
    if not target_codes:
        return lots, cash

    # 卖出不在目标组合内的持仓（防止超过 top_n）
    for code in sorted(lots.keys()):
        if code in target_set:
            continue
        lot = lots[code]
        metrics = store.metrics_at(code, rb_date)
        mkt_price = metrics.get("price") or store.price_at(code, rb_date)
        if not mkt_price or mkt_price <= 0:
            continue
        price = trade_price_fn(
            code,
            panel,
            rb_date,
            "sell",
            metrics=metrics,
            trade_amount_cny=mkt_price * lot.shares,
        )
        if price is None or price <= 0:
            continue
        proceeds = lot.shares * price
        fee, stamp, net = settle_sell(proceeds, rb_date)
        realized = net - lot.cost_basis
        ret_pct = realized / lot.cost_basis * 100 if lot.cost_basis > 0 else None
        _record_partial_sell(
            code=code,
            lot=lot,
            sell_shares=lot.shares,
            rb_date=rb_date,
            price=price,
            fee=fee,
            stamp=stamp,
            net=net,
            realized=realized,
            ret_pct=ret_pct,
            rank=rank_map.get(code),
            reason="调出目标组合",
            stock_stats=stock_stats,
            trade_rows=trade_rows,
        )
        st = stock_stats.setdefault(code, StockStats(code=code))
        st.holding_days += (rb_date - lot.buy_date).days
        st.closed_lots += 1
        cash += net
        del lots[code]

    weight_map = target_weights_for_portfolio(target_codes, ranked, panel)
    target_equity = port_value * position_scale

    # 先减持超重仓位
    for code in sorted(lots.keys()):
        if code not in weight_map:
            continue
        lot = lots[code]
        if min_hold_days > 0 and (rb_date - lot.buy_date).days < min_hold_days:
            continue
        metrics = store.metrics_at(code, rb_date)
        mkt_price = metrics.get("price") or store.price_at(code, rb_date)
        if not mkt_price or mkt_price <= 0:
            continue
        target_mv = target_equity * weight_map[code]
        current_mv = lot.shares * mkt_price
        if current_mv <= target_mv * 1.01:
            continue
        excess_mv = current_mv - target_mv
        sell_shares = int(excess_mv / mkt_price / LOT_SIZE) * LOT_SIZE
        if sell_shares < LOT_SIZE or sell_shares >= lot.shares:
            continue
        price = trade_price_fn(
            code,
            panel,
            rb_date,
            "sell",
            metrics=metrics,
            trade_amount_cny=sell_shares * mkt_price,
        )
        if price is None or price <= 0:
            continue
        proceeds = sell_shares * price
        fee, stamp, net = settle_sell(proceeds, rb_date)
        sold_cost = lot.cost_basis * (sell_shares / lot.shares)
        realized = net - sold_cost
        ret_pct = realized / sold_cost * 100 if sold_cost > 0 else None
        _record_partial_sell(
            code=code,
            lot=lot,
            sell_shares=sell_shares,
            rb_date=rb_date,
            price=price,
            fee=fee,
            stamp=stamp,
            net=net,
            realized=realized,
            ret_pct=ret_pct,
            rank=rank_map.get(code),
            reason="指数再平衡减持",
            stock_stats=stock_stats,
            trade_rows=trade_rows,
        )
        lot.shares -= sell_shares
        lot.cost_basis -= sold_cost
        cash += net
        st = stock_stats.setdefault(code, StockStats(code=code))
        if lot.shares <= 0:
            st.holding_days += (rb_date - lot.buy_date).days
            st.closed_lots += 1
            del lots[code]

    # 再买入欠配 / 新成分（按代码排序，保证现金分配顺序可复现）
    for code in sorted(target_codes):
        weight = weight_map.get(code)
        if not weight:
            continue
        metrics = store.metrics_at(code, rb_date)
        mkt_price = metrics.get("price") or store.price_at(code, rb_date)
        if not mkt_price or mkt_price <= 0:
            continue
        target_mv = target_equity * weight
        current_mv = lots[code].shares * mkt_price if code in lots else 0.0
        need_mv = target_mv - current_mv
        if need_mv < mkt_price * LOT_SIZE:
            continue
        budget = min(need_mv, cash)
        price = trade_price_fn(
            code,
            panel,
            rb_date,
            "buy",
            metrics=metrics,
            trade_amount_cny=budget,
        )
        if price is None or price <= 0:
            continue
        shares = max_buy_shares(budget, price)
        if shares <= 0:
            continue
        gross = shares * price
        fee = single_side_commission(gross)
        total_cost = gross + fee
        if total_cost > cash:
            continue
        name = _name_for(code, panel, name_cache)
        if code in lots:
            lot = lots[code]
            lot.shares += shares
            lot.cost_basis += total_cost
        else:
            lots[code] = PositionLot(
                code=code,
                name=name,
                shares=shares,
                buy_date=rb_date,
                buy_price=price,
                cost_basis=total_cost,
                buy_fee=fee,
                peak_price=price,
                prev_price=price,
            )
        st = stock_stats.setdefault(code, StockStats(code=code))
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
                "reason": "指数再平衡买入",
                "hold_days": None,
                "buy_price": round(price, 4),
                "buy_date": rb_date.date().isoformat(),
                "realized_pnl": None,
                "return_pct": None,
                "max_drawdown_pct": None,
            }
        )

    assert len(lots) <= top_n, f"持仓数 {len(lots)} 超过上限 {top_n}"
    return lots, cash


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
    strategy_params: StrategyParams | None = None,
    rebalance_mode: str | None = None,
    min_hold_days: int | None = None,
    sell_mode: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict, pd.DataFrame]:
    strategy_params = strategy_params or StrategyParams()
    start = start or default_start_years()
    end = end or date.today().isoformat()
    top_n = strategy_params.resolved_top_n(top_n)
    rebalance_days = strategy_params.resolved_rebalance_days(rebalance_days)
    mode = rebalance_mode or BACKTEST_REBALANCE_MODE
    min_hold_days = _resolve_min_hold_days(mode, min_hold_days)
    sell_mode = (sell_mode or SELL_MODE).lower()
    sell_rank = strategy_params.resolved_sell_rank(top_n) if sell_rank is None else sell_rank
    if apply_dividend_tax is None:
        apply_dividend_tax = DIVIDEND_TAX_ENABLED
    dividend_cash_mode = BACKTEST_DIVIDEND_CASH

    if ctx is None:
        ctx = prepare_backtest_context(
            start,
            end,
            prefetch_size=prefetch_size,
            rebalance_days=rebalance_days,
            rebalance_mode=mode,
            reb_dates=reb_dates_override,
            verbose=verbose,
        )
    elif verbose:
        print("复用已加载 K 线缓存…")

    records = ctx.records
    store = ctx.store
    industry_df = ctx.industry_df
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    # 复用 ctx 时按本次 start/end 截取日历（滚动窗口验证）
    if pd.Timestamp(ctx.start) == start_ts and pd.Timestamp(ctx.end) == end_ts:
        calendar = ctx.calendar
    else:
        calendar = [d for d in ctx.calendar if start_ts <= d <= end_ts]
        if not calendar:
            calendar = _trading_calendar(start, end)
    entry_anchor = pd.Timestamp(calendar[0]) if calendar else start_ts
    reb_dates = reb_dates_override or resolve_rebalance_dates(
        calendar,
        mode=mode,
        rebalance_days=rebalance_days,
        entry_anchor=entry_anchor,
    )

    lots: dict[str, PositionLot] = {}
    cash = float(initial_capital)
    name_cache: dict[str, str] = {}
    stock_stats: dict[str, StockStats] = {}
    watch_out_rank: dict[str, int] = {}
    watch_down_streak: dict[str, int] = {}

    nav_rows: list[dict] = []
    trade_rows: list[dict] = []
    holding_rows: list[dict] = []
    dividend_tax_rows: list[dict] = []
    total_dividend_tax = 0.0
    total_gross_dividend = 0.0

    div_records = ctx.dividend_cash_records if ctx.dividend_cash_records is not None else records
    div_index = (
        build_dividend_index(div_records) if (apply_dividend_tax or dividend_cash_mode) else {}
    )
    split_index = (
        build_split_index(ctx.split_records)
        if uses_rqalpha_price_source() and ctx.split_records is not None
        else {}
    )
    prev_rb: pd.Timestamp | None = None
    pool_vol_history: list[float] = []

    def _credit_period_dividends(period_end: pd.Timestamp) -> None:
        nonlocal cash, total_gross_dividend, total_dividend_tax
        if not lots or prev_rb is None:
            return
        if not (dividend_cash_mode or apply_dividend_tax):
            return
        tax, gross, rows = accrue_dividend_taxes(lots, div_index, prev_rb, period_end)
        if not rows:
            return
        total_gross_dividend += gross
        total_dividend_tax += tax
        dividend_tax_rows.extend(rows)
        if dividend_cash_mode:
            cash += gross - (tax if apply_dividend_tax else 0.0)
        elif apply_dividend_tax and tax > 0:
            cash -= tax

    use_payable_dividends = uses_rqalpha_price_source() and dividend_cash_mode

    def _apply_splits_on_date(as_of: pd.Timestamp) -> None:
        if not split_index or not lots:
            return
        apply_splits_on_date(lots, split_index, as_of)

    def _credit_dividends_on_date(as_of: pd.Timestamp) -> None:
        nonlocal cash, total_gross_dividend
        if not use_payable_dividends or not lots:
            return
        _tax, gross, rows = accrue_dividend_cash_on_date(
            lots,
            div_index,
            as_of,
            dividend_cash=True,
            apply_tax=False,
            use_payable_date=True,
        )
        if not rows:
            return
        total_gross_dividend += gross
        cash += gross

    def _pay_dividend_tax_on_date(as_of: pd.Timestamp) -> None:
        nonlocal cash, total_dividend_tax
        if not use_payable_dividends or not lots or not apply_dividend_tax:
            return
        tax, _gross, rows = accrue_dividend_cash_on_date(
            lots,
            div_index,
            as_of,
            dividend_cash=True,
            apply_tax=True,
            use_payable_date=True,
        )
        if tax <= 0:
            return
        total_dividend_tax += tax
        dividend_tax_rows.extend(rows)
        cash -= tax

    def _resolve_bear_vol_threshold() -> float:
        if not BEAR_VOL_USE_PERCENTILE or len(pool_vol_history) < BEAR_VOL_MIN_SAMPLES:
            return BEAR_VOL_THRESHOLD_PCT
        window = pool_vol_history[-BEAR_VOL_PERCENTILE_LOOKBACK:]
        return float(np.percentile(window, BEAR_VOL_PERCENTILE_THRESHOLD * 100))

    def _stats(code: str) -> StockStats:
        if code not in stock_stats:
            stock_stats[code] = StockStats(code=code)
        return stock_stats[code]

    def _trade_price(
        code: str,
        panel: pd.DataFrame,
        as_of: pd.Timestamp,
        side: str,
        *,
        metrics: dict | None = None,
        trade_amount_cny: float | None = None,
    ) -> float | None:
        if uses_rqalpha_price_source():
            from dividend_lowvol_rotation.rqalpha.rqalpha_bundle_prices import (
                is_suspended_on_date,
            )

            if is_suspended_on_date(code, as_of):
                return None
        raw = resolve_execution_raw_price(
            code, as_of, store, panel=panel, metrics=metrics
        )
        if raw is None or raw <= 0:
            return None
        vol = metrics.get("ann_vol_pct") if metrics else None
        if vol is None and not panel.empty and "code" in panel.columns:
            row = panel[panel["code"] == code]
            if not row.empty and "ann_vol_pct" in row.columns:
                vol = float(row["ann_vol_pct"].iloc[0])
        amount = trade_amount_cny
        if amount is None and metrics and metrics.get("price"):
            amount = float(metrics["price"]) * 5000
        return trade_execution_price(raw, side, ann_vol_pct=vol, trade_amount_cny=amount)

    for rb_date in reb_dates:
        _credit_dividends_on_date(rb_date)
        _apply_splits_on_date(rb_date)
        _pay_dividend_tax_on_date(rb_date)
        if not use_payable_dividends:
            _credit_period_dividends(rb_date)

        if lots:
            store.ensure(list(lots.keys()))

        if hold_only and lots:
            panel = ctx.panel_at(rb_date, prefetch_size) if not lots else pd.DataFrame()
        else:
            panel = ctx.panel_at(rb_date, prefetch_size)
        if panel.empty:
            if hold_only and lots:
                port_value = cash
                for code, lot in lots.items():
                    price = store.price_at(code, rb_date)
                    if price and price > 0:
                        lot.update_peak_drawdown(price)
                        port_value += lot.shares * price
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
            continue
        for _, r in panel.iterrows():
            name_cache[str(r["code"])] = str(r.get("name", ""))

        dynamic = resolve_dynamic_params(
            panel, as_of=rb_date, strategy_params=strategy_params, rebalance_mode=mode
        )
        if dynamic.market_vol_median_pct is not None:
            pool_vol_history.append(dynamic.market_vol_median_pct)

        bear_vol_thresh = _resolve_bear_vol_threshold()

        portfolio_vol = estimate_portfolio_vol_pct(lots, store, rb_date, panel) if lots else None
        position_scale, _scale_notes = resolve_position_scale(
            market_vol_median_pct=dynamic.market_vol_median_pct,
            panel=panel,
            portfolio_vol_pct=portfolio_vol,
        )
        effective_top_n = max(3, int(round(top_n * position_scale)))

        val_regime = {"valuation_tight": False, "pause_new_buys": False}
        if MARKET_VALUATION_ENABLED:
            val_regime = valuation_regime(rb_date, ctx.market_pe_hist)

        ranked, buy_pool, _stats_panel = run_screening(
            panel,
            top_n=effective_top_n,
            sell_rank=sell_rank,
            dynamic=dynamic,
            as_of=rb_date,
            strategy_params=strategy_params,
            valuation_tight=val_regime.get("valuation_tight", False),
            bear_vol_threshold_pct=bear_vol_thresh,
            rebalance_mode=mode,
        )
        if ranked.empty:
            continue

        rank_map = dict(zip(ranked["code"], ranked["rank"]))
        buy_codes = buy_pool["code"].tolist() if not buy_pool.empty else []
        if val_regime.get("pause_new_buys"):
            buy_codes = []

        equity_value = 0.0
        for code, lot in lots.items():
            px = _trade_price(code, panel, rb_date, "buy")
            if px is None or px <= 0:
                px = store.price_at(code, rb_date)
            if px and px > 0:
                equity_value += lot.shares * px
        port_value = cash + equity_value

        if hold_only and lots:
            # 买入持有对照：建仓后不再调仓
            pass
        else:
            # 卖出
            stop_loss_pct = resolve_stop_loss_pct(dynamic.market_vol_median_pct)
            atr_mult = STOP_ATR_MULTIPLIER
            if strategy_params and strategy_params.stop_atr_multiplier is not None:
                atr_mult = float(strategy_params.stop_atr_multiplier)
            retention_panel = panel
            if sell_mode == "index_rules" and lots:
                retention_panel = enrich_panel_with_holdings(
                    panel,
                    lots,
                    store=store,
                    records=records,
                    as_of=rb_date,
                    risk_hist=ctx.risk_hist,
                    div_index=ctx.dividend_year_index,
                )
            for code, lot in list(lots.items()):
                rank = rank_map.get(code)
                metrics = store.metrics_at(code, rb_date)
                mkt_price = metrics.get("price")
                if mkt_price and mkt_price > 0:
                    lot.update_peak_drawdown(mkt_price)
                    if lot.prev_price > 0 and mkt_price < lot.prev_price:
                        lot.down_streak += 1
                    else:
                        lot.down_streak = 0
                    lot.prev_price = mkt_price

                do_sell = False
                index_reason = ""
                if sell_mode == "index_rules":
                    do_sell, index_reason = should_sell_index_rules(code, retention_panel)
                else:
                    emergency_sell = False
                    stop_loss = False
                    if (
                        EMERGENCY_SELL_ENABLED
                        and mkt_price
                        and prev_rb is not None
                    ):
                        prev_px = store.price_at(code, prev_rb)
                        if prev_px and prev_px > 0:
                            day_drop = (mkt_price / prev_px - 1) * 100
                            if day_drop <= -EMERGENCY_SELL_DAILY_DROP_PCT:
                                emergency_sell = True
                            prev2_px = None
                            if len(reb_dates) > 1:
                                idx = reb_dates.index(rb_date) if rb_date in reb_dates else -1
                                if idx >= 2:
                                    prev2_px = store.price_at(code, reb_dates[idx - 2])
                            if prev2_px and prev2_px > 0:
                                cum_drop = (mkt_price / prev2_px - 1) * 100
                                if cum_drop <= -EMERGENCY_SELL_TWO_DAY_DROP_PCT:
                                    emergency_sell = True

                    momentum_sell = False
                    take_profit = False
                    trailing_sell = False
                    unrealized_pct = None
                    if mkt_price and lot.cost_basis > 0:
                        unrealized_pct = (mkt_price * lot.shares / lot.cost_basis - 1) * 100
                        if STOP_LOSS_ENABLED and unrealized_pct <= stop_loss_pct:
                            stop_loss = True
                        if (
                            STOP_ATR_ENABLED
                            and not stop_loss
                            and metrics.get("atr") is not None
                        ):
                            atr = float(metrics["atr"])
                            if atr > 0 and mkt_price < lot.buy_price - atr_mult * atr:
                                stop_loss = True

                    if TAKE_PROFIT_ENABLED and TAKE_PROFIT_STATIC_ENABLED and unrealized_pct is not None:
                        if unrealized_pct >= TAKE_PROFIT_PCT:
                            take_profit = True

                    if (
                        not take_profit
                        and TAKE_PROFIT_ENABLED
                        and TRAILING_STOP_ENABLED
                        and unrealized_pct is not None
                        and lot.peak_price > 0
                        and mkt_price
                    ):
                        if unrealized_pct >= TRAILING_STOP_ACTIVATION_PCT:
                            trail_pct = TRAILING_STOP_FROM_PEAK_PCT
                            if unrealized_pct >= TAKE_PROFIT_PCT:
                                trail_pct = TRAILING_STOP_EXTENDED_PCT
                            if mkt_price <= lot.peak_price * (1 - trail_pct / 100):
                                trailing_sell = True

                    if not take_profit and not trailing_sell and MOMENTUM_SELL_ENABLED:
                        ma200 = metrics.get("ma_200")
                        if (
                            mkt_price
                            and ma200
                            and mkt_price < ma200
                            and (rank is None or rank > MOMENTUM_SELL_RANK_THRESHOLD)
                        ):
                            momentum_sell = True

                    grace_hit = False
                    if GRACE_VOL_ADAPTIVE_ENABLED:
                        grace_days = resolve_grace_period_days(dynamic.market_vol_median_pct)
                    else:
                        grace_days = SELL_GRACE_PERIOD_DAYS
                        if dynamic.market_vol_median_pct is not None:
                            if dynamic.market_vol_median_pct >= GRACE_VOL_HIGH_THRESHOLD_PCT:
                                grace_days = GRACE_PERIOD_DAYS_HIGH_VOL
                            else:
                                grace_days = GRACE_PERIOD_DAYS_LOW_VOL

                    if (
                        not emergency_sell
                        and not stop_loss
                        and not momentum_sell
                        and not take_profit
                        and not trailing_sell
                    ):
                        if rank is not None and rank <= sell_rank:
                            watch_out_rank.pop(code, None)
                            watch_down_streak.pop(code, None)
                            continue
                        if SELL_GRACE_PERIOD_ENABLED:
                            if (
                                GRACE_REBOUND_RESET_ENABLED
                                and mkt_price
                                and lot.cost_basis > 0
                                and lot.shares > 0
                                and mkt_price * lot.shares >= lot.cost_basis
                            ):
                                watch_out_rank[code] = 0
                                watch_down_streak[code] = 0
                                continue
                            watch_out_rank[code] = watch_out_rank.get(code, 0) + 1
                            if lot.down_streak > 0:
                                watch_down_streak[code] = watch_down_streak.get(code, 0) + 1
                            else:
                                watch_down_streak[code] = 0
                            if watch_down_streak.get(code, 0) >= GRACE_EARLY_SELL_DOWN_DAYS:
                                grace_hit = True
                            elif watch_out_rank[code] < grace_days:
                                continue
                            else:
                                grace_hit = True
                        else:
                            grace_hit = True
                        watch_out_rank.pop(code, None)
                        watch_down_streak.pop(code, None)

                    do_sell = (
                        emergency_sell
                        or stop_loss
                        or momentum_sell
                        or take_profit
                        or trailing_sell
                        or grace_hit
                    )

                if not do_sell:
                    continue

                if min_hold_days > 0:
                    held = (rb_date - lot.buy_date).days
                    if held < min_hold_days:
                        continue

                price = _trade_price(
                    code,
                    panel,
                    rb_date,
                    "sell",
                    metrics=metrics,
                    trade_amount_cny=mkt_price * lot.shares if mkt_price else None,
                )
                if price is None or price <= 0:
                    continue
                proceeds = lot.shares * price
                fee, stamp, net = settle_sell(proceeds, rb_date)
                realized = net - lot.cost_basis
                ret_pct = realized / lot.cost_basis * 100 if lot.cost_basis > 0 else None
                hold_days = (rb_date - lot.buy_date).days
                st = _stats(code)
                st.name = lot.name or st.name
                st.sell_count += 1
                st.total_sell_amount += proceeds
                st.total_fees += fee + stamp
                st.realized_pnl += realized
                st.max_drawdown_pct = min(st.max_drawdown_pct, lot.max_drawdown_pct)
                st.holding_days += hold_days
                st.closed_lots += 1
                cash += net
                if sell_mode == "index_rules":
                    reason = index_reason
                elif emergency_sell:
                    reason = (
                        f"紧急止损(单日≥{EMERGENCY_SELL_DAILY_DROP_PCT:.0f}%"
                        f"或两日≥{EMERGENCY_SELL_TWO_DAY_DROP_PCT:.0f}%)"
                    )
                elif stop_loss:
                    if STOP_ATR_ENABLED and metrics.get("atr"):
                        reason = f"止损(ATR×{atr_mult:.2f}或≤{stop_loss_pct:.0f}%)"
                    else:
                        reason = f"止损≤{stop_loss_pct:.0f}%"
                elif take_profit:
                    reason = f"静态止盈≥{TAKE_PROFIT_PCT:.0f}%"
                elif trailing_sell:
                    trail_pct = TRAILING_STOP_EXTENDED_PCT
                    if unrealized_pct is not None and unrealized_pct < TAKE_PROFIT_PCT:
                        trail_pct = TRAILING_STOP_FROM_PEAK_PCT
                    reason = f"移动止盈（峰值回撤≥{trail_pct:.0f}%）"
                elif momentum_sell:
                    reason = f"跌破{MOMENTUM_SELL_MA_DAYS}日均线且排名>{MOMENTUM_SELL_RANK_THRESHOLD}"
                elif rank is None:
                    reason = "跌出缓冲带"
                elif grace_hit:
                    reason = f"排名{rank}>{sell_rank}(观察{grace_days}日)"
                else:
                    reason = f"排名{rank}>{sell_rank}"
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
                watch_out_rank.pop(code, None)

            # 买入 / 再平衡
            if INDEX_DIVIDEND_WEIGHTING and sell_mode == "index_rules":
                lots, cash = _apply_index_dividend_rebalance(
                    lots=lots,
                    cash=cash,
                    buy_pool=buy_pool,
                    ranked=ranked,
                    panel=panel,
                    store=store,
                    rb_date=rb_date,
                    top_n=top_n,
                    position_scale=position_scale,
                    port_value=port_value,
                    rank_map=rank_map,
                    name_cache=name_cache,
                    stock_stats=stock_stats,
                    trade_rows=trade_rows,
                    min_hold_days=min_hold_days,
                    trade_price_fn=_trade_price,
                )
            else:
                slots = effective_top_n - len(lots)
                new_codes = [c for c in buy_codes if c not in lots][:slots]
                target_equity = port_value * position_scale
                deploy_budget = max(0.0, target_equity - equity_value)
                max_per_stock = port_value * MAX_SINGLE_STOCK_WEIGHT
                if new_codes and deploy_budget > 0 and cash > 0:
                    if (
                        CONDITIONAL_REBUY_ENABLED
                        and position_scale < CONDITIONAL_REBUY_MIN_POSITION_SCALE
                    ):
                        new_codes = []
                if new_codes and deploy_budget > 0 and cash > 0:
                    per_slot = min(deploy_budget / len(new_codes), max_per_stock)
                    for code in new_codes:
                        metrics = store.metrics_at(code, rb_date)
                        price = _trade_price(
                            code,
                            panel,
                            rb_date,
                            "buy",
                            metrics=metrics,
                            trade_amount_cny=min(per_slot, cash),
                        )
                        if price is None or price <= 0:
                            continue
                        alloc = min(per_slot, cash, max_per_stock)
                        shares = max_buy_shares(alloc, price)
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
                            prev_price=price,
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
            price = resolve_execution_raw_price(code, rb_date, store, panel=panel)
            if price is None or price <= 0:
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

        # 调仓日间：index_rules 日间风控 + 逐日净值（准确 maxDD）
        rb_idx = reb_dates.index(rb_date)
        next_rb = (
            reb_dates[rb_idx + 1]
            if rb_idx + 1 < len(reb_dates)
            else pd.Timestamp(end)
        )
        inter_days = [d for d in calendar if rb_date < d < next_rb]
        daily_stop_loss_pct = resolve_stop_loss_pct(dynamic.market_vol_median_pct)
        daily_atr_mult = STOP_ATR_MULTIPLIER
        if strategy_params and strategy_params.stop_atr_multiplier is not None:
            daily_atr_mult = float(strategy_params.stop_atr_multiplier)
        use_daily_risk = (
            sell_mode == "index_rules"
            and INDEX_RULES_DAILY_RISK_ENABLED
            and not hold_only
        )
        use_stop = STOP_LOSS_ENABLED or use_daily_risk
        use_emergency = EMERGENCY_SELL_ENABLED or use_daily_risk

        if use_daily_risk and inter_days and lots:
            store.ensure(list(lots.keys()))

        for day_idx, day in enumerate(inter_days):
            _credit_dividends_on_date(day)
            _apply_splits_on_date(day)
            _pay_dividend_tax_on_date(day)
            if use_daily_risk and lots:
                prev_day = inter_days[day_idx - 1] if day_idx > 0 else rb_date
                prev2_day = inter_days[day_idx - 2] if day_idx >= 2 else None
                for code, lot in list(lots.items()):
                    metrics = store.metrics_at(code, day)
                    mkt_price = metrics.get("price")
                    if not mkt_price or mkt_price <= 0:
                        continue
                    lot.update_peak_drawdown(mkt_price)
                    if lot.prev_price > 0 and mkt_price < lot.prev_price:
                        lot.down_streak += 1
                    else:
                        lot.down_streak = 0
                    lot.prev_price = mkt_price

                    emergency_sell = False
                    stop_loss = False
                    if use_emergency:
                        prev_px = store.price_at(code, prev_day)
                        if prev_px and prev_px > 0:
                            day_drop = (mkt_price / prev_px - 1) * 100
                            if day_drop <= -EMERGENCY_SELL_DAILY_DROP_PCT:
                                emergency_sell = True
                        if not emergency_sell and prev2_day is not None:
                            prev2_px = store.price_at(code, prev2_day)
                            if prev2_px and prev2_px > 0:
                                cum_drop = (mkt_price / prev2_px - 1) * 100
                                if cum_drop <= -EMERGENCY_SELL_TWO_DAY_DROP_PCT:
                                    emergency_sell = True

                    unrealized_pct = None
                    if lot.cost_basis > 0:
                        unrealized_pct = (mkt_price * lot.shares / lot.cost_basis - 1) * 100
                        if use_stop and unrealized_pct <= daily_stop_loss_pct:
                            stop_loss = True
                        if (
                            use_stop
                            and STOP_ATR_ENABLED
                            and not stop_loss
                            and metrics.get("atr") is not None
                        ):
                            atr = float(metrics["atr"])
                            if atr > 0 and mkt_price < lot.buy_price - daily_atr_mult * atr:
                                stop_loss = True

                    if not (emergency_sell or stop_loss):
                        continue

                    if emergency_sell:
                        reason = (
                            f"紧急止损(单日≥{EMERGENCY_SELL_DAILY_DROP_PCT:.0f}%"
                            f"或两日≥{EMERGENCY_SELL_TWO_DAY_DROP_PCT:.0f}%)"
                        )
                    elif stop_loss:
                        if STOP_ATR_ENABLED and metrics.get("atr"):
                            reason = f"止损(ATR×{daily_atr_mult:.2f}或≤{daily_stop_loss_pct:.0f}%)"
                        else:
                            reason = f"止损≤{daily_stop_loss_pct:.0f}%"
                    else:
                        continue

                    cash = _execute_lot_sell(
                        code=code,
                        lot=lot,
                        as_of=day,
                        panel=pd.DataFrame(),
                        store=store,
                        rank=None,
                        reason=reason,
                        min_hold_days=min_hold_days,
                        cash=cash,
                        stock_stats=stock_stats,
                        trade_rows=trade_rows,
                        trade_price_fn=_trade_price,
                        lots=lots,
                    )

            day_value = cash
            for code, lot in lots.items():
                px = store.price_at(code, day)
                if px and px > 0:
                    day_value += lot.shares * px
            nav_rows.append(
                {
                    "date": day.date().isoformat(),
                    "nav": round(day_value, 2),
                    "cash": round(cash, 2),
                    "holdings_count": len(lots),
                    "return_pct": round((day_value / initial_capital - 1) * 100, 4),
                }
            )

        prev_rb = rb_date

    nav_df = pd.DataFrame(nav_rows)

    if lots and prev_rb is not None and (dividend_cash_mode or apply_dividend_tax) and not use_payable_dividends:
        end_ts = pd.Timestamp(end)
        if end_ts > prev_rb:
            row_count_before = len(dividend_tax_rows)
            _credit_period_dividends(end_ts)
            if len(dividend_tax_rows) > row_count_before and not nav_df.empty:
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
        "entry_anchor": entry_anchor.date().isoformat() if mode == "entry_anniversary" else None,
        "rebalance_days": rebalance_days,
        "rebalance_mode": mode,
        "min_hold_days": min_hold_days,
        "sell_mode": sell_mode,
        "index_style_ranking": INDEX_STYLE_RANKING,
        "index_dividend_weighting": INDEX_DIVIDEND_WEIGHTING,
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
        "dividend_cash_mode": dividend_cash_mode,
        "kline_fq": ctx.store.kline_fq or "none",
        "price_source": "rqalpha" if uses_rqalpha_price_source() else "duckdb",
        "slippage_rate": 0.0 if not execution_slippage_enabled() else SLIPPAGE_RATE,
        "execution_at_close": not execution_slippage_enabled(),
        "total_gross_dividend": round(total_gross_dividend, 2),
        "total_dividend_tax": round(total_dividend_tax, 2),
        "total_net_dividend": round(total_gross_dividend - total_dividend_tax, 2),
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
            sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
        dd = (nav_df["nav"] / nav_df["nav"].cummax() - 1).min()
        meta.update(
            {
                "final_nav": final_nav,
                "holdings_count": len(lots),
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
    parser.add_argument(
        "--rebalance-mode",
        choices=["monthly", "index_annual", "entry_anniversary", "quarterly_report", "fixed_days"],
        default=BACKTEST_REBALANCE_MODE,
        help="调仓日程：每月/指数年度/建仓周年/季报/固定 N 日",
    )
    parser.add_argument(
        "--sell-mode",
        choices=["rank_buffer", "index_rules"],
        default=SELL_MODE,
        help="调出逻辑：排名缓冲带（默认）或指数硬门槛不达标才卖",
    )
    parser.add_argument("--capital", type=float, default=BACKTEST_INITIAL_CAPITAL)
    parser.add_argument("--prefetch", type=int, default=BACKTEST_PREFETCH_SIZE, help="候选预筛数量")
    parser.add_argument("--no-dividend-tax", action="store_true", help="不扣分红个税（对比税前收益）")
    parser.add_argument("-o", "--output-dir", default=None, help="输出目录")
    args = parser.parse_args(argv)

    start = args.start or default_start_years(args.years)
    sell_rank = resolve_sell_rank(args.top, args.sell_rank)
    if args.rebalance_mode == "monthly":
        mode = "每月首个交易日"
    elif args.rebalance_mode == "index_annual":
        from dividend_lowvol_rotation.config import INDEX_ANNUAL_REBALANCE_TIMING

        mode = (
            "指数年度调仓(1月中旬首个交易日)"
            if INDEX_ANNUAL_REBALANCE_TIMING == "january"
            else "指数年度调仓(12月第二个周五次日)"
        )
    elif args.rebalance_mode == "entry_anniversary":
        mode = "建仓周年调仓（回测起点为建仓日）"
    elif args.rebalance_mode == "quarterly_report":
        mode = "季报截止后首个交易日"
    else:
        mode = f"每 {args.rebalance_days} 日调仓"
    print(f"回测 {start} ~ {args.end or '今'}，持仓 {args.top} 只，{mode}…")
    t0 = time.time()

    nav_df, trades_df, holdings_df, summary_df, meta, dividend_tax_df = run_backtest(
        start=start,
        end=args.end,
        top_n=args.top,
        sell_rank=sell_rank,
        rebalance_days=args.rebalance_days,
        rebalance_mode=args.rebalance_mode,
        initial_capital=args.capital,
        prefetch_size=args.prefetch,
        apply_dividend_tax=not args.no_dividend_tax,
        sell_mode=args.sell_mode,
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
