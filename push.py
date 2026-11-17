"""生成当日买卖建议 HTML 面板（供定时任务使用；原微信推送已停用，改为生成面板）。
当日建议卡片直接刷新到回测面板 inception_present.html，与历史图表共用一个 HTML。
"""

import argparse
import sys

from backtest_html import refresh_daily_advice
from config import BACKTEST_OUTPUT_DIR, BACKTEST_PRESENT_LABEL
from report import MODULE_CHOICES

PANEL_PATH = BACKTEST_OUTPUT_DIR / f"{BACKTEST_PRESENT_LABEL}.html"


def main(argv=None):
    parser = argparse.ArgumentParser(description="刷新回测面板中的当日买卖建议卡片")
    parser.add_argument(
        "-m",
        "--module",
        action="append",
        choices=MODULE_CHOICES,
        dest="modules",
        help="生成模块：cn_broad、cyb、us、all(全部，默认)",
    )
    args = parser.parse_args(argv)

    try:
        path = refresh_daily_advice(PANEL_PATH, modules=args.modules)
    except Exception as exc:  # noqa: BLE001 - 定时任务需可观测失败
        print(f"刷新当日建议失败：{exc}")
        return 1

    print(f"已刷新回测面板当日建议：{path}")
    return 0
