"""独立能力层（desk-agent）。本批只做 vision。

形态参照 Hermes Agent：每个能力有自己独立的 provider/model/配置（capabilities.<name> 段），
走自己的调用路径，不占用主聊天模型的循环。
"""
from __future__ import annotations

import sys as _sys
import os as _os

# 保证平铺的 python/ 目录可导入（子包被当脚本或从任意 cwd 导入时都成立）
_here = _os.path.dirname(_os.path.abspath(__file__))
_parent = _os.path.dirname(_here)
if _parent not in _sys.path:
    _sys.path.insert(0, _parent)

from capabilities.vision_provider import (  # noqa: E402,F401
    VISION_ASPECT_HINT,
    VISION_ERROR_CODES,
    VisionError,
    VisionProvider,
    VisionResult,
    fail_result,
    ok_result,
    split_content_text,
)
from capabilities.vision import (  # noqa: E402,F401
    VisionConfig,
    get_provider,
    load_vision_config,
    register_provider,
    vision_analyze,
)
