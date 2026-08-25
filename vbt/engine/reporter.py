"""Archive portable results and render Markdown/HTML reports."""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from vbt.config import ROOT


def _json_default(value: Any):
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _markdown_table(frame: pd.DataFrame) -> str:
    """Render a compact Markdown table without pandas' optional tabulate dependency."""
    columns = [str(column) for column in frame.columns]
    rows = [[str(value) for value in row] for row in frame.itertuples(index=False, name=None)]
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(values) + " |" for values in rows]
    return "\n".join([header, separator, *body])


class ReportGenerator:
    def __init__(self, results, performance, params: Mapping[str, Any] | None = None):
        self.results = results
        self.performance = performance
        self.params = dict(params or {})

    def current_holdings(self) -> pd.DataFrame:
        if self.results.shares.empty:
            return pd.DataFrame(columns=["code", "shares", "price", "market_value", "weight"])
        shares = pd.to_numeric(self.results.shares.iloc[-1], errors="coerce").fillna(0.0)
        shares = shares[shares.abs() > 1e-10]
        if shares.empty:
            return pd.DataFrame(columns=["code", "shares", "price", "market_value", "weight"])
        prices = pd.Series(index=shares.index, dtype=float)
        if not self.results.positions.empty:
            weights = self.results.positions.iloc[-1].reindex(shares.index).fillna(0.0)
        else:
            weights = pd.Series(0.0, index=shares.index)
        # market_value / shares is reliable even when the price matrix is not attached here.
        nav = float(self.results.nav.iloc[-1])
        market_values = weights * nav
        prices.loc[:] = market_values.div(shares.replace(0, np.nan))
        return (
            pd.DataFrame(
                {
                    "code": shares.index.astype(str),
                    "shares": shares.values,
                    "price": prices.values,
                    "market_value": market_values.values,
                    "weight": weights.values,
                }
            )
            .sort_values("weight", ascending=False)
            .reset_index(drop=True)
        )

    def _metrics_table(self) -> pd.DataFrame:
        metrics = self.performance.compute_metrics()
        labels = {
            "total_return": "累计收益",
            "annual_return": "年化收益",
            "max_drawdown": "最大回撤",
            "sharpe_ratio": "夏普比率",
            "win_rate": "日胜率",
            "profit_loss_ratio": "盈亏比",
            "avg_holding_days": "平均持有天数",
            "turnover": "换手率",
        }
        rows = []
        for key, label in labels.items():
            value = metrics.get(key, float("nan"))
            if key in {"total_return", "annual_return", "max_drawdown", "win_rate"}:
                shown = "—" if not np.isfinite(value) else f"{value:.2%}"
            else:
                shown = "—" if not np.isfinite(value) else f"{value:.4f}"
            rows.append({"指标": label, "值": shown})
        return pd.DataFrame(rows)

    def markdown(self) -> str:
        nav = self.results.nav.dropna()
        metrics = self._metrics_table()
        holdings = self.current_holdings().copy()
        if not holdings.empty:
            holdings["price"] = holdings["price"].map(lambda x: f"{x:.3f}")
            holdings["market_value"] = holdings["market_value"].map(lambda x: f"{x:,.2f}")
            holdings["weight"] = holdings["weight"].map(lambda x: f"{x:.2%}")
        repro = self.results.metadata.get("reproducibility", {})
        lines = [
            "# VectorBT 红利低波回测报告",
            "",
            f"- 回测区间：{nav.index.min().date()} ～ {nav.index.max().date()}",
            f"- 初始资金：{self.results.initial_capital:,.2f}",
            f"- 期末净值：{float(nav.iloc[-1]):,.2f}",
            f"- 成交笔数：{len(self.results.trades)}",
            f"- 生成时间：{repro.get('timestamp', '—')}",
            f"- Commit：`{repro.get('commit_hash', 'unknown')}`",
            f"- 配置版本：`{repro.get('config_version', 'unknown')}`",
            f"- 数据版本：`{repro.get('data_version', 'unknown')}`",
            "",
            "## 核心指标",
            "",
            _markdown_table(metrics),
            "",
            "## 期末持仓",
            "",
            _markdown_table(holdings) if not holdings.empty else "当前无持仓。",
            "",
            "## 使用参数",
            "",
            "```json",
            json.dumps(self.params, ensure_ascii=False, indent=2, default=_json_default),
            "```",
            "",
        ]
        return "\n".join(lines)

    def _chart_html(self) -> str:
        nav = self.results.nav.astype(float).dropna()
        drawdown = nav.div(nav.cummax()).sub(1.0)
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.68, 0.32])
        fig.add_trace(go.Scatter(x=nav.index, y=nav.values, name="组合净值", line={"width": 2}), row=1, col=1)
        fig.add_trace(go.Scatter(x=drawdown.index, y=drawdown.values, name="回撤", fill="tozeroy", line={"color": "#d95f59"}), row=2, col=1)
        fig.update_yaxes(title_text="资产", row=1, col=1)
        fig.update_yaxes(title_text="回撤", tickformat=".0%", row=2, col=1)
        fig.update_layout(height=680, margin={"l": 55, "r": 25, "t": 35, "b": 35}, hovermode="x unified", template="plotly_white")
        return fig.to_html(full_html=False, include_plotlyjs=True, config={"responsive": True})

    def html(self) -> str:
        nav = self.results.nav.astype(float).dropna()
        metrics = self._metrics_table().to_html(index=False, classes="dataframe")
        holdings = self.current_holdings().copy()
        if not holdings.empty:
            holdings["price"] = holdings["price"].map(lambda x: f"{x:.3f}")
            holdings["market_value"] = holdings["market_value"].map(lambda x: f"{x:,.2f}")
            holdings["weight"] = holdings["weight"].map(lambda x: f"{x:.2%}")
        holding_html = holdings.to_html(index=False, classes="dataframe") if not holdings.empty else "<p>当前无持仓。</p>"
        params_json = html.escape(json.dumps(self.params, ensure_ascii=False, indent=2, default=_json_default))
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VectorBT 红利低波回测报告</title>
<style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;max-width:1180px;margin:32px auto;padding:0 22px;color:#1f2937}}h1,h2{{color:#0f3d56}}.cards{{display:flex;gap:16px;flex-wrap:wrap}}.card{{background:#f3f7f9;border-radius:10px;padding:14px 20px;min-width:180px}}table{{border-collapse:collapse;width:100%;margin:12px 0 26px}}th,td{{padding:8px 10px;border-bottom:1px solid #dfe7eb;text-align:right}}th:first-child,td:first-child{{text-align:left}}pre{{background:#f6f8fa;padding:16px;overflow:auto;border-radius:8px}}</style></head>
<body><h1>VectorBT 红利低波回测报告</h1>
<div class="cards"><div class="card">区间<br><strong>{nav.index.min().date()} ～ {nav.index.max().date()}</strong></div><div class="card">期末资产<br><strong>{float(nav.iloc[-1]):,.2f}</strong></div><div class="card">成交笔数<br><strong>{len(self.results.trades)}</strong></div></div>
<h2>净值与回撤</h2>{self._chart_html()}<h2>核心指标</h2>{metrics}<h2>期末持仓</h2>{holding_html}<h2>使用参数</h2><pre>{params_json}</pre></body></html>"""

    def to_markdown(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.markdown(), encoding="utf-8")
        return target

    def to_html(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.html(), encoding="utf-8")
        return target

    def archive(self, output_root: str | Path = ROOT / "output/vectorbt") -> dict[str, Path]:
        root = Path(output_root)
        if not root.is_absolute():
            root = ROOT / root
        repro = self.results.metadata.get("reproducibility", {})
        timestamp = str(repro.get("timestamp") or datetime.now().isoformat())
        stamp = timestamp.replace("-", "").replace(":", "").replace("+", "_").replace("T", "_")[:15]
        config_version = str(repro.get("config_version", "unknown"))[:8]
        run_id = f"{stamp}_{config_version}"
        results_dir = root / "backtest_results" / run_id
        reports_dir = root / "reports" / run_id
        results_dir.mkdir(parents=True, exist_ok=False)
        reports_dir.mkdir(parents=True, exist_ok=False)

        nav_path = results_dir / "nav.parquet"
        self.results.nav.rename("nav").to_frame().rename_axis("trade_date").to_parquet(nav_path)
        positions_path = results_dir / "positions.parquet"
        self.results.positions.rename_axis("trade_date").to_parquet(positions_path)
        trades_path = results_dir / "trades.parquet"
        self.results.trades.to_parquet(trades_path, index=False)
        holdings_path = results_dir / "current_holdings.parquet"
        self.current_holdings().to_parquet(holdings_path, index=False)
        metrics_path = results_dir / "metrics.json"
        metrics_path.write_text(json.dumps(self.performance.compute_metrics(), ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        metadata_path = results_dir / "metadata.json"
        metadata_path.write_text(json.dumps(self.results.metadata, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        markdown_path = self.to_markdown(reports_dir / "report.md")
        html_path = self.to_html(reports_dir / "report.html")
        latest = root / "LATEST.txt"
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(str(reports_dir.relative_to(root)).replace("\\", "/"), encoding="utf-8")
        return {"run_dir": results_dir, "report_dir": reports_dir, "markdown": markdown_path, "html": html_path, "latest": latest}


__all__ = ["ReportGenerator"]
