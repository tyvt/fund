"""生成当日买卖建议 HTML 面板（供定时任务使用；原微信推送已停用，改为生成面板）。"""

import argparse
import sys

from daily_panel import generate_panel
from report import MODULE_CHOICES


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成当日买卖建议 HTML 面板")
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
        path = generate_panel(modules=args.modules)
    except Exception as exc:  # noqa: BLE001 - 定时任务需可观测失败
        print(f"生成面板失败：{exc}")
        return 1

    print(f"已生成当日建议面板：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
