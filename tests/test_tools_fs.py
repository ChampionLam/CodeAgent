"""Unit tests for the three filesystem tools (search_files / edit_file / delete_path).

Run either way:
    cd <repo> && python3 -m unittest tests.test_tools_fs -v
    python3 tests/test_tools_fs.py

Hard constraints inherited from tests/test_tools.py:
  * Architecture boundary: tools.py must not import permissions (ast + namespace).
  * Never write into the project tree: one throwaway workspace per test (tempfile).
  * Boundary checks: no tool may reach a path outside the workspace root.
  * Permission wiring is asserted indirectly (tools.py stays permission-free;
    the levels live in permissions.py and are already covered by
    tests/test_permissions.py - here we only pin the base levels for the three
    names so a registry drift is caught next to the implementation).
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import permissions  # noqa: E402  (test side only; tools.py itself must NOT import it)
import tools  # noqa: E402
from tools import ToolResult  # noqa: E402

TOOLS_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python", "tools.py")


class FsToolsTestBase(unittest.TestCase):
    """Fresh registry + one throwaway workspace per test."""

    def setUp(self) -> None:
        tools._reset_for_tests()
        tools.register_defaults()
        self.ws = tempfile.mkdtemp(prefix="desk_fs_ws_")
        self.base = os.path.dirname(self.ws.rstrip(os.sep))   # sibling area, outside ws

    def tearDown(self) -> None:
        tools._reset_for_tests()
        tools.register_defaults()
        shutil.rmtree(self.ws, ignore_errors=True)

    # ---- helpers -------------------------------------------------------
    def write_ws(self, rel: str, content: str = "") -> str:
        full = os.path.join(self.ws, rel)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return full

    def run_ok(self, name: str, args: dict) -> ToolResult:
        result = tools.execute(name, args, workspace_root=self.ws)
        self.assertTrue(result.ok, "expected success, got %s: %s" % (result.error_code, result.error_message))
        return result


class TestRegistrationAndBoundary(FsToolsTestBase):

    def test_three_tools_registered(self) -> None:
        names = {t.name for t in tools.list_tools()}
        for expected in ("search_files", "edit_file", "delete_path"):
            self.assertIn(expected, names)

    def test_tools_py_does_not_import_permissions(self) -> None:
        with open(TOOLS_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
        self.assertNotIn("permissions", imports, "tools.py imports permissions: boundary violation")

    def test_paths_outside_the_base_dir_are_allowed(self) -> None:
        """没有工作区概念了：基准目录之外也能读写（旧实现是 BAD_REQUEST）。"""
        # 用一个专门的小目录，别指向 /tmp 根 —— 递归扫整个临时目录会把测试挂死。
        outside_dir = os.path.join(self.base, "outside_probe")
        os.makedirs(outside_dir, exist_ok=True)
        outside = os.path.join(outside_dir, "probe.txt")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("hello")
        self.assertTrue(tools.execute("read_file", {"path": outside},
                                      workspace_root=self.ws).ok)
        self.assertTrue(tools.execute("search_files",
                                      {"pattern": "hello", "path": outside_dir},
                                      workspace_root=self.ws).ok)
        self.assertTrue(tools.execute("write_file",
                                      {"path": os.path.join(outside_dir, "new.txt"),
                                       "content": "x"},
                                      workspace_root=self.ws).ok)

    def test_no_levels_only_whether_it_asks(self) -> None:
        """分级（L0-L3）已经取消：现在只有「要不要问」这一件事。"""
        import guard
        self.assertFalse(guard.judge("search_files", {"pattern": "x", "path": self.ws})
                         .requires_approval)
        self.assertFalse(guard.judge("read_file", {"path": os.path.join(self.ws, "a.txt")})
                         .requires_approval)
        self.assertTrue(guard.judge_command("rm -rf /").requires_approval)
    def test_search_and_edit_never_ask(self) -> None:
        """读、写、搜、改都不问（没有工作区概念，也没有分级）。"""
        import guard
        for name, args in (("search_files", {"pattern": "x", "path": self.ws}),
                           ("edit_file", {"path": os.path.join(self.ws, "a.txt")}),
                           ("write_file", {"path": os.path.join(self.ws, "b.txt")}),
                           ("read_file", {"path": os.path.join(self.ws, "a.txt")})):
            self.assertFalse(guard.judge(name, args).requires_approval, name)

class TestSearchFiles(FsToolsTestBase):

    def test_finds_matches_with_paths_and_lines(self) -> None:
        self.write_ws("a.txt", "alpha\nbeta gamma\n")
        self.write_ws("sub/b.txt", "nothing here\nGAMMA tail\n")
        result = self.run_ok("search_files", {"pattern": "gamma"})
        # case-sensitive by default; sub/b.txt's GAMMA does not match
        self.assertEqual(result.content,
                         "%s:2: beta gamma" % os.path.join(self.ws, "a.txt"))
        self.assertEqual(len(result.data["matches"]), 1)
        match = result.data["matches"][0]
        self.assertEqual(match["path"], os.path.join(self.ws, "a.txt"))
        self.assertEqual(match["line"], 2)
        self.assertEqual(match["text"], "beta gamma")

    def test_relative_path_and_subdir_scope(self) -> None:
        self.write_ws("top.txt", "hit\n")
        self.write_ws("sub/inner.txt", "hit\n")
        result = self.run_ok("search_files", {"pattern": "hit", "path": "sub"})
        self.assertEqual(len(result.data["matches"]), 1)
        self.assertTrue(result.data["matches"][0]["path"].startswith(
            os.path.join(self.ws, "sub")))

    def test_file_glob_filters_filenames(self) -> None:
        self.write_ws("keep.py", "needle\n")
        self.write_ws("skip.txt", "needle\n")
        result = self.run_ok("search_files", {"pattern": "needle", "file_glob": "*.py"})
        paths = [m["path"] for m in result.data["matches"]]
        self.assertEqual(paths, [os.path.join(self.ws, "keep.py")])

    def test_no_matches_is_ok_not_error(self) -> None:
        self.write_ws("a.txt", "hello\n")
        result = self.run_ok("search_files", {"pattern": "zzz-no-such-token"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["matches"], [])
        self.assertIn("no matches", result.content)

    def test_regex_semantics(self) -> None:
        self.write_ws("r.txt", "foo123bar\nfoo45\n")
        result = self.run_ok("search_files", {"pattern": r"foo\d{2}\b"})
        texts = [m["text"] for m in result.data["matches"]]
        self.assertEqual(texts, ["foo45"])

    def test_reject_missing_search_root(self) -> None:
        result = tools.execute("search_files",
                               {"pattern": "x", "path": os.path.join(self.ws, "nope")},
                               workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")

    def test_reject_invalid_regex(self) -> None:
        result = tools.execute("search_files", {"pattern": "([unclosed"},
                               workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_reject_empty_or_wrong_type_pattern(self) -> None:
        for bad in ({}, {"pattern": ""}, {"pattern": "   "}, {"pattern": 42}):
            with self.subTest(bad=bad):
                result = tools.execute("search_files", bad, workspace_root=self.ws)
                self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_reject_not_a_directory(self) -> None:
        plain = self.write_ws("plain.txt", "x")
        result = tools.execute("search_files", {"pattern": "x", "path": plain},
                               workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_A_DIR")

    def test_binary_file_is_skipped_not_fatal(self) -> None:
        with open(os.path.join(self.ws, "blob.bin"), "wb") as fh:
            fh.write(b"\x00\x01\x02needle\x00")
        self.write_ws("text.txt", "needle\n")
        result = self.run_ok("search_files", {"pattern": "needle"})
        paths = [m["path"] for m in result.data["matches"]]
        self.assertEqual(paths, [os.path.join(self.ws, "text.txt")])

    @unittest.skipIf(os.name == "nt", "symlink semantics differ on Windows")
    def test_symlink_escape_is_not_followed(self) -> None:
        outside = tempfile.mkdtemp(prefix="desk_fs_outside_")
        try:
            with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as fh:
                fh.write("needle\n")
            os.symlink(outside, os.path.join(self.ws, "link"))
            result = self.run_ok("search_files", {"pattern": "needle"})
            self.assertEqual(result.data["matches"], [])
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class TestEditFile(FsToolsTestBase):

    def test_replaces_single_occurrence(self) -> None:
        path = self.write_ws("code.py", "value = 1\nprint(value)\n")
        result = self.run_ok("edit_file", {"path": path,
                                           "old_string": "value = 1",
                                           "new_string": "value = 2"})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "value = 2\nprint(value)\n")
        self.assertEqual(result.data["occurrences"], 1)
        self.assertEqual(result.data["bytes_before"], 23)
        self.assertEqual(result.data["bytes_after"], 23)

    def test_relative_path_anchors_to_workspace(self) -> None:
        self.write_ws("rel.txt", "aa bb cc\n")
        self.run_ok("edit_file", {"path": "rel.txt", "old_string": "bb", "new_string": "XX"})
        with open(os.path.join(self.ws, "rel.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "aa XX cc\n")

    def test_empty_new_string_deletes_the_match(self) -> None:
        path = self.write_ws("cut.txt", "keep [remove] this\n")
        self.run_ok("edit_file", {"path": path, "old_string": " [remove]",
                                  "new_string": ""})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep this\n")

    def test_reject_zero_occurrences_and_file_untouched(self) -> None:
        path = self.write_ws("a.txt", "original\n")
        result = tools.execute("edit_file", {"path": path, "old_string": "absent",
                                             "new_string": "x"}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")
        self.assertIn("not found", result.error_message or "")
        self.assertIn("not changed", result.error_message or "")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "original\n")     # no partial write

    def test_reject_multiple_occurrences_and_file_untouched(self) -> None:
        path = self.write_ws("dup.txt", "same\nsame\nsame\n")
        result = tools.execute("edit_file", {"path": path, "old_string": "same",
                                             "new_string": "x"}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")
        self.assertIn("3 times", result.error_message or "")   # says how many
        self.assertIn("unique", result.error_message or "")    # says what to do
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "same\nsame\nsame\n")  # untouched

    def test_more_context_disambiguates_to_success(self) -> None:
        path = self.write_ws("dup.txt", "a same\nb same\nc same\n")
        self.run_ok("edit_file", {"path": path, "old_string": "b same\n",
                                  "new_string": "b OTHER\n"})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "a same\nb OTHER\nc same\n")

    def test_reject_missing_file(self) -> None:
        result = tools.execute("edit_file",
                               {"path": os.path.join(self.ws, "missing.txt"),
                                "old_string": "a", "new_string": "b"},
                               workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")

    def test_reject_directory(self) -> None:
        result = tools.execute("edit_file", {"path": self.ws, "old_string": "a",
                                             "new_string": "b"}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "IS_A_DIR")

    def test_reject_bad_arguments(self) -> None:
        bad_cases = (
            {},                                              # no path
            {"path": ""},                                    # empty path
            {"path": 42, "old_string": "a"},                 # wrong path type
            {"path": "a.txt"},                               # no old_string
            {"path": "a.txt", "old_string": ""},             # empty old_string
            {"path": "a.txt", "old_string": "a", "new_string": 7},   # wrong type
        )
        for case in bad_cases:
            with self.subTest(case=case):
                result = tools.execute("edit_file", case, workspace_root=self.ws)
                self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_non_utf8_file_is_edited_byte_safely(self) -> None:
        path = os.path.join(self.ws, "mixed.bin")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe plain text here\n")
        result = self.run_ok("edit_file", {"path": path, "old_string": "plain",
                                           "new_string": "fancier"})
        self.assertEqual(result.data["bytes_before"], 19)
        self.assertEqual(result.data["bytes_after"], 21)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(b"\xff\xfe"))          # lossy bytes preserved
        self.assertIn(b"fancier", raw)


class TestDeletePath(FsToolsTestBase):

    def test_deletes_file(self) -> None:
        path = self.write_ws("gone.txt", "bye\n")
        result = self.run_ok("delete_path", {"path": path})
        self.assertFalse(os.path.exists(path))
        self.assertEqual(result.data["kind"], "file")

    def test_deletes_empty_directory(self) -> None:
        empty = os.path.join(self.ws, "empty_dir")
        os.mkdir(empty)
        result = self.run_ok("delete_path", {"path": empty})
        self.assertFalse(os.path.exists(empty))
        self.assertEqual(result.data["kind"], "directory")

    def test_relative_path_anchors_to_workspace(self) -> None:
        self.write_ws("rel.txt", "x")
        self.run_ok("delete_path", {"path": "rel.txt"})
        self.assertFalse(os.path.exists(os.path.join(self.ws, "rel.txt")))

    def test_reject_missing_path(self) -> None:
        result = tools.execute("delete_path",
                               {"path": os.path.join(self.ws, "nope.txt")},
                               workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")
        self.assertIn("no such path", result.error_message or "")

    def test_reject_non_empty_directory_and_names_blockers(self) -> None:
        busy = os.path.join(self.ws, "busy")
        os.makedirs(os.path.join(busy, "inner"), exist_ok=True)
        with open(os.path.join(busy, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        result = tools.execute("delete_path", {"path": busy}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")
        message = result.error_message or ""
        self.assertIn("not empty", message)
        self.assertIn("f.txt", message)      # the blockers are named
        self.assertIn("inner", message)
        # Nothing was removed: the tree is intact.
        self.assertTrue(os.path.isdir(busy))
        self.assertTrue(os.path.isfile(os.path.join(busy, "f.txt")))

    def test_reject_bad_path_argument(self) -> None:
        for bad in ({}, {"path": ""}, {"path": "   "}, {"path": 42}):
            with self.subTest(bad=bad):
                result = tools.execute("delete_path", bad, workspace_root=self.ws)
                self.assertEqual(result.error_code, "BAD_REQUEST")

    @unittest.skipIf(os.name == "nt", "symlink semantics differ on Windows")
    def test_deleting_a_symlink_removes_the_link_not_the_target(self) -> None:
        """删符号链接只删链接本身（旧实现会因为目标在工作区外而直接拒绝）。"""
        target = os.path.join(self.base, "symlink_target.txt")
        link = os.path.join(self.ws, "link.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("payload")
        if not hasattr(os, "symlink"):
            self.skipTest("no symlink support")
        os.symlink(target, link)
        result = tools.execute("delete_path", {"path": link}, workspace_root=self.ws)
        self.assertTrue(result.ok, result.content)
        self.assertFalse(os.path.lexists(link), "链接还在")
        self.assertTrue(os.path.exists(target), "目标被误删了")
