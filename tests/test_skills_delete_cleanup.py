"""回归: 删技能必须真删干净(不留 .skdel-*) + 批量失败回滚完整搬回。

对应 bug: apply_operations 把 delete 的目录搬去 .skdel-* 临时处后, 循环结束
从不调用 _discard_held, 导致「删除成功」但内容原封不动留在盘上。
(DESK_AGENT_SKILLS_DIR 由 _target_dirs 每次调用时读取, 所以在 setUp 里
设置环境变量即可生效, 无需在 import 前。)
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import skills_registry  # noqa: E402
from skills_registry import SkillWriteError, apply_operations

BODY = (
    "---\nname: %s\ndescription: %s\n---\n\nbody-of-%s\n"
)


def _create(name, body_desc=None):
    apply_operations([{
        "action": "create", "name": name,
        "description": body_desc or name,
        "body": BODY % (name, name, name),
        "when_to_use": "regression",
    }])


class DeleteCleanupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="skills-del-regr-")
        self._old = os.environ.get("DESK_AGENT_SKILLS_DIR")
        os.environ["DESK_AGENT_SKILLS_DIR"] = self._tmp
        skills_registry._invalidate_scan_cache()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DESK_AGENT_SKILLS_DIR", None)
        else:
            os.environ["DESK_AGENT_SKILLS_DIR"] = self._old
        skills_registry._invalidate_scan_cache()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _leftovers(self):
        return [d for d in os.listdir(self._tmp) if d.startswith(".skdel-")]

    def _has(self, name):
        return os.path.isdir(os.path.join(self._tmp, name))

    def test_delete_actually_removes_directory_and_leaves_no_skdel(self):
        _create("ca-regr-del")
        apply_operations([{"action": "write_file", "name": "ca-regr-del",
                           "rel_path": "references/notes.md", "content": "n"}])
        results = apply_operations([{"action": "delete", "name": "ca-regr-del"}])
        self.assertEqual(results, [{"action": "delete", "name": "ca-regr-del",
                                    "path": os.path.join(self._tmp, "ca-regr-del")}])
        self.assertFalse(self._has("ca-regr-del"))
        self.assertEqual(self._leftovers(), [])

    def test_failed_batch_rolls_back_delete_and_leaves_no_skdel(self):
        _create("ca-regr-a")
        apply_operations([{"action": "write_file", "name": "ca-regr-a",
                           "rel_path": "references/notes.md",
                           "content": "original-note"}])
        _create("ca-regr-b")
        with self.assertRaises(SkillWriteError) as ctx:
            apply_operations([
                {"action": "delete", "name": "ca-regr-a"},
                {"action": "create", "name": "ca-regr-c",
                 "description": "c", "body": BODY % ("ca-regr-c", "c", "c"),
                 "when_to_use": "regression"},
                {"action": "patch", "name": "no-such-skill",
                 "old_string": "x", "new_string": "y"},
            ])
        self.assertIn("rolled back", str(ctx.exception))
        # 被删的 a 完整搬回(含附属文件原文)
        self.assertTrue(self._has("ca-regr-a"))
        with open(os.path.join(self._tmp, "ca-regr-a", "SKILL.md"),
                  encoding="utf-8") as fh:
            self.assertIn("body-of-ca-regr-a", fh.read())
        with open(os.path.join(self._tmp, "ca-regr-a", "references", "notes.md"),
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "original-note")
        # 批内新建的 c 被撤掉, 无关的 b 仍在, 盘上无残留
        self.assertFalse(self._has("ca-regr-c"))
        self.assertTrue(self._has("ca-regr-b"))
        self.assertEqual(self._leftovers(), [])


if __name__ == "__main__":
    unittest.main()
