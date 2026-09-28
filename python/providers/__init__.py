"""providers 包：每家后端一个文件。

导入即注册不是这里的策略 —— 注册由调用方（sidecar / 测试）显式做，
这里只暴露类，避免 import 副作用把测试环境搞脏。
"""
from providers.image_dashscope import DashscopeImageProvider  # noqa: F401
from providers.image_minimax import MiniMaxImageProvider  # noqa: F401
from providers.video_minimax import MiniMaxVideoProvider  # noqa: F401

__all__ = [
    "DashscopeImageProvider",
    "MiniMaxImageProvider",
    "MiniMaxVideoProvider",
]
