# -*- coding: utf-8 -*-
"""RQAlpha Mod：在引擎启动时用 ParquetDataSource 替换 bundle。"""

from __future__ import annotations

from rqalpha.interface import AbstractMod


def load_mod():
    return ParquetDataMod()


class ParquetDataMod(AbstractMod):
    def start_up(self, env, mod_config):
        from data.parquet_data_source import ParquetDataSource

        root = getattr(mod_config, "parquet_root", None)
        env.set_data_source(ParquetDataSource(root))

    def tear_down(self, code, exception=None):
        return None
