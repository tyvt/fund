"""统一回测入口：按 --mode 分发到各回测实现模块。"""

from __future__ import annotations

import argparse
import importlib
import sys

MODE_MODULES = {
    "trade": "backtest_trade_signals",
}

MODE_HELP = {
    "trade": "分指数买卖波段回测（trade_inception_present.md/html）",
    "inception": "分指数买卖回测（等同 python backtest.py --mode trade）",
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
    parser.add_argument(
        "--no-refresh-panel",
        action="store_true",
        help="回测成功后不自动刷新当日建议面板 trade_inception_present.html",
    )
    args, rest = parser.parse_known_args(argv)

    if args.mode == "inception":
        rc = _run_mode("trade", rest)
    else:
        rc = _run_mode(args.mode, rest)

    # 每次回测成功后自动刷新当日建议面板；失败仅告警，不影响回测结果
    if rc == 0 and not args.no_refresh_panel:
        try:
            from push import refresh_panel

            path = refresh_panel()
            print(f"\n已自动刷新当日建议面板：{path}")
        except Exception as exc:  # noqa: BLE001 - 面板刷新失败不应使回测失败
            print(f"\n警告：自动刷新当日建议面板失败（回测结果不受影响）：{exc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
