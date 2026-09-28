"""usage.summary 必须真的被路由到 —— 别只声明 handler 就以为接上了。

教训（2026-09-25 真机）：我第一次把分支插进 sessions 的 startswith 保护里，
第二次建了 `_handle_usage_rpc` 却忘了在分发点挂路由，两次真机都报
`unknown method: usage.summary`，但本地测试全绿 —— 因为没人测「路由」这一层。
这个测试就是钉这一层：声明在 CAPABILITIES 里的方法，调用必须不是 BAD_METHOD。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "python"))

import sidecar  # noqa: E402


class UsageRpcRoutingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # _DATA_DIR 是模块级常量（import 时就定了），改环境变量已经晚了 ——
        # 直接改常量 + 清掉缓存的 store，测试才不会往仓库 data/ 里写。
        self._old_dir = sidecar._DATA_DIR
        self._old_store = getattr(sidecar, "_SESSIONS", None)
        sidecar._DATA_DIR = self._tmp.name
        sidecar._SESSIONS = None

    def tearDown(self):
        sidecar._SESSIONS = self._old_store
        sidecar._DATA_DIR = self._old_dir
        self._tmp.cleanup()

    def test_usage_summary_is_advertised(self):
        self.assertIn("usage.summary", sidecar.SIDECAR_CAPABILITIES)

    def test_usage_summary_is_routed_not_bad_method(self):
        resp = sidecar.handle_request({
            "type": "req", "id": "t1", "method": "usage.summary",
            "params": {"days": 7, "protocolVersion": sidecar.SIDECAR_PROTOCOL_VERSION},
        })
        self.assertNotEqual(resp.get("error", {}).get("code"), "BAD_METHOD",
                            "usage.summary 又没挂上路：%r" % (resp,))

    def test_usage_summary_returns_aggregate_shape(self):
        resp = sidecar.handle_request({
            "type": "req", "id": "t2", "method": "usage.summary",
            "params": {"days": 7, "protocolVersion": sidecar.SIDECAR_PROTOCOL_VERSION},
        })
        result = resp.get("result") or {}
        for key in ("total", "today", "window", "byModel", "bySession", "byDay"):
            self.assertIn(key, result, "聚合结果缺字段 %s：%r" % (key, sorted(result)))
        self.assertEqual(result.get("windowDays"), 7)
        # 顺带钉死隔离：库得落在临时目录，别写进仓库 data/
        self.assertTrue(os.path.exists(os.path.join(self._tmp.name, "sessions.db")),
                        "usage 测试没落在临时目录，跑到仓库 data/ 去了")


if __name__ == "__main__":
    unittest.main()