"""Point-in-time replication of the CSI Dividend Low Volatility index (H30269).

The implementation intentionally contains no tunable factor blend.  It follows the
public CSI methodology as checked on 2026-08-27: CSI All Share eligibility, three
consecutive cash-dividend years, size/liquidity top 80%, payout/dividend-growth
exclusions, dividend-yield top 75, one-year volatility bottom 50, dividend-yield
weighting with a 15% cap, and the annual December review buffer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
RAW_GLOB = ROOT / "data/parquet/stock_daily/year=*/*.parquet"
QFQ_GLOB = ROOT / "data/parquet/stock_daily_qfq/year=*/*.parquet"
SECURITY_PATH = ROOT / "data/parquet/stock_meta/securities.parquet"
DIVIDEND_PATH = ROOT / "data/parquet/stock_dividend/dividend_events.parquet"
FHPS_PATH = ROOT / "cache/dividend_lowvol/fhps_all_records.csv"
CALENDAR_PATH = ROOT / "data/parquet/trade_calendar/calendar.parquet"


@dataclass(frozen=True)
class RebalanceSnapshot:
    signal_date: pd.Timestamp
    effective_date: pd.Timestamp
    candidates: pd.DataFrame
    pure_selection: tuple[str, ...]
    final_selection: tuple[str, ...]
    weights: pd.Series
    stage_counts: dict[str, int]
    buffer_dependency: float
    exceptions: tuple[dict[str, str], ...]


def second_friday(year: int, month: int = 12) -> pd.Timestamp:
    """Return the second calendar Friday of ``month``."""
    first = pd.Timestamp(year=year, month=month, day=1)
    offset = (4 - first.weekday()) % 7
    return first + pd.Timedelta(days=offset + 7)


def capped_proportional_weights(values: pd.Series, cap: float = 0.15) -> pd.Series:
    """Proportionally allocate positive values while strictly respecting ``cap``."""
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    clean = clean.fillna(0.0).clip(lower=0.0)
    if clean.empty or float(clean.sum()) <= 0.0:
        raise ValueError("股息率权重输入必须至少包含一个正值")
    if cap <= 0.0 or cap * len(clean) < 1.0 - 1e-12:
        raise ValueError("单股上限与持仓数量无法满足满仓约束")
    out = pd.Series(0.0, index=clean.index, dtype=float)
    active = list(clean.index)
    remaining = 1.0
    while active:
        raw = clean.loc[active]
        allocation = raw / float(raw.sum()) * remaining
        breached = allocation[allocation > cap + 1e-12]
        if breached.empty:
            out.loc[active] = allocation
            break
        out.loc[breached.index] = cap
        remaining -= cap * len(breached)
        active = [symbol for symbol in active if symbol not in set(breached.index)]
    out /= float(out.sum())
    if float(out.max()) > cap + 1e-10:
        raise RuntimeError("权重再分配后仍突破单股上限")
    return out


class DividendLowVolBaseline:
    """Build official-rule annual H30269 replication snapshots from local PIT data."""

    def __init__(self, params: Mapping[str, Any] | None = None):
        self.params = dict(params or {})
        self.tax_factor = float(self.params.get("after_tax_factor", 0.90))
        self.top_dividend = int(self.params.get("dividend_top_n", 75))
        self.top_volatility = int(self.params.get("volatility_top_n", 50))
        self.weight_cap = float(self.params.get("max_single_weight", 0.15))
        self._securities = self._load_securities()
        self._dividends = self._load_dividends()
        self._fhps = self._load_fhps()

    @staticmethod
    def _load_securities() -> pd.DataFrame:
        frame = pd.read_parquet(SECURITY_PATH)
        frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
        frame = frame[
            frame["symbol"].str.fullmatch(r"\d{6}")
            & frame["exchange"].isin(["XSHG", "XSHE"])
        ].copy()
        frame["listed_date"] = pd.to_datetime(frame["listed_date"], errors="coerce")
        frame["de_listed_date"] = pd.to_datetime(
            frame["de_listed_date"].replace("0000-00-00", pd.NA), errors="coerce"
        )
        return frame.drop_duplicates("symbol", keep="last").set_index("symbol")

    @staticmethod
    def _load_dividends() -> pd.DataFrame:
        raw = pd.read_parquet(DIVIDEND_PATH)
        lot = pd.to_numeric(raw["round_lot"], errors="coerce").replace(0, np.nan).fillna(10.0)
        frame = pd.DataFrame(
            {
                "symbol": raw["symbol"].astype(str).str.zfill(6),
                "ex_date": pd.to_datetime(
                    raw["ex_dividend_date"].astype("Int64").astype(str).str[:8],
                    format="%Y%m%d",
                    errors="coerce",
                ),
                "cash_per_share": pd.to_numeric(
                    raw["dividend_cash_before_tax"], errors="coerce"
                ) / lot,
            }
        )
        frame = frame[frame["cash_per_share"].gt(0)].dropna()
        return frame.drop_duplicates(["symbol", "ex_date", "cash_per_share"])

    @staticmethod
    def _load_fhps() -> pd.DataFrame:
        if not FHPS_PATH.is_file():
            return pd.DataFrame(
                columns=["symbol", "ex_date", "report_year", "cash_per_share", "eps"]
            )
        raw = pd.read_csv(FHPS_PATH, dtype={"code": str})
        frame = pd.DataFrame(
            {
                "symbol": raw["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6),
                "ex_date": pd.to_datetime(raw["ex_date"], errors="coerce"),
                "report_year": pd.to_numeric(raw["report_date"], errors="coerce") // 10000,
                "cash_per_share": pd.to_numeric(raw["cash_per_share"], errors="coerce"),
                "eps": pd.to_numeric(raw["eps"], errors="coerce"),
            }
        ).dropna(subset=["symbol", "ex_date", "report_year"])
        frame["report_year"] = frame["report_year"].astype(int)
        return frame.drop_duplicates(
            ["symbol", "ex_date", "report_year", "cash_per_share"], keep="last"
        )

    @staticmethod
    def calendar(start: str | pd.Timestamp, end: str | pd.Timestamp) -> pd.DatetimeIndex:
        frame = pd.read_parquet(CALENDAR_PATH, columns=["trade_date"])
        dates = pd.DatetimeIndex(pd.to_datetime(frame["trade_date"], errors="coerce").dropna())
        return dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))].sort_values().unique()

    def schedule(self, start_year: int, end_year: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        calendar = self.calendar(f"{start_year}-12-01", f"{end_year}-12-31")
        result: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        for year in range(start_year, end_year + 1):
            review = second_friday(year)
            before = calendar[calendar <= review]
            after = calendar[calendar > review]
            if len(before) and len(after):
                result.append((pd.Timestamp(before[-1]), pd.Timestamp(after[0])))
        return result

    def _market_statistics(self, signal_date: pd.Timestamp) -> pd.DataFrame:
        lower = (signal_date - pd.Timedelta(days=370)).date().isoformat()
        upper = signal_date.date().isoformat()
        raw_path = RAW_GLOB.resolve().as_posix().replace("'", "''")
        qfq_path = QFQ_GLOB.resolve().as_posix().replace("'", "''")
        query = f"""
        WITH raw AS (
          SELECT try_cast(trade_date AS DATE) trade_date, symbol,
                 max(close) AS close_px, max(total_mv) AS total_mv, max(amount) AS amount,
                 max(is_st) AS is_st, max(pe_ttm) AS pe_ttm
          FROM read_parquet('{raw_path}', hive_partitioning=true, union_by_name=true)
          WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{lower}' AND DATE '{upper}'
          GROUP BY 1,2
        ), raw_stats AS (
          SELECT symbol, avg(total_mv) avg_total_mv, avg(amount) avg_amount,
                 arg_max(close_px, trade_date) signal_close,
                 arg_max(is_st, trade_date) is_st,
                 arg_max(pe_ttm, trade_date) pe_ttm,
                 count(*) observations
          FROM raw GROUP BY symbol
        ), qfq AS (
          SELECT try_cast(trade_date AS DATE) trade_date, symbol, max(close) AS close_px
          FROM read_parquet('{qfq_path}', hive_partitioning=true, union_by_name=true)
          WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{lower}' AND DATE '{upper}'
          GROUP BY 1,2
        ), returns AS (
          SELECT trade_date, symbol, ln(close_px / lag(close_px) OVER (PARTITION BY symbol ORDER BY trade_date)) ret
          FROM qfq WHERE close_px > 0
        ), vol AS (
          SELECT symbol, stddev_samp(ret) * sqrt(252.0) volatility_1y,
                 count(ret) return_observations
          FROM returns WHERE trade_date > DATE '{lower}' GROUP BY symbol
        )
        SELECT r.*, v.volatility_1y, v.return_observations
        FROM raw_stats r LEFT JOIN vol v USING(symbol)
        """
        with duckdb.connect() as con:
            con.execute("SET threads TO 4")
            return con.execute(query).fetch_df().set_index("symbol")

    def _dividend_statistics(self, signal_date: pd.Timestamp, close: pd.Series) -> pd.DataFrame:
        years = [signal_date.year - 3, signal_date.year - 2, signal_date.year - 1]
        events = self._dividends[
            self._dividends["ex_date"].le(signal_date)
            & self._dividends["ex_date"].dt.year.isin(years)
        ].copy()
        events["year"] = events["ex_date"].dt.year
        annual = events.pivot_table(
            index="symbol", columns="year", values="cash_per_share", aggfunc="sum"
        ).reindex(columns=years)
        annual_after_tax = annual * self.tax_factor
        out = pd.DataFrame(index=close.index)
        out["continuous_dividend_3y"] = annual_after_tax.reindex(out.index).gt(0).all(axis=1)
        out["average_after_tax_dividend_yield_3y"] = (
            annual_after_tax.mean(axis=1).reindex(out.index) / close.replace(0, np.nan)
        )
        out["last_after_tax_dividend_yield"] = (
            annual_after_tax[years[-1]].reindex(out.index) / close.replace(0, np.nan)
        )

        fiscal_years = years
        fhps = self._fhps[
            self._fhps["ex_date"].le(signal_date)
            & self._fhps["report_year"].isin(fiscal_years)
        ].copy()
        dps = fhps.pivot_table(
            index="symbol", columns="report_year", values="cash_per_share", aggfunc="sum"
        ).reindex(columns=fiscal_years)
        eps = fhps.pivot_table(
            index="symbol", columns="report_year", values="eps", aggfunc="last"
        ).reindex(columns=fiscal_years)
        latest_dps = dps[fiscal_years[-1]].reindex(out.index)
        latest_eps = eps[fiscal_years[-1]].reindex(out.index)
        out["payout_ratio_1y"] = latest_dps / latest_eps.replace(0, np.nan)
        # The methodology names but does not publish a formula for this field.
        # Endpoint growth over ex-date calendar-year DPS is the literal,
        # deterministic three-year translation and remains available before the
        # local fiscal-report cache begins in 2013.
        out["dps_growth_3y"] = (
            annual_after_tax[years[-1]].reindex(out.index)
            / annual_after_tax[years[0]].reindex(out.index).replace(0, np.nan)
            - 1.0
        )
        out["dps_growth_years_complete"] = annual_after_tax.reindex(out.index).gt(0).all(axis=1)
        return out

    def build_candidates(self, signal_date: pd.Timestamp) -> tuple[pd.DataFrame, dict[str, int]]:
        market = self._market_statistics(signal_date)
        eligible_symbols = market.index.intersection(self._securities.index)
        frame = market.reindex(eligible_symbols).join(self._securities, how="left")
        active = frame["de_listed_date"].isna() | frame["de_listed_date"].gt(signal_date)
        listed = frame["listed_date"].le(signal_date)
        base = frame[
            active & listed & frame["signal_close"].gt(0) & frame["is_st"].fillna(1).eq(0)
        ].copy()
        counts = {"csi_all_share_proxy": int(len(base))}

        base["market_cap_rank_pct"] = base["avg_total_mv"].rank(
            ascending=False, method="first", pct=True
        )
        base["amount_rank_pct"] = base["avg_amount"].rank(
            ascending=False, method="first", pct=True
        )
        base = base[
            base["market_cap_rank_pct"].le(0.80)
            & base["amount_rank_pct"].le(0.80)
        ].copy()
        counts["size_liquidity_top80"] = int(len(base))

        dividends = self._dividend_statistics(signal_date, base["signal_close"])
        base = base.join(dividends)
        base = base[
            base["continuous_dividend_3y"]
            & base["average_after_tax_dividend_yield_3y"].gt(0)
        ].copy()
        counts["continuous_cash_dividend_3y"] = int(len(base))

        valid_payout = base["payout_ratio_1y"].notna() & base["dps_growth_years_complete"]
        base = base[valid_payout].copy()
        base["payout_high_rank_pct"] = base["payout_ratio_1y"].rank(
            ascending=False, method="first", pct=True
        )
        base = base[
            base["payout_ratio_1y"].ge(0)
            & base["payout_high_rank_pct"].gt(0.05)
            & base["dps_growth_3y"].gt(0)
        ].copy()
        counts["payout_and_dps_growth"] = int(len(base))

        base["symbol_key"] = base.index.astype(str)
        base = base.sort_values(
            ["average_after_tax_dividend_yield_3y", "symbol_key"],
            ascending=[False, True],
        )
        base["dividend_rank"] = np.arange(1, len(base) + 1)
        counts["eligible_before_top75"] = int(len(base))
        top75 = base.head(self.top_dividend).copy()
        counts["dividend_top75"] = int(len(top75))
        top75 = top75.dropna(subset=["volatility_1y"]).sort_values(
            ["volatility_1y", "dividend_rank", "symbol_key"], ascending=[True, True, True]
        )
        top75["volatility_rank"] = np.arange(1, len(top75) + 1)
        counts["volatility_available"] = int(len(top75))
        return top75, counts

    def _apply_review_buffer(
        self,
        pure: Sequence[str],
        candidates: pd.DataFrame,
        previous: Sequence[str],
        market: pd.DataFrame,
    ) -> tuple[list[str], float, list[dict[str, str]]]:
        if not previous:
            return list(pure), 0.0, []
        previous = list(dict.fromkeys(str(value) for value in previous))
        retention = market.reindex(previous)
        mv_rank = market["avg_total_mv"].rank(ascending=False, method="first", pct=True)
        amount_rank = market["avg_amount"].rank(ascending=False, method="first", pct=True)
        retention["mv90"] = mv_rank.reindex(previous).le(0.90)
        retention["amount90"] = amount_rank.reindex(previous).le(0.90)
        retention["yield_gate"] = retention["last_after_tax_dividend_yield"].gt(0.005)
        retention["retained"] = retention[["mv90", "amount90", "yield_gate"]].all(axis=1)

        yield_failures = [s for s in previous if not bool(retention.loc[s, "yield_gate"])]
        replacement_budget = max(10, len(yield_failures))
        failed = [s for s in previous if not bool(retention.loc[s, "retained"])]
        # Yield failures have explicit priority in the official exception clause.
        ordered_failures = list(dict.fromkeys(yield_failures + failed))
        exits = ordered_failures[:replacement_budget]
        kept = [s for s in previous if s not in set(exits)]
        final = kept.copy()
        for symbol in pure:
            if symbol not in final:
                final.append(symbol)
            if len(final) >= self.top_volatility:
                break
        if len(final) < self.top_volatility:
            for symbol in candidates.index:
                if symbol not in final:
                    final.append(str(symbol))
                if len(final) >= self.top_volatility:
                    break
        final = final[: self.top_volatility]
        buffer_only = set(final) - set(pure)
        exceptions = [
            {"symbol": symbol, "reason": "review_buffer_retention"}
            for symbol in sorted(buffer_only)
        ]
        return final, len(buffer_only) / max(len(final), 1), exceptions

    def select(
        self,
        signal_date: pd.Timestamp,
        effective_date: pd.Timestamp,
        previous: Sequence[str] = (),
    ) -> RebalanceSnapshot:
        candidates, counts = self.build_candidates(signal_date)
        pure = list(candidates.head(self.top_volatility).index.astype(str))
        # Retention gates need the wider, pre-top75 fields. Rebuild only a compact
        # market frame and attach dividend fields for the prior 50 names.
        market = self._market_statistics(signal_date)
        dividend = self._dividend_statistics(signal_date, market["signal_close"])
        market = market.join(dividend)
        final, dependency, exceptions = self._apply_review_buffer(
            pure, candidates, previous, market
        )
        if len(final) < self.top_volatility:
            raise ValueError(
                f"{signal_date.date()} 仅选出 {len(final)} 只证券，无法复制 50 只样本"
            )
        weight_factor = market.loc[final, "average_after_tax_dividend_yield_3y"]
        weights = capped_proportional_weights(weight_factor, self.weight_cap)
        counts["pure_selection"] = len(pure)
        counts["final_selection"] = len(final)
        return RebalanceSnapshot(
            signal_date=pd.Timestamp(signal_date),
            effective_date=pd.Timestamp(effective_date),
            candidates=candidates,
            pure_selection=tuple(pure),
            final_selection=tuple(final),
            weights=weights,
            stage_counts=counts,
            buffer_dependency=float(dependency),
            exceptions=tuple(exceptions),
        )

    def build_history(self, start_year: int, end_year: int) -> list[RebalanceSnapshot]:
        snapshots: list[RebalanceSnapshot] = []
        previous: Sequence[str] = ()
        for signal, effective in self.schedule(start_year, end_year):
            snapshot = self.select(signal, effective, previous)
            snapshots.append(snapshot)
            previous = snapshot.final_selection
        return snapshots


__all__ = [
    "DividendLowVolBaseline",
    "RebalanceSnapshot",
    "capped_proportional_weights",
    "second_friday",
]
