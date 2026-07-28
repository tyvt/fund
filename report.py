"""统一报告生成与本地查看入口。"""

import argparse
import sys

from config import CN_BROAD_INDICES, CYB_EXPECTED_GROWTH, US_INDEX_KEYS
from market_data import configure_stdout_utf8, get_gov_bond_yield_history

MODULE_DIVIDEND = "dividend"
MODULE_CN_BROAD = "cn_broad"
MODULE_CYB = "cyb"
MODULE_HSTECH = "hstech"
MODULE_US = "us"
MODULE_ALL = "all"
MODULE_CHOICES = (
    MODULE_DIVIDEND,
    MODULE_CN_BROAD,
    MODULE_CYB,
    MODULE_HSTECH,
    MODULE_US,
    MODULE_ALL,
)

CN_BROAD_MODULE_BY_CODE = {
    "000510": "a500",
    "000300": "hs300",
    "000905": "zz500",
    "000852": "zz1000",
    "000688": "kc50",
}


def print_report(report):
    print("\n--- 数据分析 ---")
    for line in report.splitlines():
        print(line)
    print("----------------")


def generate_dividend_report(index_codes=None):
    from core import generate_report

    print("正在获取红利指数数据，请稍候...")
    return generate_report(index_codes)


def generate_cn_broad_report(index_meta):
    from cn_broad_data import fetch_cn_broad_snapshot
    from cn_broad_signal import (
        evaluate_cn_broad_buy,
        format_cn_broad_report,
        format_cn_broad_section,
    )

    print(f"正在获取{index_meta['name']}数据，请稍候...")
    bond_history = get_gov_bond_yield_history()
    snapshot = fetch_cn_broad_snapshot(index_meta["code"], bond_history)
    buy_eval = evaluate_cn_broad_buy(snapshot)
    module = CN_BROAD_MODULE_BY_CODE.get(index_meta["code"], "cn_broad")
    section = format_cn_broad_section(snapshot, buy_eval, module=module)
    return format_cn_broad_report(
        snapshot, section, title=f"{index_meta['name']} 投资信号"
    )


def generate_cn_broad_reports():
    report_parts = []
    sections = []
    for index_meta in CN_BROAD_INDICES:
        report, section = generate_cn_broad_report(index_meta)
        if report_parts:
            report_parts.append("")
        report_parts.append(report)
        sections.append(section)
    return "\n".join(report_parts), sections


def generate_cyb_report(expected_growth=None):
    from cyb_data import fetch_cyb_snapshot
    from cyb_signal import evaluate_cyb_signal, format_cyb_report, format_cyb_section

    if expected_growth is None:
        expected_growth = CYB_EXPECTED_GROWTH
    print("正在获取创业板指数据，请稍候...")
    snapshot = fetch_cyb_snapshot(expected_growth=expected_growth)
    signal_eval = evaluate_cyb_signal(snapshot)
    section = format_cyb_section(snapshot, signal_eval)
    return format_cyb_report(snapshot, section)


def generate_hstech_report(expected_growth=None):
    from hstech_data import fetch_hstech_snapshot
    from hstech_signal import (
        evaluate_hstech_signal,
        format_hstech_report,
        format_hstech_section,
    )

    print("正在获取恒生科技指数数据，请稍候...")
    snapshot = fetch_hstech_snapshot(expected_growth=expected_growth)
    signal_eval = evaluate_hstech_signal(snapshot)
    section = format_hstech_section(snapshot, signal_eval)
    return format_hstech_report(snapshot, section)


def generate_us_index_report(key, expected_growth=None):
    from us_index_data import fetch_snapshot
    from us_index_signal import evaluate_signal, format_report, format_section

    index_name = {"ndx": "纳斯达克 100", "spx": "标普 500"}[key]
    print(f"正在获取{index_name}数据，请稍候...")
    snapshot = fetch_snapshot(key, expected_growth=expected_growth)
    signal_eval = evaluate_signal(key, snapshot)
    section = format_section(key, snapshot, signal_eval)
    return format_report(key, snapshot, section)


def generate_us_reports(expected_growth=None):
    report_parts = []
    sections = []
    for key in US_INDEX_KEYS:
        report, section = generate_us_index_report(key, expected_growth=expected_growth)
        if report_parts:
            report_parts.append("")
        report_parts.append(report)
        sections.append(section)
    return "\n".join(report_parts), sections


def _resolve_modules(modules):
    if not modules or MODULE_ALL in modules:
        return [
            MODULE_DIVIDEND,
            MODULE_CN_BROAD,
            MODULE_CYB,
            MODULE_HSTECH,
            MODULE_US,
        ]
    return modules


def generate_reports(
    modules=None, index_codes=None, cyb_growth=None, us_growth=None
):
    """按模块生成报告；modules 含 all 或未指定时生成全部。"""
    resolved = _resolve_modules(modules)
    report_parts = []
    sections = []

    generators = {
        MODULE_DIVIDEND: lambda: generate_dividend_report(index_codes),
        MODULE_CN_BROAD: generate_cn_broad_reports,
        MODULE_CYB: lambda: generate_cyb_report(cyb_growth),
        MODULE_HSTECH: generate_hstech_report,
        MODULE_US: lambda: generate_us_reports(us_growth),
    }

    for module in resolved:
        report, module_sections = generators[module]()
        if report_parts:
            report_parts.append("")
        report_parts.append(report)
        if isinstance(module_sections, list):
            sections.extend(module_sections)
        else:
            sections.append(module_sections)

    return "\n".join(report_parts), sections


def main(argv=None):
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="投资信号报告（本地查看，不推送）")
    parser.add_argument(
        "-m",
        "--module",
        action="append",
        choices=MODULE_CHOICES,
        dest="modules",
        help="报告模块：dividend(红利)、cn_broad(A股宽基)、cyb(创业板)、hstech(恒生科技)、us(纳指+标普)、all(全部，默认)",
    )
    parser.add_argument(
        "--index",
        action="append",
        metavar="CODE",
        help="红利模块：仅分析指定指数（可多次指定）",
    )
    parser.add_argument(
        "--growth",
        type=float,
        default=None,
        help="创业板模块：机构预期净利润增速（小数，默认读取配置）",
    )
    parser.add_argument(
        "--us-growth",
        type=float,
        default=None,
        help="美股模块：预期盈利增速（小数，纳指/标普共用，默认由 TTM/Forward PE 隐含）",
    )
    args = parser.parse_args(argv)

    try:
        report, _sections = generate_reports(
            modules=args.modules,
            index_codes=args.index,
            cyb_growth=args.growth,
            us_growth=args.us_growth,
        )
    except (ValueError, RuntimeError) as exc:
        print(exc)
        return 1

    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
