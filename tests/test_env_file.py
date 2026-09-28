"""`.env` 灌进程环境：界面上的 key 状态必须和实际能不能调通一致。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "python"))

import modelconfig  # noqa: E402


class LoadEnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.added = []

    def tearDown(self):
        for name in self.added:
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def _write(self, text):
        with open(os.path.join(self.root, ".env"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_loads_keys_and_strips_quotes(self):
        self._write("# comment\n\nA_TEST_KEY=\"abc123\"\nB_TEST_KEY='xyz'\nbroken line\n")
        self.added += ["A_TEST_KEY", "B_TEST_KEY"]
        count = modelconfig.load_env_file(self.root)
        self.assertEqual(count, 2)
        self.assertEqual(os.environ["A_TEST_KEY"], "abc123")
        self.assertEqual(os.environ["B_TEST_KEY"], "xyz")

    def test_existing_environment_wins(self):
        os.environ["C_TEST_KEY"] = "from-env"
        self.added.append("C_TEST_KEY")
        self._write("C_TEST_KEY=from-file\n")
        count = modelconfig.load_env_file(self.root)
        self.assertEqual(count, 0)
        self.assertEqual(os.environ["C_TEST_KEY"], "from-env")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(modelconfig.load_env_file(os.path.join(self.root, "nope")), 0)

    def test_makes_hasKey_agree_with_the_env_file(self):
        self._write("MINIMAX_CN_API_KEY_FAKE=1\n")
        self.added.append("MINIMAX_CN_API_KEY_FAKE")
        modelconfig.load_env_file(self.root)
        self.assertEqual(os.environ.get("MINIMAX_CN_API_KEY_FAKE"), "1")


if __name__ == "__main__":
    unittest.main()