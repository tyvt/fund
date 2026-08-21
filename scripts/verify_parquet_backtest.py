#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
对比 Bundle 与 Parquet 数据源的 RQAlpha 回测 NAV。

Parquet 并非在本脚本里直接 read_parquet，而是通过子进程回测链路注入：

  DLV_RQALPHA_DATA_SOURCE=parquet
    → run_backtest.build_config() 启用 mod「parquet_data」
    → mod_parquet_data.start_up() 调用 env.set_data_source(ParquetDataSource)
    → 引擎从 data/parquet/ 读行情、停牌、分红、日历（单一数据源）

用法：
  python scripts/verify_parquet_backtest.py --years 10 --end 2026-08-20 --capital 100000
  python scripts/verify_parquet_backtest.py --skip-bundle   # 已有 bundle NAV 时
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from config.paths import PARQUET_DIR, PROJECT_DIR
from dividend_lowvol_rotation.config import BACKTEST_INITIAL_CAPITAL, BACKTEST_OUTPUT_DIR as DLV_OUT
from dividend_lowvol_rotation.rqalpha.compare_baseline import _load_rqalpha_native_nav, _metrics
from market_data import configure_stdout_utf8


def _default_start(years: int, *, end: str | None = None) -> str:
    anchor = date.fromisoformat(end) if end else date.today()
    return (anchor - timedelta(days=int(365.25 * years))).isoformat()


def _preflight_parquet(parquet_root: Path) -> None:
    """确认 Parquet 数据湖就绪。"""
    required = [
        parquet_root / "stock_meta" / "instruments.pkl",
        parquet_root / "trade_calendar" / "calendar.parquet",
        parquet_root / "stock_dividend" / "dividend_events.parquet",
        parquet_root / "stock_daily",
    ]
    missing = [p for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Parquet 数据湖不完整，请先完成 export_to_parquet：\n"
            + "\n".join(f"  - {p}" for p in missing)
        )
    manifest = parquet_root / "sync_manifest.json"
    if manifest.exists():
        import json

        data = json.loads(manifest.read_text(encoding="utf-8"))
        n_ok = sum(
            1 for v in data.get("stock_daily", {}).values() if v.get("status") == "ok"
        )
        print(f"Parquet 根目录：{parquet_root}")
        print(f"manifest 已完成日 K：{n_ok} 只")
    else:
        print(f"Parquet 根目录：{parquet_root}（无 manifest，请确认已导出）")


def _compare_nav(baseline: pd.Series, test: pd.Series) -> dict:
    common = sorted(baseline.index.intersection(test.index))
    if len(common) < 2:
        return {"max_daily_nav_gap": None, "common_days": len(common)}
    b = baseline.reindex(common).astype(float)
    t = test.reindex(common).astype(float)
    gap = (t.round(2) - b.round(2)).abs()
    return {
        "max_daily_nav_gap": float(gap.max()),
        "common_days": len(common),
        "baseline_metrics": _metrics(b),
        "parquet_metrics": _metrics(t),
    }


def _run_rqalpha(
    *,
    start: str,
    end: str,
    capital: float,
    data_source: str,
    nav_path: Path,
    pkl_path: Path,
    parquet_root: Path,
) -> int:
    env = os.environ.copy()
    env["DLV_RQALPHA_DATA_SOURCE"] = data_source
    env["DLV_RQALPHA_NATIVE_NAV_PATH"] = str(nav_path)
    env["DLV_PARQUET_ROOT"] = str(parquet_root)
    if data_source == "parquet":
        print(
            f"Parquet 回测：DLV_PARQUET_ROOT={parquet_root}\n"
            "  → mod parquet_data → ParquetDataSource(data/parquet/*)"
        )
    else:
        print("Bundle 回测：RQAlpha 默认 BaseDataSource(bundle)")
    cmd = [
        sys.executable,
        "-m",
        "dividend_lowvol_rotation.rqalpha.run_backtest",
        "--start",
        start,
        "--end",
        end,
        "--capital",
        str(capital),
        "--no-report",
        "--output",
        str(pkl_path),
    ]
    print(f"运行回测：data_source={data_source} → {nav_path.name}")
    return subprocess.call(cmd, env=env, cwd=str(_ROOT))


def write_report(result: dict, path: Path, *, parquet_root: Path) -> None:
    lines = [
        "# Parquet vs Bundle 回测验证",
        "",
        f"> 生成时间：{date.today().isoformat()}",
        "",
        f"- Parquet 根目录：`{parquet_root}`",
        "",
        "## 对比结果",
        "",
        f"- max_daily_nav_gap: **{result.get('max_daily_nav_gap')}**",
        f"- 共有交易日: {result.get('common_days')}",
        "",
        "## 基准（Bundle / BaseDataSource）",
        "",
        "```json",
        str(result.get("baseline_metrics", {})),
        "```",
        "",
        "## 待测（Parquet / ParquetDataSource）",
        "",
        "```json",
        str(result.get("parquet_metrics", {})),
        "```",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="Parquet vs Bundle RQAlpha NAV 验证")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--end", default="2026-08-20")
    parser.add_argument("--capital", type=float, default=BACKTEST_INITIAL_CAPITAL)
    parser.add_argument(
        "--parquet-root",
        default=str(PARQUET_DIR),
        help="Parquet 数据湖目录（默认 data/parquet）",
    )
    parser.add_argument("--skip-bundle", action="store_true", help="跳过 bundle 基准回测（使用已有 NAV）")
    parser.add_argument(
        "--output",
        default=str(PROJECT_DIR / "output" / "verify_parquet_vs_bundle.md"),
    )
    args = parser.parse_args(argv)

    parquet_root = Path(args.parquet_root)
    _preflight_parquet(parquet_root)

    start = _default_start(args.years, end=args.end)
    end = args.end
    out_dir = DLV_OUT
    baseline_nav_path = out_dir / "rqalpha_native_nav.csv"
    parquet_nav_path = out_dir / "rqalpha_native_nav_parquet.csv"
    bundle_pkl = out_dir / "rqalpha_result_bundle_verify.pkl"
    parquet_pkl = out_dir / "rqalpha_result_parquet_verify.pkl"

    if not args.skip_bundle:
        code = _run_rqalpha(
            start=start,
            end=end,
            capital=args.capital,
            data_source="bundle",
            nav_path=baseline_nav_path,
            pkl_path=bundle_pkl,
            parquet_root=parquet_root,
        )
        if code != 0:
            return code

    code = _run_rqalpha(
        start=start,
        end=end,
        capital=args.capital,
        data_source="parquet",
        nav_path=parquet_nav_path,
        pkl_path=parquet_pkl,
        parquet_root=parquet_root,
    )
    if code != 0:
        return code

    baseline_nav = _load_rqalpha_native_nav(baseline_nav_path)
    parquet_nav = _load_rqalpha_native_nav(parquet_nav_path)
    if baseline_nav is None or parquet_nav is None:
        print("NAV 文件读取失败")
        return 2

    result = _compare_nav(baseline_nav, parquet_nav)
    write_report(result, Path(args.output), parquet_root=parquet_root)
    print(f"max_daily_nav_gap = {result.get('max_daily_nav_gap')}")
    print(f"报告：{args.output}")
    gap = result.get("max_daily_nav_gap")
    return 0 if gap is not None and gap == 0.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
