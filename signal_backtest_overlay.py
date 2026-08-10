"""回测时临时覆盖买卖点阈值（环境变量 / config / 模块属性）。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

import config


def clear_signal_caches() -> None:
    if hasattr(config.get_cn_broad_signal_config, "cache_clear"):
        config.get_cn_broad_signal_config.cache_clear()


@contextmanager
def signal_backtest_overlay(patches: dict | None):
    """应用单次试验的参数覆盖，退出后恢复。"""
    if not patches:
        yield
        return

    old_env: dict[str, str | None] = {}
    old_config: dict[str, Any] = {}
    old_cyb: dict[str, Any] = {}
    old_rotation: dict[str, Any] = {}

    try:
        for key, val in patches.get("env", {}).items():
            old_env[key] = os.environ.get(key)
            os.environ[key] = str(val)

        for attr, val in patches.get("config_attrs", {}).items():
            old_config[attr] = getattr(config, attr)
            setattr(config, attr, val)

        cyb_mod = None
        if patches.get("cyb_attrs"):
            import cyb_signal as cyb_mod  # noqa: F811

            for attr, val in patches["cyb_attrs"].items():
                old_cyb[attr] = getattr(cyb_mod, attr)
                setattr(cyb_mod, attr, val)

        rot_mod = None
        if patches.get("rotation_sell_attrs"):
            import rotation_sell as rot_mod  # noqa: F811

            for attr, val in patches["rotation_sell_attrs"].items():
                old_rotation[attr] = getattr(rot_mod, attr)
                setattr(rot_mod, attr, val)

        clear_signal_caches()
        yield
    finally:
        for key, val in old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

        for attr, val in old_config.items():
            setattr(config, attr, val)

        if old_cyb:
            import cyb_signal as cyb_mod

            for attr, val in old_cyb.items():
                setattr(cyb_mod, attr, val)

        if old_rotation:
            import rotation_sell as rot_mod

            for attr, val in old_rotation.items():
                setattr(rot_mod, attr, val)

        clear_signal_caches()


@contextmanager
def merge_overlays(*patch_dicts: dict):
    merged: dict[str, dict] = {}
    for p in patch_dicts:
        if not p:
            continue
        for kind, items in p.items():
            merged.setdefault(kind, {}).update(items)
    with signal_backtest_overlay(merged if merged else None):
        yield
