"""统一报告生成与本地查看入口。"""

import argparse
import sys

from config import CN_BROAD_INDICES, CYB_EXPECTED_GROWTH, US_INDEX_KEYS
from market_data import configure_stdout_utf8, get_gov_bond_yield_history
from signal_format import join_index_sections, log_fetch_done, log_fetch_start

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
    "000016": "sz50",
    "000300": "hs300",
    "000905": "zz500",
    "000852": "zz1000",
    "930050": "a50",
    "000903": "a100",
    "000688": "kc50",
}


def print_report(report):
    print("\n--- 数据分析 ---")
    for line in report.splitlines():
        print(line)
    print("----------------")


def _log_snapshot_ready(snapshot):
    from live_snapshot import format_live_meta_extra

    live_extra = format_live_meta_extra(snapshot)
    log_fetch_done(
        snapshot.get("name", "—"),
        code=snapshot.get("code"),
        data_date=snapshot.get("data_date") or snapshot.get("date"),
        history_start=snapshot.get("history_start"),
        history_days=snapshot.get("history_days"),
        extra=live_extra,
    )


def generate_dividend_report(index_codes=None, live_quotes=None):
    from core import generate_report

    return generate_report(index_codes, live_quotes=live_quotes)


def generate_cn_broad_report(index_meta, bond_history=None, live_quotes=None):
    from cn_broad_data import fetch_cn_broad_snapshot
    from cn_broad_signal import (
        evaluate_cn_broad_buy,
        format_cn_broad_report,
        format_cn_broad_section,
    )
    from live_snapshot import maybe_apply_live

    log_fetch_start(index_meta["name"], index_meta["code"])
    if bond_history is None:
        bond_history = get_gov_bond_yield_history()
    snapshot = fetch_cn_broad_snapshot(index_meta["code"], bond_history)
    snapshot = maybe_apply_live(snapshot, live_quotes)
    _log_snapshot_ready(snapshot)
    buy_eval = evaluate_cn_broad_buy(snapshot)
    module = CN_BROAD_MODULE_BY_CODE.get(index_meta["code"], "cn_broad")
    section = format_cn_broad_section(snapshot, buy_eval, module=module)
    return format_cn_broad_report(
        snapshot, section, title=f"{index_meta['name']} 投资信号"
    )


def generate_cn_broad_reports(live_quotes=None):
    from concurrent.futures import ThreadPoolExecutor

    bond_history = get_gov_bond_yield_history()
    with ThreadPoolExecutor(max_workers=len(CN_BROAD_INDICES)) as executor:
        results = list(
            executor.map(
                lambda meta: generate_cn_broad_report(
                    meta, bond_history, live_quotes=live_quotes
                ),
                CN_BROAD_INDICES,
            )
        )
    sections = [section for _, section in results]
    return join_index_sections(sections), sections


def generate_cyb_report(expected_growth=None, live_quotes=None):
    from cyb_data import fetch_cyb_snapshot
    from cyb_signal import evaluate_cyb_signal, format_cyb_report, format_cyb_section
    from live_snapshot import maybe_apply_live

    if expected_growth is None:
        expected_growth = CYB_EXPECTED_GROWTH
    log_fetch_start("创业板指", "399006")
    snapshot = fetch_cyb_snapshot(expected_growth=expected_growth)
    snapshot = maybe_apply_live(snapshot, live_quotes)
    _log_snapshot_ready(snapshot)
    signal_eval = evaluate_cyb_signal(snapshot)
    section = format_cyb_section(snapshot, signal_eval)
    return format_cyb_report(snapshot, section)


def generate_hstech_report(expected_growth=None, live_quotes=None):
    from hstech_data import fetch_hstech_snapshot
    from hstech_signal import (
        evaluate_hstech_signal,
        format_hstech_report,
        format_hstech_section,
    )
    from live_snapshot import maybe_apply_live

    log_fetch_start("恒生科技指数", "HSTECH")
    snapshot = fetch_hstech_snapshot(expected_growth=expected_growth)
    snapshot = maybe_apply_live(snapshot, live_quotes)
    _log_snapshot_ready(snapshot)
    signal_eval = evaluate_hstech_signal(snapshot)
    section = format_hstech_section(snapshot, signal_eval)
    return format_hstech_report(snapshot, section)


def generate_us_index_report(key, expected_growth=None, live_quotes=None):
    from live_snapshot import maybe_apply_live
    from us_index_data import fetch_snapshot
    from us_index_signal import evaluate_signal, format_report, format_section

    index_name = {"ndx": "纳斯达克100", "spx": "标普500"}[key]
    code = {"ndx": "NDX", "spx": "SPX"}[key]
    log_fetch_start(index_name, code)
    snapshot = fetch_snapshot(key, expected_growth=expected_growth)
    snapshot = maybe_apply_live(snapshot, live_quotes)
    _log_snapshot_ready(snapshot)
    signal_eval = evaluate_signal(key, snapshot)
    section = format_section(key, snapshot, signal_eval)
    return format_report(key, snapshot, section)


def generate_us_reports(expected_growth=None, live_quotes=None):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(US_INDEX_KEYS)) as executor:
        results = list(
            executor.map(
                lambda key: generate_us_index_report(
                    key, expected_growth=expected_growth, live_quotes=live_quotes
                ),
                US_INDEX_KEYS,
            )
        )
    sections = [section for _, section in results]
    return join_index_sections(sections), sections


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
    from concurrent.futures import ThreadPoolExecutor
    from config import BUY_AMOUNT_RANKING_ENABLED
    from realtime_quote import fetch_live_quotes

    if BUY_AMOUNT_RANKING_ENABLED:
        from buy_amount_ranking import get_ranking_allocation

        get_ranking_allocation()

    live_quotes = fetch_live_quotes()

    resolved = _resolve_modules(modules)
    generators = {
        MODULE_DIVIDEND: lambda: generate_dividend_report(
            index_codes, live_quotes=live_quotes
        ),
        MODULE_CN_BROAD: lambda: generate_cn_broad_reports(live_quotes=live_quotes),
        MODULE_CYB: lambda: generate_cyb_report(cyb_growth, live_quotes=live_quotes),
        MODULE_HSTECH: lambda: generate_hstech_report(live_quotes=live_quotes),
        MODULE_US: lambda: generate_us_reports(us_growth, live_quotes=live_quotes),
    }

    if len(resolved) == 1:
        module = resolved[0]
        _, module_sections = generators[module]()
        all_sections = (
            module_sections
            if isinstance(module_sections, list)
            else [module_sections]
        )
    else:
        all_sections = []
        with ThreadPoolExecutor(max_workers=len(resolved)) as executor:
            futures = [
                (module, executor.submit(generators[module]))
                for module in resolved
            ]
            for module, future in futures:
                _, module_sections = future.result()
                if isinstance(module_sections, list):
                    all_sections.extend(module_sections)
                else:
                    all_sections.append(module_sections)

    return join_index_sections(all_sections), all_sections


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
