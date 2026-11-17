"""生成“当日买卖建议”静态 HTML 面板：每天跑一次刷新一份，浏览器打开即可查看。

替代原微信推送的呈现方式：数据仍取自 report.generate_reports（含当日实时快照），
把全部 6 只指数的完整建议渲染成一个自包含 HTML 文件。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from html import escape
from pathlib import Path

from config import PROJECT_DIR
from report import MODULE_ALL, MODULE_CHOICES, generate_reports
from signal_format import SIGNAL_BUY, SIGNAL_SELL

PANEL_DIR = PROJECT_DIR / "output" / "panel"
PANEL_FILE = PANEL_DIR / "daily_signals.html"


def _signal_class(signal_short):
    if signal_short == SIGNAL_BUY:
        return "sig-buy"
    if signal_short == SIGNAL_SELL:
        return "sig-sell"
    return "sig-hold"


def _render_card(section, index):
    code = str(section.get("code") or "")
    name = str(section.get("name") or "")
    signal_short = str(section.get("signal_short") or "")
    text = str(section.get("text") or "")
    score = section.get("score")
    total = section.get("total")

    score_html = ""
    if score is not None and total:
        score_html = f'<span class="score">{escape(str(score))}/{escape(str(total))}</span>'

    title = " ".join(x for x in (code, name) if x) or f"指数 {index + 1}"
    return f"""
    <section class="card {_signal_class(signal_short)}">
      <header class="card-head">
        <span class="card-title">{escape(title)}</span>
        <span class="badge">{escape(signal_short)}</span>
        {score_html}
      </header>
      <pre class="card-body">{escape(text)}</pre>
    </section>"""


def render_panel(sections, generated_at=None):
    generated_at = generated_at or datetime.now()
    cards = "".join(_render_card(s, i) for i, s in enumerate(sections))
    if not cards:
        cards = '<p class="empty">今日暂无可展示的建议数据。</p>'
    ts = generated_at.strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>当日买卖建议 · {escape(ts)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f4f5f7; color: #1f2328; padding: 16px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #6b7280; font-size: 13px; margin-bottom: 16px; }}
  .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }}
  .card {{ border-radius: 10px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.08);
          overflow: hidden; border-left: 6px solid #9ca3af; align-self: start; }}
  .sig-buy {{ border-left-color: #16a34a; }}
  .sig-sell {{ border-left-color: #dc2626; }}
  .sig-hold {{ border-left-color: #9ca3af; }}
  .card-head {{ display: flex; align-items: center; gap: 8px; padding: 12px 14px;
               border-bottom: 1px solid #eef0f2; flex-wrap: wrap; }}
  .card-title {{ font-weight: 600; font-size: 15px; }}
  .badge {{ font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #eef0f2; }}
  .sig-buy .badge {{ background: #dcfce7; color: #166534; }}
  .sig-sell .badge {{ background: #fee2e2; color: #991b1b; }}
  .score {{ margin-left: auto; font-size: 12px; color: #6b7280; }}
  .card-body {{ margin: 0; padding: 12px 14px; white-space: pre-wrap; word-break: break-word;
               font-family: "Consolas", "Menlo", monospace; font-size: 13px; line-height: 1.5; }}
  .empty {{ color: #6b7280; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0d1117; color: #e6edf3; }}
    .card {{ background: #161b22; box-shadow: none; }}
    .card-head {{ border-bottom-color: #21262d; }}
    .badge {{ background: #21262d; }}
  }}
</style>
</head>
<body>
  <h1>当日买卖建议</h1>
  <div class="meta">生成时间：{escape(ts)} · 共 {len(sections)} 只指数</div>
  <div class="grid">{cards}
  </div>
</body>
</html>
"""


def generate_panel(modules=None):
    """生成面板 HTML 文件，返回输出路径。异常/空数据时写出降级页并抛错。"""
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _, sections = generate_reports(modules=modules or [MODULE_ALL])
    except Exception as exc:  # noqa: BLE001 - 定时任务需可观测失败
        sections = []
        html = render_panel(sections).replace(
            "今日暂无可展示的建议数据。",
            f"生成失败：{escape(str(exc))}",
        )
        PANEL_FILE.write_text(html, encoding="utf-8")
        raise
    html = render_panel(sections)
    PANEL_FILE.write_text(html, encoding="utf-8")
    return PANEL_FILE


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成当日买卖建议 HTML 面板")
    parser.add_argument(
        "-m", "--module", action="append", choices=MODULE_CHOICES,
        dest="modules", help="生成模块：cn_broad、cyb、us、all(默认)",
    )
    args = parser.parse_args(argv)
    try:
        path = generate_panel(modules=args.modules)
    except Exception as exc:  # noqa: BLE001
        print(f"生成面板失败：{exc}")
        return 1
    print(f"已生成当日建议面板：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
