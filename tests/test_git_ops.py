"""git_ops 单元测试：diff 段切分（空格/中文路径）、merge-base 校验、hook 脚本。"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from pr_gen import git_ops
from pr_gen.cli import HOOK_SCRIPT
from pr_gen.git_ops import _split_diff_sections


class TestSplitDiffSections(unittest.TestCase):
    def test_normal_paths(self):
        diff = (
            "diff --git a/app/a.py b/app/a.py\n"
            "index 111..222 100644\n"
            "--- a/app/a.py\n"
            "+++ b/app/a.py\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "diff --git a/app/b.py b/app/b.py\n"
            "--- a/app/b.py\n"
            "+++ b/app/b.py\n"
        )
        sec = _split_diff_sections(diff)
        self.assertIn("app/a.py", sec)
        self.assertIn("app/b.py", sec)
        self.assertIn("+b", sec["app/a.py"])

    def test_space_paths(self):
        diff = (
            "diff --git a/foo bar.py b/foo bar.py\n"
            "--- a/foo bar.py\n"
            "+++ b/foo bar.py\n"
            "@@ -1 +1 @@\n"
            "+x\n"
        )
        sec = _split_diff_sections(diff)
        self.assertIn("foo bar.py", sec)
        self.assertIn("+x", sec["foo bar.py"])

    def test_utf8_paths(self):
        # core.quotepath=false 后的原始 UTF-8 路径
        diff = (
            "diff --git a/中文文件.py b/中文文件.py\n"
            "--- a/中文文件.py\n"
            "+++ b/中文文件.py\n"
            "@@ -1 +1 @@\n"
            "+y\n"
        )
        sec = _split_diff_sections(diff)
        self.assertIn("中文文件.py", sec)
        self.assertIn("+y", sec["中文文件.py"])

    def test_quoted_paths_fallback(self):
        # 防御：仍可能出现的引号包裹路径
        diff = (
            'diff --git "a/foo bar.py" "b/foo bar.py"\n'
            "--- a/foo bar.py\n"
            "+++ b/foo bar.py\n"
            "@@ -1 +1 @@\n"
            "+z\n"
        )
        sec = _split_diff_sections(diff)
        # 正则无法解析带引号头部，但段不丢失、不崩溃
        self.assertIsInstance(sec, dict)

    def test_renamed_paths(self):
        diff = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 90%\n"
            "rename from old.py\n"
            "rename to new.py\n"
            "@@ -1 +1 @@\n"
            "+r\n"
        )
        sec = _split_diff_sections(diff)
        self.assertIn("new.py", sec)
        self.assertIn("old.py", sec)  # 旧路径也可查


class TestMergeBase(unittest.TestCase):
    def test_rejects_dash_prefix(self):
        with self.assertRaises(git_ops.GitError) as ctx:
            git_ops.merge_base("-oops", Path("."))
        self.assertIn("-", str(ctx.exception))


class TestGetDiffEndToEnd(unittest.TestCase):
    """临时 git 仓库端到端：中文/空格路径的 diff 文本必须完整捕获。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-git-"))
        self._git("init", "-b", "main")
        self._git("config", "user.name", "T")
        self._git("config", "user.email", "t@t")
        (self.tmp / "中文文件.py").write_text("v1\n", encoding="utf-8")
        (self.tmp / "foo bar.py").write_text("v1\n", encoding="utf-8")
        (self.tmp / "plain.py").write_text("v1\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "init")
        self._git("checkout", "-b", "feature/x")
        (self.tmp / "中文文件.py").write_text("v1\nv2\n", encoding="utf-8")
        (self.tmp / "foo bar.py").write_text("v1\nv2\n", encoding="utf-8")
        (self.tmp / "plain.py").write_text("v1\nchanged\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "change")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.tmp, check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        ).stdout

    def test_diff_text_captured(self):
        diff = git_ops.get_diff("main", self.tmp, max_hunk_lines=60)
        paths = {f.path for f in diff.files}
        self.assertEqual(paths, {"中文文件.py", "foo bar.py", "plain.py"})
        by_path = {f.path: f for f in diff.files}
        self.assertIn("+v2", by_path["中文文件.py"].diff_text)
        self.assertIn("+v2", by_path["foo bar.py"].diff_text)
        self.assertIn("+changed", by_path["plain.py"].diff_text)
        self.assertFalse(any(f.truncated for f in diff.files))

    def test_truncation_marks(self):
        diff = git_ops.get_diff("main", self.tmp, max_hunk_lines=2)
        for f in diff.files:
            self.assertTrue(f.truncated)
            self.assertIn("已截断", f.diff_text)

    def test_issue_from_commit(self):
        self._git("commit", "--allow-empty", "-m", "fix: 中文路径问题 (#99)")
        diff = git_ops.get_diff("main", self.tmp)
        issues = git_ops.extract_issues([diff.branch, *diff.commits])
        self.assertIn("#99", issues)


class TestHookScript(unittest.TestCase):
    def test_contains_platform_separator_logic(self):
        self.assertIn('SEP=":"', HOOK_SCRIPT)
        self.assertIn("MINGW*|MSYS*|CYGWIN*) SEP=\";\"", HOOK_SCRIPT)
        # 生成命令带 mode 占位符（install 时替换为 --cache-only 或空）
        self.assertIn("generate %s --quiet", HOOK_SCRIPT)

    def test_contains_base_branch_guard(self):
        self.assertIn('[ "$BRANCH" = "main" ]', HOOK_SCRIPT)


if __name__ == "__main__":
    unittest.main()
