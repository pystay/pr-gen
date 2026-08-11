"""本地静态分析：在不调用 LLM 的前提下，从 diff 中识别变更类型、关键符号与影响面。

这些启发式结果会作为「结构信号」喂给 LLM，帮助它正确区分
「修复缓存击穿」与「新增用户认证 API」这类语义差异，同时显著压缩 token。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .git_ops import FileChange

# 变更类型关键词（大小写不敏感，匹配 + 行）
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "bugfix": [
        "bug", "fix", "hotfix", "workaround", "hack", "crash", "exception",
        "error handling", "null pointer", "npe", "outofmemory", "ooms",
        "缓存击穿", "穿透", "雪崩", "死锁", "race condition", "off-by-one",
        "regression", "回退", "修正", "修复",
    ],
    "performance": [
        "perf", "optimize", "optimization", "cache", "caching", "lru", "ttl",
        "index", "索引", "缓存", "性能", "延迟", "latency", "throughput",
        "批处理", "batch", "pool", "连接池", "压缩", "compress", "async",
    ],
    "security": [
        "auth", "token", "jwt", "oauth", "csrf", "xss", "injection", "sql injection",
        "sanitize", "encrypt", "decrypt", "password", "permission", "rbac",
        "越权", "认证", "鉴权", "权限", "加密", "注入",
    ],
    "refactor": [
        "refactor", "rename", "restructure", "extract", "inline", "cleanup",
        "重构", "重命名", "抽取", "清理",
    ],
    "dependency": [
        "requirements.txt", "pyproject.toml", "package.json", "go.mod",
        "go.sum", "pom.xml", "build.gradle", "Cargo.toml", "Gemfile",
        "yarn.lock", "package-lock.json", "poetry.lock",
    ],
    "schema": [
        "migrations", "migration", "schema.sql", "alter table", "create table",
        "drop table", "add column", "迁移", "建表", "改表",
    ],
    "config": [".env.example", "config.", ".conf", "settings."],
}

# 测试文件
_TEST_HINTS = re.compile(
    r"(^|/)(test|tests|spec|__tests__)(/|_|\.)|_test\.|\.test\.|Test\.|Tests\.",
    re.IGNORECASE,
)

# 关键声明行（新增的公开符号）
_SYMBOL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("class", re.compile(r"^\+\s*(?:public\s+|final\s+|abstract\s+|export\s+)*"
                         r"class\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
    ("function", re.compile(r"^\+\s*(?:public\s+|private\s+|protected\s+|static\s+|"
                            r"async\s+|export\s+|def\s+|func\s+)*"
                            r"(?:def|func|function|fn)\s+([A-Za-z_][A-Za-z0-9_]*)",
                            re.MULTILINE)),
    ("method", re.compile(r"^\+\s*(?:public\s+|private\s+|protected\s+|static\s+|"
                          r"async\s+)*([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)),
    ("route", re.compile(r"^\+\s*@(?:app|router|bp|api)\.(?:get|post|put|patch|delete)"
                         r"\(\s*['\"]([^'\"]+)['\"]", re.MULTILINE)),
    ("api", re.compile(r"^\+\s*(?:GET|POST|PUT|PATCH|DELETE)\s+/[^\s]+",
                       re.IGNORECASE)),
    ("db", re.compile(r"^\+\s*(?:ALTER|CREATE|DROP|UPDATE|INSERT|DELETE)\s+"
                      r"(?:TABLE|INDEX|VIEW)?\s*([A-Za-z_][A-Za-z0-9_]*)",
                      re.IGNORECASE)),
    ("interface", re.compile(r"^\+\s*(?:export\s+)?(?:interface|type)\s+"
                             r"([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
]

# 公开 API 行被修改/删除（影响面）
_API_MODIFIED = re.compile(
    r"^[+-]\s*(?:public\s+|private\s+|protected\s+|export\s+|async\s+|"
    r"def\s+|func\s+)*(?:class|interface|def|func|function|fn|const|let|var)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


@dataclass
class FileAnalysis:
    path: str
    status: str
    additions: int
    deletions: int
    language: str
    types: list[str] = field(default_factory=list)
    new_symbols: list[str] = field(default_factory=list)
    removed_symbols: list[str] = field(default_factory=list)
    truncated: bool = False
    diff_text: str = ""  # 截断后的 hunk 文本，供 LLM 使用
    old_path: str | None = None


@dataclass
class Analysis:
    files: list[FileAnalysis] = field(default_factory=list)
    change_types: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)  # 顶层模块（目录）
    new_symbols: list[str] = field(default_factory=list)
    removed_symbols: list[str] = field(default_factory=list)
    impacts: list[str] = field(default_factory=list)
    issue_refs: list[str] = field(default_factory=list)

    @property
    def total_additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deletions for f in self.files)


def _match_types(text: str, path: str) -> list[str]:
    """根据文件路径与内容关键词，判断可能的变更类型（可多个）。"""
    types: list[str] = []
    low = text.lower()
    for t, kws in _TYPE_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in low or kw.lower() in path.lower():
                types.append(t)
                break
    if _TEST_HINTS.search(path):
        types.append("test")
    return types


def analyze_file(fc: FileChange) -> FileAnalysis:
    fa = FileAnalysis(
        path=fc.path,
        status=fc.status,
        additions=fc.additions,
        deletions=fc.deletions,
        language=fc.language,
        truncated=fc.truncated,
        diff_text=fc.diff_text,
        old_path=fc.old_path,
    )
    if fc.status == "deleted":
        return fa
    fa.types = _match_types(fc.diff_text, fc.path)

    for kind, pat in _SYMBOL_PATTERNS:
        for m in pat.finditer(fc.diff_text):
            sym = m.group(1).strip()
            if sym and len(sym) <= 64:
                fa.new_symbols.append(f"{kind}:{sym}")

    fa.removed_symbols = _removed_symbols(fc.diff_text)
    return fa


def _removed_symbols(diff_text: str) -> list[str]:
    """统计被删除的公开符号（仅出现在 - 行、未出现在 + 行）。"""
    added, removed = set(), set()
    for ln in diff_text.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            m = _API_MODIFIED.match(ln)
            if m:
                added.add(m.group(1))
        elif ln.startswith("-") and not ln.startswith("---"):
            m = _API_MODIFIED.match(ln)
            if m:
                removed.add(m.group(1))
    return sorted(removed - added)


def _top_module(path: str) -> str:
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "(root)"


def analyze(diff_files: list[FileChange], commits: list[str], branch: str,
            issue_refs: list[str]) -> Analysis:
    """对 diff 做整体分析。"""
    files = [analyze_file(fc) for fc in diff_files]
    an = Analysis(files=files, issue_refs=issue_refs)

    type_counter: dict[str, int] = {}
    for fa in files:
        for t in fa.types:
            type_counter[t] = type_counter.get(t, 0) + 1
    # 提交信息中的类型信号（优先级高）
    commit_low = " ".join(commits).lower()
    for t, kws in [("bugfix", ["fix", "fixes", "fixed", "bug", "修复", "bugfix"]),
                   ("feature", ["feat", "feature", "add", "新增", "feature"]),
                   ("performance", ["perf", "优化", "performance"]),
                   ("refactor", ["refactor", "重构", "refactor"]),
                   ("security", ["security", "auth", "安全", "认证"]),
                   ("test", ["test", "测试"])]:
        if any(kw in commit_low for kw in kws):
            type_counter[t] = type_counter.get(t, 0) + 2

    # 新增文件含新符号 → feature
    for fa in files:
        if fa.status == "added" and fa.new_symbols and "feature" not in type_counter:
            type_counter["feature"] = type_counter.get("feature", 0) + 1
            break

    rank = sorted(type_counter.items(), key=lambda kv: -kv[1])
    an.change_types = [t for t, _ in rank if t != "test"][:5]
    if type_counter.get("test"):
        an.change_types.append("test")

    # 模块分组（按顶层目录）
    modules: dict[str, int] = {}
    for fa in files:
        mod = _top_module(fa.path)
        modules[mod] = modules.get(mod, 0) + 1
    an.modules = sorted(modules, key=lambda m: -modules[m])

    # 汇总符号
    seen: set[str] = set()
    for fa in files:
        for sym in fa.new_symbols:
            if sym not in seen:
                seen.add(sym)
                an.new_symbols.append(sym)
    seen = set()
    for fa in files:
        for sym in fa.removed_symbols:
            if sym not in seen:
                seen.add(sym)
                an.removed_symbols.append(sym)

    # 影响面
    for fa in files:
        if "schema" in fa.types:
            an.impacts.append(f"数据库 Schema/迁移：{fa.path}")
        if "dependency" in fa.types:
            an.impacts.append(f"依赖变更：{fa.path}")
        if fa.removed_symbols:
            an.impacts.append(
                f"公开 API 变更（{fa.path}）：删除/修改 {', '.join(fa.removed_symbols[:5])}"
            )
        if fa.language in ("vue", "typescript", "javascript", "html", "css", "scss"):
            an.impacts.append(f"前端组件/界面：{fa.path}")
    return an
