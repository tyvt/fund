"""Industry-neutralization helpers shared by factor construction and tests."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_industry_mapping(industry_path: str | Path) -> pd.DataFrame:
    """Load a unique ``symbol, industry_name`` mapping from the SW L1 CSV.

    The project cache uses ``code, industry`` while the public helper contract
    uses ``symbol, industry_name``.  Both layouts are accepted so callers do
    not need source-specific renaming logic.
    """

    frame = pd.read_csv(industry_path, dtype=str)
    symbol_col = "symbol" if "symbol" in frame.columns else "code"
    industry_col = "industry_name" if "industry_name" in frame.columns else "industry"
    missing = [name for name in (symbol_col, industry_col) if name not in frame.columns]
    if missing:
        raise ValueError(
            "行业映射必须包含 symbol/industry_name 或 code/industry 列；"
            f"实际列：{list(frame.columns)}"
        )

    result = frame[[symbol_col, industry_col]].rename(
        columns={symbol_col: "symbol", industry_col: "industry_name"}
    )
    result["symbol"] = (
        result["symbol"].astype("string").str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    )
    result["industry_name"] = result["industry_name"].astype("string").str.strip()
    result = result.replace({"industry_name": {"": pd.NA}}).dropna(
        subset=["symbol", "industry_name"]
    )
    return result.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def neutralize_by_industry(
    frame: pd.DataFrame,
    factor_col: str,
    industry_frame: pd.DataFrame,
    date_col: str = "trade_date",
) -> pd.Series:
    """Return factor minus same-date industry mean, aligned to ``frame``.

    Rows without a known industry remain missing.  This matches pandas groupby
    semantics and prevents an unmapped stock from becoming its own industry.
    """

    required = {date_col, "symbol", factor_col}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"因子数据缺少列：{sorted(missing)}")
    industry_required = {"symbol", "industry_name"}
    industry_missing = industry_required.difference(industry_frame.columns)
    if industry_missing:
        raise ValueError(f"行业映射缺少列：{sorted(industry_missing)}")

    working = frame[[date_col, "symbol", factor_col]].copy()
    working["_row_order"] = range(len(working))
    working["symbol"] = (
        working["symbol"].astype("string").str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    )
    mapping = industry_frame[["symbol", "industry_name"]].copy()
    mapping["symbol"] = (
        mapping["symbol"].astype("string").str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    )
    mapping = mapping.drop_duplicates("symbol", keep="first")
    merged = working.merge(mapping, on="symbol", how="left", sort=False)
    industry_mean = merged.groupby(
        [date_col, "industry_name"], dropna=True
    )[factor_col].transform("mean")
    values = pd.to_numeric(merged[factor_col], errors="coerce") - pd.to_numeric(
        industry_mean, errors="coerce"
    )
    ordered = pd.Series(values.to_numpy(), index=merged["_row_order"]).sort_index()
    return pd.Series(ordered.to_numpy(), index=frame.index, name=factor_col, dtype=float)


__all__ = ["load_industry_mapping", "neutralize_by_industry"]
