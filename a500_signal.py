"""中证 A500 买入信号与报告格式化。"""

from cn_broad_signal import (
    evaluate_cn_broad_buy as evaluate_a500_buy,
    format_cn_broad_report,
    format_cn_broad_section,
)


def format_a500_section(snapshot, buy_eval):
    return format_cn_broad_section(snapshot, buy_eval, module="a500")


def format_a500_report(snapshot, section):
    return format_cn_broad_report(snapshot, section, title="中证A500 投资信号")
