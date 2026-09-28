"""vision 能力注册表（capabilities.providers）。

只放各视觉 provider 的注册入口；注册表本身在 capabilities/vision.py。
"""
from __future__ import annotations

import sys as _sys
import os as _os

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))

from capabilities.providers.minimax_vision import MiniMaxVisionProvider  # noqa: E402

__all__ = ["MiniMaxVisionProvider"]
