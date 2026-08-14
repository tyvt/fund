"""统一回测入口：按 --mode 分发到各回测实现模块。"""

from __future__ import annotations

import argparse
import importlib
import sys

MODE_MODULES = {
    "buy": "backtest_buy_signals",
    "trade": "backtest_trade_signals",
    "rotation": "backtest_rotation",
    "regime": "backtest_regime_compare",
    "wfa": "backtest_wfa",
    "optimize": "backtest_optimize",
}

MODE_HELP = {
    "buy": "仅买入持有（inception_present.md/html）",
    "trade": "买卖波段 + 智能轮动（trade_inception_present.md/html）",
    "rotation": "组合级轮动对比（rotation_compare.md）",
    "regime": "牛熊状态开关对比（regime_compare.md）",
    "wfa": "轮动策略样本外检验（wfa_rotation.md）",
    "optimize": "阈值/仓位参数搜索（optimize_*.md）",
    "inception": "一键全量：buy + trade（等同 run_backtest_inception.bat）",
}


def _run_mode(mode: str, argv: list[str]) -> int:
    module_name = MODE_MODULES[mode]
    module = importlib.import_module(module_name)
    return int(module.main(argv) or 0)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="统一回测入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {k:10} {v}" for k, v in MODE_HELP.items()),
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=list(MODE_MODULES) + ["inception"],
        default="buy",
        help="回测模式（默认 buy）",
    )
    args, rest = parser.parse_known_args(argv)

    if args.mode == "inception":
        rc = _run_mode("buy", rest)
        if rc:
            return rc
        return _run_mode("trade", rest)

    return _run_mode(args.mode, rest)


if __name__ == "__main__":
    sys.exit(main())
