"""Render portable Markdown and HTML diagnosis reports."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from alphapurify_bridge.config import ROOT


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "factor"


def _number(value: Any, *, percent: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(number):
        return "—"
    return f"{number:.2%}" if percent else f"{number:.4f}"


def _table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


class DiagnosisReporter:
    def __init__(self, output_dir: str | Path = "output/alphapurify/reports"):
        target = Path(output_dir)
        self.output_dir = target if target.is_absolute() else ROOT / target

    def _chart_dir(self) -> Path:
        path = self.output_dir / "charts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _generate_charts(self, result: Mapping[str, Any]) -> dict[str, str]:
        factor = _slug(str(result["factor_name"]))
        chart_dir = self._chart_dir()
        details = result.get("details", {}) or {}
        paths: dict[str, str] = {}

        ic_series = details.get("ic_series", {}) or {}
        if ic_series:
            fig, axes = plt.subplots(2, 1, figsize=(10, 7), constrained_layout=True)
            for label, points in ic_series.items():
                if not points:
                    continue
                frame = pd.DataFrame(points)
                axes[0].plot(pd.to_datetime(frame["trade_date"]), frame["value"], linewidth=0.8, label=label)
            axes[0].axhline(0.0, color="#777", linewidth=0.7)
            axes[0].set_title(f"{result['factor_name']} IC time series")
            axes[0].legend(ncol=3, fontsize=8)
            by_horizon = result.get("ic_by_horizon", {}) or {}
            labels = list(by_horizon)
            axes[1].plot(labels, [by_horizon[key] for key in labels], marker="o", color="#0f6b78")
            axes[1].set_title("IC by forward horizon")
            axes[1].axhline(0.0, color="#777", linewidth=0.7)
            path = chart_dir / f"{factor}_ic.png"
            fig.savefig(path, dpi=130)
            plt.close(fig)
            paths["ic"] = f"charts/{path.name}"

        quantiles = result.get("quantile_returns", []) or []
        if quantiles:
            fig, ax = plt.subplots(figsize=(9, 4.5), constrained_layout=True)
            ax.bar(range(1, len(quantiles) + 1), [np.nan if value is None else value for value in quantiles], color="#3f7f93")
            ax.set_title(f"{result['factor_name']} annualized quantile returns")
            ax.set_xlabel("Quantile (high = preferred)")
            ax.set_ylabel("Annualized return")
            ax.yaxis.set_major_formatter(lambda value, _position: f"{value:.0%}")
            path = chart_dir / f"{factor}_quantile.png"
            fig.savefig(path, dpi=130)
            plt.close(fig)
            paths["quantile"] = f"charts/{path.name}"

        spread = details.get("spread_curve", []) or []
        if spread:
            frame = pd.DataFrame(spread)
            fig, ax = plt.subplots(figsize=(9, 4.5), constrained_layout=True)
            ax.plot(pd.to_datetime(frame["trade_date"]), frame["value"], color="#b45b35")
            ax.set_title(f"{result['factor_name']} top-bottom cumulative curve")
            ax.set_ylabel("Cumulative value")
            path = chart_dir / f"{factor}_spread.png"
            fig.savefig(path, dpi=130)
            plt.close(fig)
            paths["spread"] = f"charts/{path.name}"

        histogram = details.get("factor_distribution", {}) or {}
        edges, counts = histogram.get("edges", []), histogram.get("counts", [])
        if edges and counts:
            fig, ax = plt.subplots(figsize=(9, 4.5), constrained_layout=True)
            widths = np.diff(edges)
            ax.bar(edges[:-1], counts, width=widths, align="edge", color="#638c68")
            ax.set_title(f"{result['factor_name']} distribution")
            path = chart_dir / f"{factor}_distribution.png"
            fig.savefig(path, dpi=130)
            plt.close(fig)
            paths["distribution"] = f"charts/{path.name}"
        return paths

    def factor_markdown(self, result: Mapping[str, Any], charts: Mapping[str, str] | None = None) -> str:
        charts = dict(charts or {})
        checks = result.get("checks", {}) or {}
        labels = {
            "ic_mean": "IC 均值",
            "ic_ir": "IC IR",
            "spread_return": "多空收益（年化）",
            "quantile_monotonicity": "分层单调性",
            "ic_decay": "最大 IC 衰减",
        }
        rows = []
        for key, check in checks.items():
            percent = key in {"spread_return", "ic_decay"}
            value = str(check.get("value")) if isinstance(check.get("value"), bool) else _number(check.get("value"), percent=percent)
            threshold = str(check.get("threshold")) if isinstance(check.get("threshold"), bool) else _number(check.get("threshold"), percent=percent)
            icon = {"PASS": "✅", "WARNING": "⚠️", "FAIL": "❌"}.get(check.get("status"), "")
            rows.append([labels.get(key, key), value, f"{check.get('operator', '')} {threshold}", f"{icon} {check.get('status', '')}"])
        lines = [
            f"# 因子诊断报告：{result['factor_name']}",
            "",
            f"- 显示名称：{result.get('display_name', result['factor_name'])}",
            f"- 诊断区间：{result.get('start_date')} ～ {result.get('end_date')}",
            f"- 有效样本：{int(result.get('sample_count', 0)):,}",
            f"- 因子方向：{'越大越好' if int(result.get('direction', 1)) == 1 else '越小越好'}",
            f"- 主预测期：{int(result.get('primary_horizon', 1))} 个交易日（IC、IR 与分层收益统一口径）",
            f"- 因子版本：{result.get('factor_version', 'unknown')}；数据版本：{result.get('data_version', 'unknown')}",
            f"- AlphaPurify：{result.get('alphapurify_version') or '未安装'}",
            "",
            "## 判定摘要",
            "",
            _table(rows, ["指标", "值", "阈值", "状态"]),
            "",
            f"**最终判定：{result.get('status')}** — {result.get('summary', '')}",
            "",
            "## IC 分析",
            "",
            _table(
                [[key, _number(value), _number((result.get('ic_decay') or {}).get(key), percent=True)] for key, value in (result.get("ic_by_horizon") or {}).items()],
                ["预测期", "IC 均值", "相对主预测期衰减"],
            ),
            "",
        ]
        for key, title in (("ic", "IC 时间序列与预测期衰减"), ("quantile", "分层收益"), ("spread", "多空累计收益"), ("distribution", "因子分布")):
            if key in charts:
                lines.extend([f"## {title}", "", f"![{title}]({charts[key]})", ""])
        return "\n".join(lines)

    @staticmethod
    def _html_document(title: str, markdown_text: str) -> str:
        try:
            import markdown

            body = markdown.markdown(markdown_text, extensions=["tables", "fenced_code"])
        except ImportError:
            body = f"<pre>{html.escape(markdown_text)}</pre>"
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;max-width:1180px;margin:32px auto;padding:0 22px;color:#1f2937}}h1,h2{{color:#0f3d56}}table{{border-collapse:collapse;width:100%;margin:12px 0 26px}}th,td{{padding:8px 10px;border-bottom:1px solid #dfe7eb;text-align:right}}th:first-child,td:first-child{{text-align:left}}img{{max-width:100%;height:auto;border:1px solid #e5e7eb}}code{{background:#f3f4f6;padding:2px 4px}}</style></head><body>{body}</body></html>"""

    def generate_factor_report(self, result: Mapping[str, Any], format: str = "md") -> Path:
        return self.generate_factor_reports(result, [format])[0]

    def generate_factor_reports(
        self,
        result: Mapping[str, Any],
        formats: Sequence[str] = ("md", "html"),
    ) -> list[Path]:
        """Generate several formats while rendering expensive charts only once."""

        selected = list(dict.fromkeys(str(value).lower() for value in formats))
        if not selected or any(value not in {"md", "html"} for value in selected):
            raise ValueError("formats 必须包含 md 或 html")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        charts = self._generate_charts(result)
        markdown_text = self.factor_markdown(result, charts)
        paths: list[Path] = []
        for output_format in selected:
            target = self.output_dir / f"{_slug(str(result['factor_name']))}_diagnosis.{output_format}"
            content = (
                markdown_text
                if output_format == "md"
                else self._html_document(str(result["factor_name"]), markdown_text)
            )
            target.write_text(content, encoding="utf-8")
            paths.append(target)
        return paths

    def batch_markdown(self, results: Sequence[Mapping[str, Any]]) -> str:
        rows = [
            [
                str(result["factor_name"]),
                f"{int(result.get('primary_horizon', 1))}日",
                _number(result.get("ic_mean")),
                _number(result.get("ic_ir")),
                _number(result.get("spread_return"), percent=True),
                str(result.get("status")),
                str(result.get("summary", "")),
            ]
            for result in results
        ]
        return "\n".join([
            "# 因子批量诊断报告",
            "",
            _table(rows, ["因子", "主预测期", "IC 均值", "IC IR", "多空收益", "判定", "摘要"]),
            "",
            f"通过 {sum(result.get('status') == 'PASS' for result in results)} / {len(results)} 个因子。",
            "",
        ])

    def generate_batch_report(
        self,
        results: Sequence[Mapping[str, Any]],
        format: str = "html",
        *,
        stem: str = "batch_diagnosis",
    ) -> Path:
        output_format = str(format).lower()
        if output_format not in {"md", "html"}:
            raise ValueError("format 必须为 md 或 html")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        markdown_text = self.batch_markdown(results)
        target = self.output_dir / f"{_slug(stem)}.{output_format}"
        content = markdown_text if output_format == "md" else self._html_document("因子批量诊断报告", markdown_text)
        target.write_text(content, encoding="utf-8")
        return target


__all__ = ["DiagnosisReporter"]
