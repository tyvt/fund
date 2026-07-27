"""统一报告生成与本地查看入口。"""

import argparse
import sys

from config import CYB_EXPECTED_GROWTH, NDX_EXPECTED_GROWTH, SPX_EXPECTED_GROWTH
from market_data import configure_stdout_utf8, get_gov_bond_yield_history

MODULE_DIVIDEND = "dividend"
MODULE_A500 = "a500"
MODULE_HS300 = "hs300"
MODULE_ZZ500 = "zz500"
MODULE_ZZ1000 = "zz1000"
MODULE_KC50 = "kc50"
MODULE_CYB = "cyb"
MODULE_HSTECH = "hstech"
MODULE_NDX = "ndx"
MODULE_SPX = "spx"
MODULE_ALL = "all"
MODULE_CHOICES = (
    MODULE_DIVIDEND,
    MODULE_A500,
    MODULE_HS300,
    MODULE_ZZ500,
    MODULE_ZZ1000,
    MODULE_KC50,
    MODULE_CYB,
    MODULE_HSTECH,
    MODULE_NDX,
    MODULE_SPX,
    MODULE_ALL,
)


def print_report(report):
    print("\n--- 数据分析 ---")
    for line in report.splitlines():
        print(line)
    print("----------------")


def generate_dividend_report(index_codes=None):
    from core import generate_report

    print("正在获取红利指数数据，请稍候...")
    return generate_report(index_codes)


def generate_a500_report():
    from config import A500_INDEX

    return generate_cn_broad_report(A500_INDEX)


def generate_hs300_report():
    from config import HS300_INDEX

    return generate_cn_broad_report(HS300_INDEX)


def generate_zz500_report():
    from config import ZZ500_INDEX

    return generate_cn_broad_report(ZZ500_INDEX)


def generate_zz1000_report():
    from config import ZZ1000_INDEX

    return generate_cn_broad_report(ZZ1000_INDEX)


def generate_kc50_report():
    from config import KC50_INDEX

    return generate_cn_broad_report(KC50_INDEX)


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
    module = {
        "000510": "a500",
        "000300": "hs300",
        "000905": "zz500",
        "000852": "zz1000",
        "000688": "kc50",
    }.get(index_meta["code"], "cn_broad")
    section = format_cn_broad_section(snapshot, buy_eval, module=module)
    return format_cn_broad_report(
        snapshot, section, title=f"{index_meta['name']} 投资信号"
    )


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


def generate_ndx_report(expected_growth=None):
    from ndx_data import fetch_ndx_snapshot
    from ndx_signal import evaluate_ndx_signal, format_ndx_report, format_ndx_section

    if expected_growth is None:
        expected_growth = NDX_EXPECTED_GROWTH
    print("正在获取纳斯达克 100 数据，请稍候...")
    snapshot = fetch_ndx_snapshot(expected_growth=expected_growth)
    signal_eval = evaluate_ndx_signal(snapshot)
    section = format_ndx_section(snapshot, signal_eval)
    return format_ndx_report(snapshot, section)


def generate_spx_report(expected_growth=None):
    from spx_data import fetch_spx_snapshot
    from spx_signal import evaluate_spx_signal, format_spx_report, format_spx_section

    if expected_growth is None:
        expected_growth = SPX_EXPECTED_GROWTH
    print("正在获取标普 500 数据，请稍候...")
    snapshot = fetch_spx_snapshot(expected_growth=expected_growth)
    signal_eval = evaluate_spx_signal(snapshot)
    section = format_spx_section(snapshot, signal_eval)
    return format_spx_report(snapshot, section)


def _resolve_modules(modules):
    if not modules or MODULE_ALL in modules:
        return [
            MODULE_DIVIDEND,
            MODULE_A500,
            MODULE_HS300,
            MODULE_ZZ500,
            MODULE_ZZ1000,
            MODULE_KC50,
            MODULE_CYB,
            MODULE_HSTECH,
            MODULE_NDX,
            MODULE_SPX,
        ]
    return modules


def generate_reports(
    modules=None, index_codes=None, cyb_growth=None, ndx_growth=None, spx_growth=None
):
    """按模块生成报告；modules 含 all 或未指定时生成全部。"""
    resolved = _resolve_modules(modules)
    report_parts = []
    sections = []

    generators = {
        MODULE_DIVIDEND: lambda: generate_dividend_report(index_codes),
        MODULE_A500: generate_a500_report,
        MODULE_HS300: generate_hs300_report,
        MODULE_ZZ500: generate_zz500_report,
        MODULE_ZZ1000: generate_zz1000_report,
        MODULE_KC50: generate_kc50_report,
        MODULE_CYB: lambda: generate_cyb_report(cyb_growth),
        MODULE_HSTECH: generate_hstech_report,
        MODULE_NDX: lambda: generate_ndx_report(ndx_growth),
        MODULE_SPX: lambda: generate_spx_report(spx_growth),
    }

    for module in resolved:
        report, module_sections = generators[module]()
        if report_parts:
            report_parts.append("")
        report_parts.append(report)
        if isinstance(module_sections, dict):
            sections.append(module_sections)
        else:
            sections.extend(module_sections)

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
        help="报告模块：dividend(红利)、a500(中证A500/000510)、hs300(沪深300)、zz500(中证500/000905)、zz1000(中证1000)、kc50(科创50)、cyb(创业板)、hstech(恒生科技)、ndx(纳斯达克100)、spx(标普500)、all(全部，默认)",
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
        "--ndx-growth",
        type=float,
        default=None,
        help="纳斯达克100模块：预期盈利增速（小数，默认由 TTM/Forward PE 隐含）",
    )
    parser.add_argument(
        "--spx-growth",
        type=float,
        default=None,
        help="标普500模块：预期盈利增速（小数，默认由 TTM/Forward PE 隐含）",
    )
    args = parser.parse_args(argv)

    try:
        report, _sections = generate_reports(
            modules=args.modules,
            index_codes=args.index,
            cyb_growth=args.growth,
            ndx_growth=args.ndx_growth,
            spx_growth=args.spx_growth,
        )
    except (ValueError, RuntimeError) as exc:
        print(exc)
        return 1

    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
