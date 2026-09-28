"""用量聚合 + 算钱口径的测试。

口径是用户明确要求的：底部信息栏和用量统计页必须同源、对得上；套餐制不许
编单价；不同币种的钱不许相加。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "python"))

import model_catalog  # noqa: E402
import sessionstore  # noqa: E402


class UsageSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sessionstore.SessionStore(os.path.join(self.tmp.name, "s.db"))
        self.sid = self.store.create_session(model="MiniMax-M3", title="t")
        self.other = self.store.create_session(model="MiniMax-M3", title="t2")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _usage(self, sid, model, i, o, cached=0):
        self.store.append_usage(session_id=sid, model=model, base_url="https://x",
                                input_tokens=i, output_tokens=o,
                                cached_input_tokens=cached, cost_usd=0.0)

    def test_totals_and_today_add_up(self):
        self._usage(self.sid, "MiniMax-M3", 1000, 200)
        self._usage(self.sid, "MiniMax-M3", 500, 100, cached=100)
        s = self.store.usage_summary(days=7, session_id=self.sid)
        self.assertEqual(s["session"]["input"], 1500)
        self.assertEqual(s["session"]["output"], 300)
        self.assertEqual(s["session"]["cached"], 100)
        self.assertEqual(s["session"]["tokens"], 1800)
        self.assertEqual(s["session"]["calls"], 2)
        # 只有这一条会话有记录：总账应等于它
        self.assertEqual(s["total"], {**s["total"], **{
            "input": 1500, "output": 300, "cached": 100, "tokens": 1800, "calls": 2}})
        # 今天必然包含刚才写的两行
        self.assertEqual(s["today"]["tokens"], 1800)
        self.assertEqual(s["byDay"][-1]["tokens"], 1800)

    def test_last_turn_is_the_context_meter_source(self):
        self._usage(self.sid, "MiniMax-M3", 900, 100)
        self._usage(self.sid, "MiniMax-M3", 1234, 56, cached=34)
        s = self.store.usage_summary(session_id=self.sid)
        last = s["session"]["lastTurn"]
        self.assertEqual(last["input"], 1234)
        self.assertEqual(last["cached"], 34)
        self.assertEqual(last["model"], "MiniMax-M3")

    def test_by_model_and_session_split(self):
        self._usage(self.sid, "MiniMax-M3", 100, 10)
        self._usage(self.other, "deepseek-flash", 200, 20)
        s = self.store.usage_summary(days=7)
        by_model = {r["model"]: r for r in s["byModel"]}
        self.assertEqual(by_model["MiniMax-M3"]["tokens"], 110)
        self.assertEqual(by_model["deepseek-flash"]["tokens"], 220)
        by_session = {r["sessionId"]: r for r in s["bySession"]}
        self.assertEqual(len(by_session), 2)
        self.assertEqual(by_session[self.other]["title"], "t2")

    def test_empty_db_is_zero_not_null(self):
        s = self.store.usage_summary(session_id="s-nope")
        self.assertEqual(s["total"]["tokens"], 0)
        self.assertEqual(s["today"]["tokens"], 0)
        self.assertEqual(s["byModel"], [])
        self.assertIsNone(s["session"]["lastTurn"])

    def test_today_section_carries_model_split(self):
        self._usage(self.sid, "MiniMax-M3", 1000, 200)
        s = self.store.usage_summary(days=7, session_id=self.sid)
        self.assertEqual([r["model"] for r in s["today"]["byModel"]], ["MiniMax-M3"])
        self.assertEqual(s["today"]["byModel"][0]["tokens"], 1200)


class CatalogSanityTest(unittest.TestCase):
    def test_catalog_entries_have_core_fields(self):
        entries = model_catalog.entries()
        self.assertGreater(len(entries), 20)
        for e in entries:
            self.assertTrue(e.get("provider"), e)
            self.assertTrue(e.get("model"), e)
            self.assertIsInstance(e.get("contextWindow"), (int, type(None)))
            self.assertIn("pricing", e)
            self.assertIn(e["pricing"].get("mode"),
                          ("per-token", "token-plan", "subscription", "unknown"))

    def test_every_unverified_quote_is_marked(self):
        statuses = set()
        for e in model_catalog.entries():
            for ev in e.get("evidence", []):
                statuses.add(ev.get("status"))
        self.assertTrue(statuses.issubset({"exact", "normalized", "unchecked", "unverified"}))
        self.assertNotIn(None, statuses)

    def test_presets_expose_catalog_for_dropdown(self):
        import modelconfig
        presets = modelconfig.list_presets()["presets"]
        self.assertTrue(any(p.get("catalog") for p in presets),
                        "至少一个对接方式要带预置模型")
        for p in presets:
            self.assertIsInstance(p.get("catalog"), list)


if __name__ == "__main__":
    unittest.main()