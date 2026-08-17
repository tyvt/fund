# -*- coding: utf-8 -*-
"""参数扫描日志巡检：每 N 分钟检查是否卡住或结果异常。"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "output" / "dividend_lowvol" / "constraint_param_sweep.log"
WATCH_LOG = ROOT / "output" / "dividend_lowvol" / "constraint_param_sweep_watch.log"
PARTIAL = ROOT / "output" / "dividend_lowvol" / "constraint_param_sweep_partial.csv"

STALE_SEC = 15 * 60
FAST_WINDOW_SEC = 45
LOW_TRADES = 30
LINE_RE = re.compile(
    r"CAGR=(?P<cagr>-?[\d.]+)% maxDD=(?P<dd>-?[\d.]+)% trades=(?P<trades>\d+) \((?P<sec>[\d.]+)s\)"
)


def _log(msg: str) -> None:
    WATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%H:%M:%S} | {msg}"
    print(line, flush=True)
    with WATCH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _sweep_pids() -> list[int]:
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "CommandLine like '%constraint_param_sweep%'", "get", "ProcessId"],
            text=True,
            errors="replace",
        )
        pids = [int(x) for x in out.split() if x.isdigit()]
        return pids
    except Exception:
        return []


def _check_once() -> list[str]:
    issues: list[str] = []
    if not LOG_PATH.exists():
        issues.append("日志文件不存在")
        return issues

    text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    if "===== RERUN" in text:
        text = text.split("===== RERUN", 1)[1]
    elif "参数扫描开始" in text:
        text = text.rsplit("参数扫描开始", 1)[-1]
    age = time.time() - LOG_PATH.stat().st_mtime
    pids = _sweep_pids()

    if pids and age > STALE_SEC:
        issues.append(f"日志 {age/60:.0f} 分钟无更新（进程仍在跑 pid={pids}）")

    for line in text.splitlines()[-40:]:
        m = LINE_RE.search(line)
        if not m:
            continue
        sec = float(m.group("sec"))
        trades = int(m.group("trades"))
        if sec < FAST_WINDOW_SEC:
            issues.append(
                f"异常快窗口: {sec:.0f}s trades={trades} (<{FAST_WINDOW_SEC}s) | {line[-80:]}"
            )
        elif trades < LOW_TRADES and "maxDD=0.00%" in line:
            issues.append(f"可疑窗口 trades={trades} | {line[-80:]}")

    if PARTIAL.exists():
        lines = PARTIAL.read_text(encoding="utf-8-sig", errors="replace").strip().splitlines()
        if len(lines) > 1:
            _log(f"进度 partial 行数={len(lines)-1}（含表头）")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=600, help="巡检间隔秒，默认 600")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    _log(f"监控启动 interval={args.interval}s log={LOG_PATH}")
    while True:
        issues = _check_once()
        tail = ""
        if LOG_PATH.exists():
            tail = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-1]
        pids = _sweep_pids()
        _log(f"进程={pids or '无'} | 末行: {tail[:120]}")
        if issues:
            for it in issues:
                _log(f"WARN {it}")
        else:
            _log("OK 正常")
        if args.once:
            return 1 if issues else 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
