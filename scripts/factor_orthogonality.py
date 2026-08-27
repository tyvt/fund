"""Orthogonality, raw/purified IC and reversal competition for fusion_v2."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alphapurify_bridge.diagnostics.metrics import compute_ic, compute_ir
from factor_snapshot_loader import load_snapshots


OUTPUT = ROOT / "output" / "orthogonality"
SNAPSHOT_ROOT = ROOT / "data" / "parquet" / "factors" / "snapshots"
QFQ_GLOB = ROOT / "data" / "parquet" / "stock_daily_qfq" / "year=*" / "*.parquet"

CANDIDATES = (
    "dividend_yield",
    "volatility_60d",
    "roe_ttm",
    "fcf_ev",
    "pe_industry_quantile",
    "gross_margin",
    "reversal_5d",
    "reversal_10d",
)
DIRECTIONS = {
    "dividend_yield": 1,
    "volatility_60d": -1,
    "roe_ttm": 1,
    "fcf_ev": 1,
    "pe_industry_quantile": 1,
    "gross_margin": 1,
    "reversal_5d": 1,
    "reversal_10d": 1,
}
NEUTRALIZATION = {
    "dividend_yield": "none（行业差异保留为信号）",
    "volatility_60d": "none（量价因子）",
    "roe_ttm": "industry_zscore",
    "fcf_ev": "cross_sectional_zscore（行业中性仅作纯化失败候选）",
    "pe_industry_quantile": "embedded_industry_quantile",
    "gross_margin": "industry_zscore",
    "reversal_5d": "none（量价因子）",
    "reversal_10d": "none（量价因子）",
}


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _configure_font() -> None:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [
                font_manager.FontProperties(fname=str(path)).get_name()
            ]
            break
    plt.rcParams["axes.unicode_minus"] = False


def load_factor_panel(start: date, end: date) -> pd.DataFrame:
    frame = load_snapshots(
        start=start.isoformat(),
        end=end.isoformat(),
        factors=list(CANDIDATES),
        snapshot_root=SNAPSHOT_ROOT,
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).astype("datetime64[ns]")
    # Extended factors are monthly; retaining only their signal dates also
    # prevents daily dividend/volatility observations from dominating.
    monthly = frame["pe_industry_quantile"].notna()
    return frame.loc[monthly, ["trade_date", "symbol", *CANDIDATES]].copy()


def load_forward_returns(start: date, end: date, horizon: int = 20) -> pd.DataFrame:
    path = QFQ_GLOB.resolve().as_posix().replace("'", "''")
    with duckdb.connect() as con:
        frame = con.execute(
            f"""
            WITH raw AS (
                SELECT try_cast(trade_date AS DATE) AS trade_date, symbol, max(close) AS close
                FROM read_parquet('{path}', hive_partitioning=true, union_by_name=true)
                WHERE try_cast(trade_date AS DATE) <= DATE '{end}' + INTERVAL '{int(horizon) + 10} days'
                GROUP BY trade_date, symbol
            ), calculated AS (
                SELECT trade_date, symbol,
                       lead(close, {int(horizon)}) OVER (
                           PARTITION BY symbol ORDER BY trade_date
                       ) / nullif(close, 0) - 1.0 AS forward_return
                FROM raw
            )
            SELECT * FROM calculated
            WHERE trade_date BETWEEN DATE '{start}' AND DATE '{end}'
            """
        ).fetchdf()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).astype("datetime64[ns]")
    return frame


def _direction_adjust(frame: pd.DataFrame) -> pd.DataFrame:
    adjusted = frame.copy()
    for factor, direction in DIRECTIONS.items():
        adjusted[factor] = pd.to_numeric(adjusted[factor], errors="coerce") * direction
    return adjusted


def compute_orthogonality(factor_df: pd.DataFrame) -> pd.DataFrame:
    """Return pooled Pearson correlation of monthly cross-sectional z-scores."""

    required = {"trade_date", *CANDIDATES}
    missing = required - set(factor_df.columns)
    if missing:
        raise KeyError(f"正交性输入缺少：{', '.join(sorted(missing))}")
    adjusted = _direction_adjust(factor_df)
    standardized = adjusted.groupby("trade_date", observed=True)[list(CANDIDATES)].transform(
        lambda values: (values - values.mean()) / values.std(ddof=0)
    )
    return standardized.corr(method="pearson", min_periods=100)


def _purified_values(frame: pd.DataFrame, target: str, factors: Sequence[str]) -> pd.Series:
    others = [factor for factor in factors if factor != target]
    output = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, locations in frame.groupby("trade_date", observed=True).groups.items():
        locations = pd.Index(locations)
        columns = [target, *others]
        valid = frame.loc[locations, columns].replace([np.inf, -np.inf], np.nan).dropna()
        if len(valid) < max(20, len(others) + 5):
            continue
        y = valid[target].to_numpy(dtype=float)
        if others:
            x = valid[others].to_numpy(dtype=float)
            x = np.column_stack([np.ones(len(x)), x])
            coefficients, *_ = np.linalg.lstsq(x, y, rcond=None)
            residual = y - x @ coefficients
        else:
            residual = y - y.mean()
        output.loc[valid.index] = residual
    return output


def compute_purified_ic_series(
    factor_df: pd.DataFrame,
    forward_returns: pd.DataFrame | pd.Series,
    factor: str,
    *,
    factors: Sequence[str] = CANDIDATES,
    min_cross_section: int = 20,
) -> pd.Series:
    """Return the direction-adjusted cross-sectional purified-IC time series.

    This is the common source for the orthogonality report and the locked
    development-period t-statistic gate.  Keeping the series calculation in
    one place prevents the two audit artefacts from silently using different
    purification or direction conventions.
    """

    selected = tuple(dict.fromkeys(str(value) for value in factors))
    if factor not in selected:
        raise ValueError(f"目标因子 {factor} 不在纯化因子集合中")
    unknown = set(selected) - set(DIRECTIONS)
    if unknown:
        raise ValueError(f"未知因子：{', '.join(sorted(unknown))}")
    required = {"trade_date", factor, *selected}
    missing = required - set(factor_df.columns)
    if missing:
        raise KeyError(f"纯化 IC 输入缺少：{', '.join(sorted(missing))}")

    if isinstance(forward_returns, pd.Series):
        merged = factor_df.copy()
        merged["forward_return"] = forward_returns.reindex(factor_df.index)
    else:
        merged = factor_df.merge(
            forward_returns[["trade_date", "symbol", "forward_return"]],
            on=["trade_date", "symbol"],
            how="left",
            validate="many_to_one",
        )
    adjusted = _direction_adjust(merged)
    purified_col = f"{factor}_purified"
    adjusted[purified_col] = _purified_values(adjusted, factor, selected)
    return compute_ic(
        adjusted[["trade_date", purified_col, "forward_return"]],
        factor_col=purified_col,
        return_col="forward_return",
        min_observations=min_cross_section,
    ).sort_index()


def compute_ic_ranking(
    factor_df: pd.DataFrame,
    forward_returns: pd.DataFrame | pd.Series,
    *,
    factors: Sequence[str] = CANDIDATES,
    min_cross_section: int = 20,
) -> pd.DataFrame:
    """Compute direction-adjusted raw and multivariate-purified IC/IR."""

    if isinstance(forward_returns, pd.Series):
        merged = factor_df.copy()
        merged["forward_return"] = forward_returns.reindex(factor_df.index)
    else:
        merged = factor_df.merge(
            forward_returns[["trade_date", "symbol", "forward_return"]],
            on=["trade_date", "symbol"],
            how="left",
            validate="many_to_one",
        )
    adjusted = _direction_adjust(merged)
    rows: list[dict[str, Any]] = []
    for factor in factors:
        raw = compute_ic(
            adjusted[["trade_date", factor, "forward_return"]],
            factor_col=factor,
            return_col="forward_return",
            min_observations=min_cross_section,
        )
        purified = compute_purified_ic_series(
            factor_df,
            forward_returns,
            factor,
            factors=factors,
            min_cross_section=min_cross_section,
        )
        raw_mean = float(raw.mean()) if len(raw) else float("nan")
        raw_ir = compute_ir(raw)
        purified_mean = float(purified.mean()) if len(purified) else float("nan")
        purified_ir = compute_ir(purified)
        diagnostics_pass = bool(
            math.isfinite(raw_mean)
            and raw_mean >= 0.010
            and math.isfinite(raw_ir)
            and raw_ir >= 0.15
            and math.isfinite(purified_mean)
            and purified_mean >= 0.010
        )
        rows.append(
            {
                "factor": factor,
                "ic_mean": raw_mean,
                "ic_ir": raw_ir,
                "purified_ic_mean": purified_mean,
                "purified_ic_ir": purified_ir,
                "ic_observations": int(len(raw)),
                "purified_ic_observations": int(len(purified)),
                "diagnostics_pass": diagnostics_pass,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["diagnostics_pass", "ic_mean"], ascending=[False, False]
    ).reset_index(drop=True)


def _reversal_decision(
    ic: pd.DataFrame, correlation: pd.DataFrame, base_factors: Sequence[str]
) -> tuple[str | None, list[int], str]:
    ranking = ic.set_index("factor")
    r5 = ranking.loc["reversal_5d"]
    r10 = ranking.loc["reversal_10d"]
    pass5 = bool(r5["diagnostics_pass"])
    pass10 = bool(r10["diagnostics_pass"])
    difference = abs(float(r5["ic_mean"]) - float(r10["ic_mean"]))
    if difference > 0.005:
        winner = "reversal_5d" if float(r5["ic_mean"]) > float(r10["ic_mean"]) else "reversal_10d"
        loser_window = 10 if winner == "reversal_5d" else 5
        return winner, [loser_window], f"IC 差 {difference:.6f} > 0.005，保留 IC 更高者"
    if not pass5 and not pass10:
        return None, [5, 10], "IC 差不显著且 R1/R2 均未通过诊断，二者剔除并启用 5日、10日过热风控"
    if pass5 != pass10:
        winner = "reversal_5d" if pass5 else "reversal_10d"
        loser_window = 10 if winner == "reversal_5d" else 5
        return winner, [loser_window], f"IC 差不显著，仅 {winner} 通过诊断"
    orth5 = float(correlation.loc["reversal_5d", list(base_factors)].abs().mean())
    orth10 = float(correlation.loc["reversal_10d", list(base_factors)].abs().mean())
    winner = "reversal_5d" if orth5 <= orth10 else "reversal_10d"
    loser_window = 10 if winner == "reversal_5d" else 5
    return (
        winner,
        [loser_window],
        f"IC 差 {difference:.6f} <= 0.005；平均|相关| R1={orth5:.4f}, R2={orth10:.4f}",
    )


def select_factors(
    correlation: pd.DataFrame,
    ic_ranking: pd.DataFrame,
    *,
    threshold: float = 0.7,
) -> dict[str, Any]:
    base = [factor for factor in CANDIDATES if not factor.startswith("reversal_")]
    reversal, risk_windows, reversal_reason = _reversal_decision(ic_ranking, correlation, base)
    selected = base + ([reversal] if reversal else [])
    ranking = ic_ranking.set_index("factor")
    conflicts: list[dict[str, Any]] = []
    removed: set[str] = set()
    pairs: list[tuple[float, str, str]] = []
    for left_index, left in enumerate(selected):
        for right in selected[left_index + 1 :]:
            value = float(correlation.loc[left, right])
            if math.isfinite(value) and abs(value) > float(threshold):
                pairs.append((abs(value), left, right))
    for absolute, left, right in sorted(pairs, reverse=True):
        if left in removed or right in removed:
            continue
        left_ic = float(ranking.loc[left, "ic_mean"])
        right_ic = float(ranking.loc[right, "ic_mean"])
        keep, drop = (left, right) if left_ic >= right_ic else (right, left)
        removed.add(drop)
        conflicts.append(
            {
                "left": left,
                "right": right,
                "correlation": float(correlation.loc[left, right]),
                "kept": keep,
                "removed": drop,
                "reason": "保留方向调整后 IC 均值更高者",
            }
        )
    final = [factor for factor in selected if factor not in removed]
    capacity_removed: list[str] = []
    while len(final) > 6:
        removable = [
            factor
            for factor in final
            if factor not in {"dividend_yield", "volatility_60d"}
        ]
        drop = min(
            removable,
            key=lambda factor: (
                bool(ranking.loc[factor, "diagnostics_pass"]),
                float(ranking.loc[factor, "ic_mean"]),
                float(ranking.loc[factor, "purified_ic_mean"]),
            ),
        )
        final.remove(drop)
        removed.add(drop)
        capacity_removed.append(drop)
    return {
        "factors": final,
        "directions": {factor: DIRECTIONS[factor] for factor in final},
        "neutralization": {factor: NEUTRALIZATION[factor] for factor in final},
        "correlation_threshold": float(threshold),
        "reversal_winner": reversal,
        "reversal_reason": reversal_reason,
        "overheat_risk_windows": risk_windows,
        "overheat_quantile": 0.95,
        "conflicts": conflicts,
        "capacity_removed": capacity_removed,
        "removed_factors": sorted(removed | ({"reversal_5d", "reversal_10d"} - ({reversal} if reversal else set()))),
    }


def plot_heatmap(correlation: pd.DataFrame, output: Path) -> None:
    _configure_font()
    fig, ax = plt.subplots(figsize=(10, 8))
    image = ax.imshow(correlation.to_numpy(), vmin=-1, vmax=1, cmap="RdBu_r")
    ax.set_xticks(range(len(correlation.columns)), correlation.columns, rotation=45, ha="right")
    ax.set_yticks(range(len(correlation.index)), correlation.index)
    for row in range(len(correlation.index)):
        for column in range(len(correlation.columns)):
            value = correlation.iloc[row, column]
            ax.text(column, row, f"{value:.2f}", ha="center", va="center", fontsize=8)
    ax.set_title("月末截面因子 Pearson 相关性（方向调整、截面标准化）")
    fig.colorbar(image, ax=ax)
    fig.tight_layout()
    fig.savefig(output, dpi=180)
    plt.close(fig)


def _markdown(
    correlation: pd.DataFrame, ic: pd.DataFrame, selection: Mapping[str, Any]
) -> str:
    display = ic.copy()
    for column in ("ic_mean", "ic_ir", "purified_ic_mean", "purified_ic_ir"):
        display[column] = display[column].map(lambda value: "—" if pd.isna(value) else f"{value:.4f}")
    threshold = float(selection.get("correlation_threshold", 0.70))
    conflict_lines: list[str] = []
    for left_index, left in enumerate(correlation.index):
        for right in correlation.columns[left_index + 1 :]:
            value = float(correlation.loc[left, right])
            if not math.isfinite(value) or abs(value) < threshold:
                continue
            if {left, right} == {"reversal_5d", "reversal_10d"}:
                conflict_lines.append(
                    f"- ⚠️ 高相关冲突对（竞争上岗已裁决）：`{left}` / `{right}`，"
                    f"r={value:.4f}；胜者 `{selection.get('reversal_winner')}`。"
                )
                continue
            decision = next(
                (
                    row for row in selection.get("conflicts", [])
                    if {row["left"], row["right"]} == {left, right}
                ),
                None,
            )
            suffix = (
                f"保留 `{decision['kept']}`，剔除 `{decision['removed']}`。"
                if decision else "待裁决。"
            )
            conflict_lines.append(
                f"- ⚠️ 高相关冲突对：`{left}` / `{right}`，r={value:.4f}；{suffix}"
            )
    if not conflict_lines:
        conflict_lines.append(f"- 无 |r| ≥ {threshold:.2f} 的冲突对。")
    if selection.get("capacity_removed"):
        conflict_lines.append(
            "- 最终池限制为 6 个，按诊断强度剔除："
            + ", ".join(f"`{factor}`" for factor in selection["capacity_removed"])
            + "。"
        )
    neutralization = [
        f"- `{factor}`：{NEUTRALIZATION[factor]}" for factor in CANDIDATES
    ]
    return "\n".join(
        [
            "# fusion_v2 因子正交性与纯化 IC 报告",
            "",
            "## 结论",
            "",
            f"- 正交性阶段候选因子（非主配置裁决）：{', '.join(f'`{factor}`' for factor in selection['factors'])}",
            f"- 反转裁决：{selection['reversal_reason']}",
            f"- 失败者过热风控窗口：{selection['overheat_risk_windows']}；阈值为当期涨幅 95% 分位。",
            "",
            "## IC 与纯化 IC",
            "",
            display.to_markdown(index=False),
            "",
            "辅助诊断参考口径：IC均值≥0.010、IC_IR≥0.15、纯化IC均值≥0.010；不作为主配置硬闸门。主配置唯一准入闸门为开发期纯化 IC Newey-West t≥2。",
            "",
            "## 高相关冲突",
            "",
            f"判定阈值：**|r| ≥ {threshold:.2f}**。",
            "",
            *conflict_lines,
            "",
            "## 中性化一致性说明",
            "",
            *neutralization,
            "",
            "## 相关矩阵",
            "",
            correlation.round(4).to_markdown(),
            "",
            "![相关性热力图](correlation_heatmap.png)",
            "",
        ]
    )


def run(start: date, end: date) -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    factors = load_factor_panel(start, end)
    forward = load_forward_returns(start, end)
    correlation = compute_orthogonality(factors)
    ic = compute_ic_ranking(factors, forward)
    selection = select_factors(correlation, ic)
    correlation.to_csv(OUTPUT / "correlation_matrix.csv", encoding="utf-8-sig")
    ic.to_csv(OUTPUT / "ic_ranking.csv", index=False, encoding="utf-8-sig")
    (OUTPUT / "selected_factors.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    plot_heatmap(correlation, OUTPUT / "correlation_heatmap.png")
    (OUTPUT / "orthogonality_report.md").write_text(
        _markdown(correlation, ic, selection), encoding="utf-8"
    )
    return selection


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fusion_v2 因子正交性与纯化 IC")
    parser.add_argument("--start", type=_parse_date, default=date(2015, 1, 1))
    parser.add_argument("--end", type=_parse_date, default=date(2024, 12, 31))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selection = run(args.start, args.end)
    print(json.dumps(selection, ensure_ascii=False, indent=2))
    print(f"正交性输出：{OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
