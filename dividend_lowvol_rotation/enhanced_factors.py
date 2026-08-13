# -*- coding: utf-8 -*-
"""增强因子：可持续股息率、利差分位陷阱、盈利动量、Beta、质量过滤。"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from data_cache import load_dataframe, save_dataframe
from dividend_lowvol_rotation.config import (
    BETA_BENCHMARK_CODE,
    BETA_LOOKBACK_DAYS,
    CACHE_DIR,
    DIVIDEND_COVERAGE_FILTER_ENABLED,
    ENH_PENALTY_COVERAGE,
    ENH_PENALTY_MOMENTUM,
    ENH_PENALTY_STABILITY,
    ENH_PENALTY_TRAP,
    EXPECTED_DIVIDEND_LOOKBACK_YEARS,
    MAX_PROFIT_CV,
    MIN_DIVIDEND_COVERAGE,
    PROFIT_MOMENTUM_FILTER_ENABLED,
    PROFIT_MOMENTUM_MIN_QOQ_POSITIVE,
    PROFIT_STABILITY_FILTER_ENABLED,
    SOFT_ENHANCED_SCORING_ENABLED,
    SUSTAINABLE_DIVIDEND_ENABLED,
    YIELD_SPREAD_LOOKBACK_DAYS,
    YIELD_SPREAD_PERCENTILE_ENABLED,
    YIELD_SPREAD_PERCENTILE_TRAP,
)
from dividend_lowvol_rotation.dynamic_params import _fetch_bond_yield_pct
from dividend_lowvol_rotation.symbols import normalize_stock_code


def _trimmed_mean(values: list[float], trim: float = 0.1) -> float | None:
    if not values:
        return None
    arr = np.array(sorted(values), dtype=float)
    if len(arr) == 1:
        return float(arr[0])
    k = int(len(arr) * trim)
    if k > 0 and len(arr) > 2 * k:
        arr = arr[k:-k]
    return float(np.mean(arr))


def compute_sustainable_dividend_yield_pct(
    *,
    price: float,
    eps: float | None,
    cash_per_share: float | None,
    payout_ratios: list[float],
    forward_eps: float | None = None,
) -> float | None:
    """可持续股息率 ≈ 修剪后平均支付率 × 预期 EPS / 价。"""
    if price is None or price <= 0:
        return None
    valid = [p for p in payout_ratios if p is not None and 10 <= p <= 100]
    avg_pay = _trimmed_mean(valid) if valid else None
    if avg_pay is None and cash_per_share and eps and eps > 0:
        avg_pay = cash_per_share / eps * 100.0
    fwd = forward_eps
    if fwd is None and eps and eps > 0:
        fwd = eps
    if avg_pay is None or fwd is None or fwd <= 0:
        return None
    return avg_pay / 100.0 * fwd / price * 100.0


def _batch_payout_ratios(
    records: pd.DataFrame,
    codes: list[str],
    as_of: pd.Timestamp,
    lookback_years: int = EXPECTED_DIVIDEND_LOOKBACK_YEARS,
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {c: [] for c in codes}
    if records is None or records.empty or not codes:
        return out
    sub = records.copy()
    sub["code"] = sub["code"].map(normalize_stock_code)
    sub = sub[sub["code"].isin(codes) & (sub["ex_date"] <= as_of)]
    if sub.empty:
        return out
    for code, grp in sub.groupby("code"):
        ratios: list[float] = []
        for year in range(as_of.year - lookback_years + 1, as_of.year + 1):
            yr = grp[grp["ex_date"].dt.year == year]
            if yr.empty:
                continue
            cash = pd.to_numeric(yr["cash_per_share"], errors="coerce").sum()
            eps_y = pd.to_numeric(yr.get("eps"), errors="coerce").dropna()
            if eps_y.empty:
                continue
            eps_val = float(eps_y.iloc[-1])
            if eps_val > 0 and cash > 0:
                ratios.append(cash / eps_val * 100.0)
        out[str(code)] = ratios
    return out


def _parse_quarterly_profits_from_fa(fa, *, as_of: pd.Timestamp | None = None) -> list[float]:
    if fa is None or fa.empty:
        return []
    sub = fa[fa["指标"] == "净利润"]
    if sub.empty:
        return []
    row = sub.iloc[0]
    vals: list[tuple[pd.Timestamp, float]] = []
    for col in fa.columns:
        if col in ("选项", "指标"):
            continue
        try:
            ts = pd.Timestamp(str(col))
            v = float(row[col])
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            if as_of is not None and ts > as_of:
                continue
            vals.append((ts, v))
    vals.sort(key=lambda x: x[0])
    return [v for _, v in vals[-8:]]


def cache_quarterly_profits_from_fa(code: str, fa) -> None:
    if fa is None or fa.empty:
        return
    sub = fa[fa["指标"] == "净利润"]
    if sub.empty:
        return
    row = sub.iloc[0]
    rows: list[dict] = []
    for col in fa.columns:
        if col in ("选项", "指标"):
            continue
        try:
            ts = pd.Timestamp(str(col))
            v = float(row[col])
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            rows.append({"report_date": ts.date().isoformat(), "net_profit": v})
    if not rows:
        return
    df = pd.DataFrame(rows).sort_values("report_date")
    path = CACHE_DIR / f"quarter_profit_{normalize_stock_code(code)}.csv"
    save_dataframe(path, df)


def _quarterly_net_profit_as_of(code: str, as_of: pd.Timestamp) -> list[float]:
    path = CACHE_DIR / f"quarter_profit_{normalize_stock_code(code)}.csv"
    cached = load_dataframe(path)
    if cached is None or cached.empty or "net_profit" not in cached.columns:
        return []
    if "report_date" in cached.columns:
        sub = cached.copy()
        sub["report_date"] = pd.to_datetime(sub["report_date"], errors="coerce")
        sub = sub[sub["report_date"].notna() & (sub["report_date"] <= as_of)]
        return pd.to_numeric(sub["net_profit"], errors="coerce").dropna().tolist()[-8:]
    # 旧缓存无日期：回测中不使用，避免未来信息
    return []


def _quarterly_net_profit_cached(code: str) -> list[float]:
    return _quarterly_net_profit_as_of(code, pd.Timestamp(date.today()))


def _quarterly_net_profit_fetch(code: str, *, as_of: pd.Timestamp | None = None) -> list[float]:
    as_of = as_of or pd.Timestamp(date.today())
    cached = _quarterly_net_profit_as_of(code, as_of)
    if cached:
        return cached
    import akshare as ak

    try:
        fa = ak.stock_financial_abstract(symbol=normalize_stock_code(code))
    except Exception:
        return []
    cache_quarterly_profits_from_fa(code, fa)
    return _quarterly_net_profit_as_of(code, as_of)


def _batch_quarterly_profits(
    codes: list[str],
    *,
    as_of: pd.Timestamp,
    allow_network: bool,
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for code in codes:
        cached = _quarterly_net_profit_as_of(code, as_of)
        if cached:
            out[code] = cached
        elif allow_network:
            out[code] = _quarterly_net_profit_fetch(code, as_of=as_of)
        else:
            out[code] = []
    return out


def profit_momentum_ok(quarterly_profits: list[float]) -> bool:
    if len(quarterly_profits) < 4:
        return True
    q = quarterly_profits[-4:]
    changes = [q[i] - q[i - 1] for i in range(1, len(q))]
    positive = sum(1 for c in changes if c >= 0)
    if positive >= PROFIT_MOMENTUM_MIN_QOQ_POSITIVE:
        return True
    if q[-1] >= q[0] * 0.95:
        return True
    return False


def profit_momentum_ok_with_fallback(
    quarterly_profits: list[float],
    annual_profits: list[float],
) -> bool:
    if len(quarterly_profits) >= 4:
        return profit_momentum_ok(quarterly_profits)
    if len(annual_profits) >= 2:
        return annual_profits[-1] >= annual_profits[-2] * 0.85
    return True


def profit_stability_ok(annual_profits: list[float]) -> bool:
    if len(annual_profits) < 3:
        return True
    arr = np.array(annual_profits[-3:], dtype=float)
    if not np.all(arr > 0):
        return False
    mean = float(np.mean(arr))
    if mean <= 0:
        return False
    cv = float(np.std(arr) / mean)
    return cv <= MAX_PROFIT_CV


def _risk_hist_by_code(risk_hist: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    if risk_hist is None or risk_hist.empty:
        return {}
    sub = risk_hist.copy()
    sub["code"] = sub["code"].map(normalize_stock_code)
    return {str(k): g.sort_values("report_year") for k, g in sub.groupby("code")}


def _annual_profits_from_hist(hist: pd.DataFrame | None, *, as_of: pd.Timestamp | None = None) -> list[float]:
    if hist is None or hist.empty or "net_profit" not in hist.columns:
        return []
    sub = hist
    if as_of is not None and "report_year" in sub.columns:
        sub = sub[sub["report_year"] <= as_of.year]
    return pd.to_numeric(sub["net_profit"], errors="coerce").dropna().tolist()


def _risk_row_as_of(hist: pd.DataFrame | None, as_of: pd.Timestamp) -> pd.Series | None:
    if hist is None or hist.empty or "report_year" not in hist.columns:
        return None
    sub = hist[hist["report_year"] <= as_of.year]
    if sub.empty:
        return None
    return sub.sort_values("report_year").iloc[-1]


def _dividend_cash_by_code_year(
    records: pd.DataFrame,
    codes: list[str],
    as_of: pd.Timestamp,
) -> dict[str, float]:
    out: dict[str, float] = {}
    if records is None or records.empty:
        return out
    sub = records.copy()
    sub["code"] = sub["code"].map(normalize_stock_code)
    yr = as_of.year
    sub = sub[
        sub["code"].isin(codes)
        & (sub["ex_date"].dt.year == yr)
        & (sub["ex_date"] <= as_of)
    ]
    if sub.empty:
        return out
    sums = sub.groupby("code")["cash_per_share"].apply(
        lambda s: float(pd.to_numeric(s, errors="coerce").sum())
    )
    return {str(k): v for k, v in sums.items() if v > 0}


def dividend_coverage_ok_row(
    *,
    code: str,
    risk_row: pd.Series | None,
    div_cash: float | None,
) -> bool:
    if not DIVIDEND_COVERAGE_FILTER_ENABLED:
        return True
    if risk_row is None or div_cash is None or div_cash <= 0:
        return True
    ocf_ratio = pd.to_numeric(risk_row.get("ocf_to_profit"), errors="coerce")
    net_profit = pd.to_numeric(risk_row.get("net_profit"), errors="coerce")
    if pd.isna(ocf_ratio) or pd.isna(net_profit) or net_profit <= 0:
        return True
    ocf = float(ocf_ratio) * float(net_profit)
    return ocf / div_cash >= MIN_DIVIDEND_COVERAGE


def yield_spread_percentile_from_kline(
    kline: pd.DataFrame,
    as_of: pd.Timestamp,
    cash_per_share: float,
    bond_yield_pct: float | None,
    lookback_days: int = YIELD_SPREAD_LOOKBACK_DAYS,
) -> float | None:
    if bond_yield_pct is None or cash_per_share <= 0 or kline is None or kline.empty:
        return None
    sub = kline[kline["date"] <= as_of].tail(lookback_days + 5)
    if len(sub) < 60:
        return None
    yields = cash_per_share / pd.to_numeric(sub["close"], errors="coerce") * 100.0
    yields = yields.replace([np.inf, -np.inf], np.nan).dropna()
    if yields.empty:
        return None
    spreads = yields - bond_yield_pct
    current = float(yields.iloc[-1] - bond_yield_pct)
    return float((spreads <= current).mean() * 100.0)


def _benchmark_daily_returns_from_kline(
    kline: pd.DataFrame | None,
    end: pd.Timestamp,
    lookback: int,
) -> pd.Series | None:
    if kline is None or kline.empty:
        return None
    k = kline.sort_values("date")
    k = k[k["date"] <= end]
    if len(k) < 60:
        return None
    ret = k.set_index("date")["close"].astype(float).pct_change().dropna()
    return ret.tail(lookback + 30)


@lru_cache(maxsize=64)
def _benchmark_daily_returns(
    benchmark: str,
    end: str,
    lookback: int,
) -> pd.Series | None:
    """基准日收益率序列（按日期索引），每个 as_of 只加载一次。"""
    from dividend_lowvol_rotation.prices import load_kline_history

    end_ts = pd.Timestamp(end)
    start = (end_ts - pd.Timedelta(days=lookback * 2 + 60)).date().isoformat()
    k = load_kline_history(benchmark, start, end)
    return _benchmark_daily_returns_from_kline(k, end_ts, lookback)


def compute_beta_from_kline(
    kline: pd.DataFrame,
    as_of: pd.Timestamp,
    bench_ret: pd.Series | None,
    lookback: int = BETA_LOOKBACK_DAYS,
) -> float | None:
    """按交易日对齐计算 Beta（修复原先按行号对齐的错误）。"""
    if kline is None or kline.empty or bench_ret is None or bench_ret.empty:
        return None
    sub = kline[kline["date"] <= as_of].tail(lookback + 30)
    if len(sub) < 60:
        return None
    stock_ret = sub.set_index("date")["close"].astype(float).pct_change()
    aligned = pd.concat([stock_ret, bench_ret], axis=1, join="inner").dropna()
    aligned.columns = ["s", "b"]
    aligned = aligned.tail(lookback)
    if len(aligned) < 40:
        return None
    cov = np.cov(aligned["s"], aligned["b"])
    if cov[1, 1] <= 0:
        return None
    return float(cov[0, 1] / cov[1, 1])


def compute_beta(
    *,
    store,
    code: str,
    as_of: pd.Timestamp,
    benchmark: str = BETA_BENCHMARK_CODE,
    lookback: int = BETA_LOOKBACK_DAYS,
) -> float | None:
    kline = store.kline_df(code) if store is not None else None
    bench_ret = _benchmark_daily_returns(benchmark, as_of.date().isoformat(), lookback)
    return compute_beta_from_kline(kline, as_of, bench_ret, lookback)


def attach_enhanced_factors(
    panel: pd.DataFrame,
    *,
    records: pd.DataFrame,
    risk_hist: pd.DataFrame | None,
    as_of: pd.Timestamp,
    store=None,
    bond_yield_pct: float | None = None,
    allow_network: bool = True,
) -> pd.DataFrame:
    if panel.empty:
        return panel
    if bond_yield_pct is None:
        from dividend_lowvol_rotation.dynamic_params import bond_yield_pct_as_of

        bond_yield_pct = bond_yield_pct_as_of(as_of)

    out = panel.copy()
    codes = out["code"].map(normalize_stock_code).tolist()
    payout_map = _batch_payout_ratios(records, codes, as_of)
    risk_by_code = _risk_hist_by_code(risk_hist)
    div_cash_map = _dividend_cash_by_code_year(records, codes, as_of)
    qprofit_map = (
        _batch_quarterly_profits(codes, as_of=as_of, allow_network=allow_network)
        if PROFIT_MOMENTUM_FILTER_ENABLED
        else {}
    )

    bench_ret = None
    if store is not None:
        bench_kline = store.kline_df(BETA_BENCHMARK_CODE)
        bench_ret = _benchmark_daily_returns_from_kline(bench_kline, as_of, BETA_LOOKBACK_DAYS)
        if bench_ret is None:
            bench_ret = _benchmark_daily_returns(
                BETA_BENCHMARK_CODE, as_of.date().isoformat(), BETA_LOOKBACK_DAYS
            )

    n = len(out)
    sustain: list[float | None] = [None] * n
    spread_pcts: list[float | None] = [None] * n
    betas: list[float | None] = [None] * n
    profit_mom_ok: list[bool] = [True] * n
    profit_stab_ok: list[bool] = [True] * n
    div_cov_ok: list[bool] = [True] * n
    trap_flags: list[bool] = [False] * n

    prices = pd.to_numeric(out["price"], errors="coerce").to_numpy()
    eps_arr = (
        pd.to_numeric(out["eps"], errors="coerce").to_numpy()
        if "eps" in out.columns
        else np.full(n, np.nan)
    )
    cash_arr = pd.to_numeric(out["cash_per_share"], errors="coerce").to_numpy()

    for i, code in enumerate(codes):
        price = prices[i]
        eps = eps_arr[i]
        cash = cash_arr[i]
        payouts = payout_map.get(code, [])
        qprofits = qprofit_map.get(code, [])
        hist = risk_by_code.get(code)
        annual_profits = _annual_profits_from_hist(hist, as_of=as_of)

        forward_eps = float(np.sum(qprofits[-4:])) if len(qprofits) >= 4 else None
        if SUSTAINABLE_DIVIDEND_ENABLED and pd.notna(price) and float(price) > 0:
            sustain[i] = compute_sustainable_dividend_yield_pct(
                price=float(price),
                eps=float(eps) if pd.notna(eps) else None,
                cash_per_share=float(cash) if pd.notna(cash) else None,
                payout_ratios=payouts,
                forward_eps=forward_eps,
            )

        if (
            YIELD_SPREAD_PERCENTILE_ENABLED
            and store is not None
            and pd.notna(cash)
            and float(cash) > 0
        ):
            kline = store.kline_df(code)
            sp_pct = yield_spread_percentile_from_kline(
                kline,
                as_of,
                float(cash),
                bond_yield_pct,
            )
            spread_pcts[i] = sp_pct
            if sp_pct is not None and sp_pct >= YIELD_SPREAD_PERCENTILE_TRAP:
                trap_flags[i] = True

        if store is not None:
            betas[i] = compute_beta_from_kline(store.kline_df(code), as_of, bench_ret)

        if PROFIT_MOMENTUM_FILTER_ENABLED:
            profit_mom_ok[i] = profit_momentum_ok_with_fallback(qprofits, annual_profits)

        if PROFIT_STABILITY_FILTER_ENABLED:
            profit_stab_ok[i] = profit_stability_ok(annual_profits)

        risk_row = _risk_row_as_of(hist, as_of)
        div_cov_ok[i] = dividend_coverage_ok_row(
            code=code,
            risk_row=risk_row,
            div_cash=div_cash_map.get(code),
        )

    out["sustainable_div_yield_pct"] = sustain
    out["yield_spread_percentile"] = spread_pcts
    out["yield_trap_flag"] = trap_flags
    out["beta_252"] = betas
    out["profit_momentum_ok"] = profit_mom_ok
    out["profit_stability_ok"] = profit_stab_ok
    out["dividend_coverage_ok"] = div_cov_ok
    return out


def enhanced_score_penalties(df: pd.DataFrame, *, skip: dict[str, bool] | None = None) -> pd.Series:
    """增强因子评分扣减（越大越差）。"""
    skip = skip or {}
    penalty = pd.Series(0.0, index=df.index, dtype=float)
    if df.empty:
        return penalty
    if not skip.get("trap") and YIELD_SPREAD_PERCENTILE_ENABLED and "yield_trap_flag" in df.columns:
        trap = df["yield_trap_flag"].fillna(False)
        sp = pd.to_numeric(df.get("yield_spread_percentile"), errors="coerce")
        excess = (sp - YIELD_SPREAD_PERCENTILE_TRAP).clip(lower=0) / 10.0
        penalty = penalty + trap.astype(float) * (ENH_PENALTY_TRAP + excess.fillna(0))
    if (
        not skip.get("momentum")
        and PROFIT_MOMENTUM_FILTER_ENABLED
        and "profit_momentum_ok" in df.columns
    ):
        bad = ~df["profit_momentum_ok"].fillna(True)
        penalty = penalty + bad.astype(float) * ENH_PENALTY_MOMENTUM
    if (
        not skip.get("stability")
        and PROFIT_STABILITY_FILTER_ENABLED
        and "profit_stability_ok" in df.columns
    ):
        bad = ~df["profit_stability_ok"].fillna(True)
        penalty = penalty + bad.astype(float) * ENH_PENALTY_STABILITY
    if (
        not skip.get("coverage")
        and DIVIDEND_COVERAGE_FILTER_ENABLED
        and "dividend_coverage_ok" in df.columns
    ):
        bad = ~df["dividend_coverage_ok"].fillna(True)
        penalty = penalty + bad.astype(float) * ENH_PENALTY_COVERAGE
    return penalty


def enhanced_filter_mask(df: pd.DataFrame, *, skip: dict[str, bool] | None = None) -> pd.Series:
    """增强因子过滤；软性模式下不过滤，由 enhanced_score_penalties 扣分。"""
    skip = skip or {}
    if SOFT_ENHANCED_SCORING_ENABLED:
        return pd.Series(True, index=df.index)
    ok = pd.Series(True, index=df.index)
    if (
        not skip.get("momentum")
        and PROFIT_MOMENTUM_FILTER_ENABLED
        and "profit_momentum_ok" in df.columns
    ):
        ok &= df["profit_momentum_ok"].fillna(True)
    if (
        not skip.get("stability")
        and PROFIT_STABILITY_FILTER_ENABLED
        and "profit_stability_ok" in df.columns
    ):
        ok &= df["profit_stability_ok"].fillna(True)
    if (
        not skip.get("coverage")
        and DIVIDEND_COVERAGE_FILTER_ENABLED
        and "dividend_coverage_ok" in df.columns
    ):
        ok &= df["dividend_coverage_ok"].fillna(True)
    if (
        not skip.get("trap")
        and YIELD_SPREAD_PERCENTILE_ENABLED
        and "yield_trap_flag" in df.columns
    ):
        ok &= ~df["yield_trap_flag"].fillna(False)
    return ok
