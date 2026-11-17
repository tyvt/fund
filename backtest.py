"""统一回测入口：按 --mode 分发到各回测实现模块。"""

from __future__ import annotations

import argparse
import importlib
import sys

MODE_MODULES = {
    "trade": "backtest_trade_signals",
    "rotation": "backtest_rotation",
    "regime": "backtest_regime_compare",
    "wfa": "backtest_wfa",
    "optimize": "backtest_optimize",
}

MODE_HELP = {
    "trade": "买卖波段 + 智能轮动（trade_inception_present.md/html）",
    "rotation": "组合级轮动对比（rotation_compare.md）",
    "regime": "牛熊状态开关对比（regime_compare.md）",
    "wfa": "轮动策略样本外检验（wfa_rotation.md）",
    "optimize": "阈值/仓位参数搜索（optimize_*.md）",
    "inception": "买卖波段回测（等同 python backtest.py --mode trade）",
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
        default="trade",
        help="回测模式（默认 trade）",
    )
    args, rest = parser.parse_known_args(argv)

    if args.mode == "inception":
        return _run_mode("trade", rest)

    return _run_mode(args.mode, rest)


if __name__ == "__main__":
    sys.exit(main())
