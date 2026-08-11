"""端到端验收测试：在 examples/demo-repo 上验证验收标准。

验收标准映射：
  A1. 至少 3 个文件变更（含新增类 + 逻辑修改）   → test_repo_meets_acceptance_criteria
  A2. 正确区分变更类型 + 可操作测试建议          → test_local_generation_classifies_types
  A3. Summary / Changes / Test Plan 三章节完整   → test_sections_complete
  A4. 无模板化空洞措辞                          → test_no_hollow_phrases
"""

import os
import unittest
from pathlib import Path
from unittest import mock

from pr_gen import cache as cache_mod
from pr_gen.generator import GenerateOptions, generate, revise
from pr_gen.llm import LLMClient

REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"

HOLLOW_PHRASES = [
    "优化了性能", "增强了稳定性", "修复了一些问题", "提升代码质量",
    "optimized performance", "improved stability", "fixed some issues",
    "improve code quality",
]

FAKE_LLM_OUTPUT = """\
## Summary
为用户信息查询引入带锁缓存以修复缓存击穿，并新增登录接口与 token 校验。

## Changes
- **app/cache_manager.py**：新增 `CacheManager` 类，`get_or_load` 使用双重检查锁，并发下仅回源一次。
- **app/api.py**：`get_user` 改为经缓存读取；新增 `login` 签发 token。
- **app/auth.py**：新增 `verify_token`（HMAC-SHA256 常量时间比较）。
- **app/config.py**：新增 `CACHE_TTL` 配置项。

## Test Plan
- 运行 `tests/test_cache_manager.py`：覆盖命中、过期、失效三路径。
- 运行 `tests/test_auth.py` 验证 token 校验。
- 手工验证：连续两次调用 `get_user(1)`，确认数据库仅查询一次。

## Impact
无破坏性变更；新增公开函数 `login`、`verify_token` 与类 `CacheManager`。

## Related Issues
#42
"""


class TestAcceptance(unittest.TestCase):
    def setUp(self):
        cache_mod.clear()
        self.assertTrue(REPO.is_dir(), f"缺少测试仓库: {REPO}")

    def tearDown(self):
        cache_mod.clear()

    # ---------- 验收 A1：仓库本身满足条件 ----------

    def test_repo_meets_acceptance_criteria(self):
        from pr_gen.generator import collect_context

        opts = GenerateOptions(cwd=REPO, use_cache=False)
        diff, analysis, _ = collect_context(opts)
        self.assertGreaterEqual(len(diff.files), 3)  # 至少 3 个文件变更
        paths = [f.path for f in diff.files]
        self.assertTrue(any(f.status == "added" for f in diff.files))
        # 含新增类
        self.assertTrue(any("class:" in s for s in analysis.new_symbols))
        # 含逻辑修改
        self.assertTrue(any(
            f.path == "app/api.py" and f.status == "modified" for f in diff.files
        ))

    # ---------- 验收 A2/A3/A4：本地模式（不调 API） ----------

    def test_local_generation_classifies_types(self):
        result = generate(GenerateOptions(cwd=REPO, local_only=True))
        self.assertIn("bugfix", result.analysis.change_types)
        self.assertIn("feature", result.analysis.change_types)
        self.assertIn("#42", result.analysis.issue_refs)
        self.assertIn("JIRA-107", result.analysis.issue_refs)

    def test_sections_complete(self):
        result = generate(GenerateOptions(cwd=REPO, local_only=True))
        for section in ["## Summary", "## Changes", "## Test Plan",
                        "## Impact", "## Related Issues"]:
            self.assertIn(section, result.text)

    def test_no_hollow_phrases(self):
        result = generate(GenerateOptions(cwd=REPO, local_only=True))
        low = result.text.lower()
        for phrase in HOLLOW_PHRASES:
            self.assertNotIn(phrase.lower(), low, f"出现空洞措辞: {phrase}")

    # ---------- 验收 A2：LLM 模式（mock） ----------

    def test_llm_generation_and_cache(self):
        with mock.patch.object(LLMClient, "messages", return_value=FAKE_LLM_OUTPUT):
            r1 = generate(GenerateOptions(cwd=REPO))
            self.assertEqual(r1.source, "llm")
            self.assertIn("## Summary", r1.text)
            self.assertIn("CacheManager", r1.text)
            # 再次运行应命中缓存
            r2 = generate(GenerateOptions(cwd=REPO))
            self.assertEqual(r2.source, "cache")
            self.assertEqual(r2.text, r1.text)
            # cache-only 模式同样命中
            r3 = generate(GenerateOptions(cwd=REPO, cache_only=True))
            self.assertEqual(r3.text, r1.text)

    def test_cache_only_miss_returns_empty(self):
        with mock.patch.object(LLMClient, "messages", return_value=FAKE_LLM_OUTPUT):
            r = generate(GenerateOptions(cwd=REPO, cache_only=True))
        self.assertEqual(r.text, "")
        self.assertEqual(r.source, "local")

    def test_sections_filled_when_missing(self):
        partial = "## Summary\n一句话。\n## Changes\n- x\n"
        with mock.patch.object(LLMClient, "messages", return_value=partial):
            r = generate(GenerateOptions(cwd=REPO))
        for section in ["## Summary", "## Changes", "## Test Plan",
                        "## Impact", "## Related Issues"]:
            self.assertIn(section, r.text)

    # ---------- 交互式微调 ----------

    def test_revise(self):
        with mock.patch.object(LLMClient, "messages",
                               return_value=FAKE_LLM_OUTPUT) as m:
            r1 = generate(GenerateOptions(cwd=REPO))
            r2 = revise(r1.text, "将测试方法部分改为侧重于集成测试",
                        GenerateOptions(cwd=REPO), prev=r1)
        self.assertEqual(r2.source, "llm")
        self.assertEqual(m.call_count, 2)

    def test_revise_cache_hit(self):
        with mock.patch.object(LLMClient, "messages",
                               return_value=FAKE_LLM_OUTPUT):
            r1 = generate(GenerateOptions(cwd=REPO))
            revise(r1.text, "改用集成测试", GenerateOptions(cwd=REPO), prev=r1)
            r3 = revise(r1.text, "改用集成测试", GenerateOptions(cwd=REPO), prev=r1)
        self.assertEqual(r3.source, "cache")


if __name__ == "__main__":
    unittest.main()
