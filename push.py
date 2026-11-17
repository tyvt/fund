"""生成当日买卖建议 HTML 面板（供定时任务使用；原微信推送已停用，改为生成面板）。
当日建议卡片刷新到买卖口径回测面板 trade_inception_present.html，与历史图表共用一个 HTML。
"""

import argparse
import sys

from backtest_html import (
    DAILY_ADVICE_END,
    DAILY_ADVICE_START,
    refresh_daily_advice,
)
from config import BACKTEST_OUTPUT_DIR, BACKTEST_TRADE_PRESENT_LABEL
from report import MODULE_CHOICES

PANEL_PATH = BACKTEST_OUTPUT_DIR / f"{BACKTEST_TRADE_PRESENT_LABEL}.html"


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
        # 审核 P1：面板路径切换后必须校验区块已真正写入，避免静默产出空/错卡片
        content = path.read_text(encoding="utf-8")
        if (
            DAILY_ADVICE_START not in content
            or DAILY_ADVICE_END not in content
            or "advice-card" not in content
            or "生成失败" in content
        ):
            raise RuntimeError(
                f"面板 {path} 未包含有效的当日建议卡片（缺标记/空卡片/生成失败）"
            )
    except Exception as exc:  # noqa: BLE001 - 定时任务需可观测失败
        print(f"刷新当日建议失败：{exc}")
        return 1

    print(f"已刷新回测面板当日建议：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
