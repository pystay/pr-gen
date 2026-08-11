"""diff_analyzer 与 git_ops 的单元测试。"""

import unittest

from pr_gen.diff_analyzer import analyze
from pr_gen.git_ops import FileChange, extract_issues

CACHE_DIFF = """\
--- a/app/api.py
+++ b/app/api.py
@@ -10,7 +10,14 @@
 def get_user(user_id: int):
-    row = query_db("SELECT * FROM users WHERE id = ?", (user_id,))
-    return row
+    if not CACHE_ENABLED:
+        return query_db("SELECT * FROM users WHERE id = ?", (user_id,))
+    def _load():
+        return query_db("SELECT * FROM users WHERE id = ?", (user_id,))
+    return _cache.get_or_load(f"user:{user_id}", _load)
+
+
+def login(username: str, password: str, secret: str) -> str:
+    from app.auth import hash_password, verify_token
+    if not verify_token(hash_password(password), secret):
+        raise PermissionError("invalid credentials")
+    return f"token-{username}-{secret[:4]}"
"""

NEW_CLASS_DIFF = """\
--- /dev/null
+++ b/app/cache_manager.py
@@ -0,0 +1,29 @@
+import threading
+import time
+
+
+class CacheManager:
+    \"\"\"线程安全的带锁缓存，使用双重检查防止缓存击穿。\"\"\"
+
+    def __init__(self, ttl: int = 300):
+        self._ttl = ttl
+
+    def get_or_load(self, key: str, loader):
+        now = time.time()
+        entry = self._store.get(key)
+        if entry and entry[0] > now:
+            return entry[1]
+        lock = self._locks.setdefault(key, threading.Lock())
+        with lock:
+            value = loader()
+            self._store[key] = (now + self._ttl, value)
+            return value
+"""

MIGRATION_DIFF = """\
--- a/db/migrations/0002.sql
+++ b/db/migrations/0002.sql
@@ -1,3 +1,5 @@
+ALTER TABLE users ADD COLUMN email VARCHAR(255);
+CREATE INDEX idx_users_email ON users(email);
"""


class TestIssueExtraction(unittest.TestCase):
    def test_github_style(self):
        self.assertEqual(
            extract_issues(["fix: 修复缓存击穿问题 (#42)", "feature/user-auth"]),
            ["#42"],
        )

    def test_keyword_style(self):
        self.assertEqual(
            extract_issues(["fixes #12", "close #34", "Resolves #56"]),
            ["#12", "#34", "#56"],
        )

    def test_jira_style(self):
        self.assertEqual(
            extract_issues(["feat: add login (JIRA-107)", "PROJ-88 done"]),
            ["JIRA-107", "PROJ-88"],
        )

    def test_no_false_positive_on_versions(self):
        self.assertEqual(extract_issues(["bump to 3.10"]), [])


class TestAnalyze(unittest.TestCase):
    def _analyze(self):
        files = [
            FileChange(path="app/api.py", status="modified", additions=14,
                       deletions=4, diff_text=CACHE_DIFF),
            FileChange(path="app/cache_manager.py", status="added",
                       additions=29, deletions=0, diff_text=NEW_CLASS_DIFF),
            FileChange(path="db/migrations/0002.sql", status="modified",
                       additions=2, deletions=0, diff_text=MIGRATION_DIFF),
        ]
        return analyze(files, commits=["fix: 缓存击穿 (#42)"], branch="feat/x",
                       issue_refs=["#42"])

    def test_change_types(self):
        an = self._analyze()
        self.assertIn("bugfix", an.change_types)
        self.assertIn("schema", an.change_types)
        self.assertIn("feature", an.change_types)

    def test_new_symbols(self):
        an = self._analyze()
        syms = " ".join(an.new_symbols)
        self.assertIn("class:CacheManager", syms)
        self.assertIn("function:login", syms)

    def test_removed_symbols(self):
        # get_user 函数签名未变（def 行是上下文行），不应被误判为 API 删除
        an = self._analyze()
        self.assertNotIn("get_user", an.removed_symbols)

    def test_removed_symbols_real_deletion(self):
        diff = (
            "-def old_api():\n"
            "-    return 1\n"
            "+def new_api():\n"
            "+    return 2\n"
        )
        files = [FileChange(path="app/api.py", status="modified",
                            additions=2, deletions=2, diff_text=diff)]
        an = analyze(files, [], "x", [])
        self.assertIn("old_api", an.removed_symbols)
        self.assertIn("function:new_api", an.new_symbols)

    def test_impacts(self):
        an = self._analyze()
        self.assertTrue(any("Schema" in i for i in an.impacts))

    def test_module_grouping(self):
        an = self._analyze()
        self.assertIn("app", an.modules)
        self.assertIn("db", an.modules)

    def test_test_file_detection(self):
        files = [
            FileChange(path="tests/test_cache_manager.py", status="added",
                       additions=10, deletions=0, diff_text="+def test_hit():"),
        ]
        an = analyze(files, [], "x", [])
        self.assertIn("test", an.files[0].types)


if __name__ == "__main__":
    unittest.main()
