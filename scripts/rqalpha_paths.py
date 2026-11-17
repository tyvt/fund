"""RQAlpha bundle path configuration shared by export helpers."""

from __future__ import annotations

import os

DEFAULT_RQALPHA_BUNDLE_PATH = r"D:\rqalpha\bundle"


def resolve_rqalpha_bundle_path(value: str | None = None) -> str:
    configured = os.environ.get("RQALPHA_BUNDLE_PATH") if value is None else value
    return configured if configured and configured.strip() else DEFAULT_RQALPHA_BUNDLE_PATH
