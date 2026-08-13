# -*- coding: utf-8 -*-
"""全量 DuckDB 同步 → 因子优化，带时间戳日志（stdout + 文件）。"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG_DIR = REPO / "logs"
DEFAULT_LOG = LOG_DIR / "pipeline_sync_optimize.log"
OPT_LOG = LOG_DIR / "optimize_enhanced_factors.log"


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class PipelineLogger:
    def __init__(self, path: Path):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8", buffering=1)

    def log(self, msg: str) -> None:
        line = f"[{_ts()}] {msg}"
        print(line, flush=True)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def _run_step(
    logger: PipelineLogger,
    title: str,
    cmd: list[str],
    *,
    cwd: Path,
    env: dict | None = None,
) -> int:
    logger.log(f"{'=' * 60}")
    logger.log(f"开始: {title}")
    logger.log(f"命令: {' '.join(cmd)}")
    logger.log(f"{'=' * 60}")
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    last_beat = time.monotonic()
    for line in proc.stdout:
        text = line.rstrip("\n\r")
        if text:
            logger.log(f"  | {text}")
        now = time.monotonic()
        if now - last_beat >= 300:
            logger.log(f"  … 仍在运行（已 {int(now - t0)}s）")
            last_beat = now
    code = proc.wait()
    elapsed = time.perf_counter() - t0
    if code == 0:
        logger.log(f"完成: {title}（{elapsed:.1f}s）")
    else:
        logger.log(f"失败: {title} exit_code={code}（{elapsed:.1f}s）")
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DuckDB 全量同步后自动跑因子优化")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="流水线日志路径")
    parser.add_argument("--skip-sync", action="store_true", help="跳过同步，仅优化")
    parser.add_argument("--sync-only", action="store_true", help="仅同步，不优化")
    parser.add_argument("--force", action="store_true", help="同步时强制刷新公网缓存")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--end", default="2025-08-01")
    parser.add_argument("--trials", type=int, default=30)
    args = parser.parse_args(argv)

    logger = PipelineLogger(args.log)
    env = {**dict(__import__("os").environ), "PYTHONUNBUFFERED": "1"}
    py = sys.executable

    try:
        logger.log("流水线启动: 同步 → 因子优化")

        if not args.skip_sync:
            code = _run_step(
                logger,
                "DuckDB 全量同步 (sync_market_duckdb)",
                [py, str(REPO / "sync_market_duckdb.py")] + (["--force"] if args.force else []),
                cwd=REPO,
                env=env,
            )
            if code != 0:
                logger.log("同步失败，已中止，不启动因子优化")
                return code
        else:
            logger.log("跳过同步（--skip-sync）")

        if args.sync_only:
            logger.log("仅同步模式（--sync-only），结束")
            return 0

        opt_cmd = [
            py,
            str(REPO / "scripts" / "optimize_enhanced_factors.py"),
            "--task",
            "all",
            "--years",
            str(args.years),
            "--end",
            args.end,
            "--trials",
            str(args.trials),
        ]
        logger.log(f"因子优化日志同时写入: {OPT_LOG}")
        code = _run_step(
            logger,
            "增强因子阈值优化",
            opt_cmd,
            cwd=REPO,
            env=env,
        )
        if code == 0:
            logger.log("流水线全部完成")
        else:
            logger.log(f"因子优化失败 exit_code={code}")
        return code
    finally:
        logger.close()


if __name__ == "__main__":
    sys.exit(main())
