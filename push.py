"""生成报告并推送微信（供定时任务使用）。"""

import argparse
import sys

from config import load_config
from notify import build_push_title, push_to_wechat
from report import MODULE_ALL, MODULE_CHOICES, generate_reports, print_report


def main(argv=None):
    parser = argparse.ArgumentParser(description="投资信号微信推送")
    parser.add_argument(
        "-m",
        "--module",
        action="append",
        choices=MODULE_CHOICES,
        dest="modules",
        help="推送模块：dividend、a500、hs300、zz500、zz1000、kc50、cyb、ndx、spx、all(全部，默认)",
    )
    parser.add_argument(
        "--index",
        action="append",
        metavar="CODE",
        help="红利模块：仅推送指定指数（可多次指定）",
    )
    parser.add_argument(
        "--growth",
        type=float,
        default=None,
        help="创业板模块：机构预期净利润增速（小数）",
    )
    parser.add_argument(
        "--ndx-growth",
        type=float,
        default=None,
        help="纳斯达克100模块：预期盈利增速（小数）",
    )
    parser.add_argument(
        "--spx-growth",
        type=float,
        default=None,
        help="标普500模块：预期盈利增速（小数）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="不打印报告正文（仅推送）",
    )
    args = parser.parse_args(argv)
    config = load_config()

    try:
        report, sections = generate_reports(
            modules=args.modules,
            index_codes=args.index,
            cyb_growth=args.growth,
            ndx_growth=args.ndx_growth,
            spx_growth=args.spx_growth,
        )
    except (ValueError, RuntimeError) as exc:
        print(exc)
        return 1

    if not args.quiet:
        print_report(report)

    if not sections:
        print("无有效报告内容，跳过推送。")
        return 1

    modules = args.modules or [MODULE_ALL]
    if MODULE_ALL in modules or len(modules) > 1:
        title = build_push_title(sections)
    else:
        module = modules[0]
        titles = {
            "dividend": "红利信号",
            "a500": "A500信号",
            "hs300": "沪深300信号",
            "zz500": "中证500信号",
            "zz1000": "中证1000信号",
            "kc50": "科创50信号",
            "cyb": "创业板信号",
            "ndx": "纳指100信号",
            "spx": "标普500信号",
        }
        title = f"{titles[module]} {sections[0]['signal_short']}"

    if not push_to_wechat(title, report, config):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
