"""git 操作：捕获分支差异、提交信息、项目结构。只读，不修改仓库状态。"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIT = "git"


class GitError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Path | None = None, timeout: int = 60) -> str:
    """运行 git 命令并返回 stdout。非零退出抛 GitError。"""
    try:
        proc = subprocess.run(
            [GIT, *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("未找到 git 命令，请先安装并配置 Git。") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git 命令超时: {args}") from exc
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or proc.stdout.strip() or f"git {args[0]} 失败")
    return proc.stdout


def repo_root(cwd: Path) -> Path:
    """返回仓库根目录的绝对路径。"""
    out = run_git(["rev-parse", "--show-toplevel"], cwd=cwd).strip()
    return Path(out)


def current_branch(cwd: Path) -> str:
    out = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).strip()
    return out or "HEAD"


def merge_base(base: str, cwd: Path) -> str:
    """目标分支与 HEAD 的 merge-base；找不到时抛 GitError。"""
    if base.startswith("-"):
        raise GitError(f"目标分支名不能以 '-' 开头: {base}")
    try:
        return run_git(["merge-base", base, "HEAD"], cwd=cwd).strip()
    except GitError as exc:
        raise GitError(f"找不到目标分支 '{base}'（或无法与 HEAD 计算共同祖先）：{exc}") from exc


def commit_messages(base_sha: str, cwd: Path, limit: int = 30) -> list[str]:
    """base..HEAD 的提交信息，按时间倒序（最新在前）。"""
    out = run_git(
        ["log", "--no-merges", "--pretty=%s", f"{base_sha}..HEAD", "-n", str(limit)],
        cwd=cwd,
    )
    return [ln for ln in out.splitlines() if ln.strip()]


@dataclass
class FileChange:
    """单个文件的变更信息。"""

    path: str
    status: str  # added | modified | deleted | renamed
    additions: int = 0
    deletions: int = 0
    old_path: str | None = None
    diff_text: str = ""  # 截断后的 hunk 文本（可能被截断）
    truncated: bool = False

    @property
    def language(self) -> str:
        ext = Path(self.path).suffix.lower()
        return {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".tsx": "typescript", ".jsx": "javascript", ".go": "go",
            ".java": "java", ".kt": "kotlin", ".rs": "rust", ".c": "c",
            ".h": "c", ".cpp": "cpp", ".cs": "csharp", ".rb": "ruby",
            ".php": "php", ".swift": "swift", ".sql": "sql", ".sh": "shell",
            ".yml": "yaml", ".yaml": "yaml", ".json": "json", ".toml": "toml",
            ".md": "markdown", ".vue": "vue", ".html": "html", ".css": "css",
            ".scss": "scss", ".proto": "proto", ".tf": "terraform",
        }.get(ext, ext.lstrip(".") or "text")


@dataclass
class DiffResult:
    base: str
    branch: str
    files: list[FileChange] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)

    @property
    def total_additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deletions for f in self.files)


def _parse_name_status(text: str) -> list[tuple[str, str, str]]:
    """解析 `git diff --name-status -z` 输出为 (status, path, old_path) 列表。"""
    entries: list[tuple[str, str, str]] = []
    parts = text.split("\0")
    i = 0
    while i < len(parts):
        st = parts[i]
        if not st:
            i += 1
            continue
        status = st[0]
        old_path = ""
        if status in ("R", "C"):
            if i + 2 < len(parts):
                old_path, path = parts[i + 1], parts[i + 2]
                i += 3
            else:
                i += 1
                continue
        else:
            if i + 1 < len(parts):
                path = parts[i + 1]
                i += 2
            else:
                i += 1
                continue
        entries.append((status, path, old_path))
    return entries


def _numstat_line(line: str) -> tuple[int, int] | None:
    """解析 `git diff --numstat` 行 (add, del)。`-` 表示二进制。"""
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def get_diff(
    base: str,
    cwd: Path,
    max_hunk_lines: int = 60,
    max_files: int | None = None,
) -> DiffResult:
    """捕获 base..HEAD 的差异。

    max_hunk_lines: 每个文件送入分析的 diff 文本行数上限（超出截断，统计仍完整）。
    max_files: 最多分析的变更文件数（超出部分仅保留统计，不送 LLM）。
    """
    root = repo_root(cwd)
    sha = merge_base(base, cwd)
    branch = current_branch(cwd)

    name_status = run_git(
        ["diff", "--name-status", "-z", f"{sha}..HEAD"], cwd=root
    )
    numstat = run_git(["diff", "--numstat", f"{sha}..HEAD"], cwd=root)

    # path -> (add, del)
    stats: dict[str, tuple[int, int]] = {}
    for ln in numstat.splitlines():
        parsed = _numstat_line(ln)
        if not parsed:
            continue
        add, dele = parsed
        path = ln.split("\t", 2)[2] if len(ln.split("\t")) >= 3 else ""
        stats[path] = (add, dele)

    files: list[FileChange] = []
    for status, path, old_path in _parse_name_status(name_status):
        add, dele = stats.get(path, (0, 0))
        st = {
            "A": "added", "M": "modified", "D": "deleted",
            "R": "renamed", "C": "copied", "T": "modified",
        }.get(status, "modified")
        files.append(
            FileChange(path=path, status=st, additions=add, deletions=dele,
                       old_path=old_path or None)
        )

    # 按路径排序，保证稳定输出（利于缓存命中）
    files.sort(key=lambda f: f.path)

    # 单次获取完整 diff，按 "diff --git " 段切分（避免 200 文件 = 200 次 git 调用）
    # core.quotepath=false：让 git 输出原始 UTF-8 路径（默认中文路径会被八进制转义）
    full_diff = run_git(
        ["-c", "core.quotepath=false", "diff", "--no-color", "--find-renames",
         f"{sha}..HEAD"],
        cwd=root,
    )
    sections = _split_diff_sections(full_diff)

    # 截断：超过 max_files 时，只保留前 max_files 个的 diff 文本
    for idx, fc in enumerate(files):
        if fc.status == "deleted":
            continue  # 删除文件没有内容可送，统计已足够
        if max_files is not None and idx >= max_files:
            continue
        text = sections.get(fc.path)
        if text is None:
            continue  # 二进制等无文本段，统计已足够
        lines = text.splitlines()
        if len(lines) > max_hunk_lines:
            kept = lines[:max_hunk_lines]
            kept.append(
                f"... (diff 内容过长，已截断，共 {len(lines)} 行；"
                f"增 {fc.additions} 删 {fc.deletions})"
            )
            fc.diff_text = "\n".join(kept)
            fc.truncated = True
        else:
            fc.diff_text = text

    commits = commit_messages(sha, root)
    return DiffResult(base=base, branch=branch, files=files, commits=commits)


_DIFF_HEAD_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DIFF_HEAD_START = "diff --git "


def _split_diff_sections(full_diff: str) -> dict[str, str]:
    """把完整 diff 按文件切分为 {new_path: section_text}。

    对 renamed/copied 文件同时记录 old_path → section，保证按旧路径查找也能命中。
    """
    sections: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    current_old: str | None = None

    def flush() -> None:
        nonlocal current_path, current_lines, current_old
        if current_path is not None:
            sections[current_path] = "\n".join(current_lines)
            if current_old and current_old != current_path:
                sections.setdefault(current_old, sections[current_path])
        current_path, current_lines, current_old = None, [], None

    for ln in full_diff.splitlines():
        if ln.startswith(_DIFF_HEAD_START):
            flush()
            m = _DIFF_HEAD_RE.match(ln)
            if m:
                current_old = _unquote_path(m.group(1))
                current_path = _unquote_path(m.group(2))
                current_lines = [ln]
            continue
        if current_path is not None:
            current_lines.append(ln)
    flush()
    return sections


def _unquote_path(p: str) -> str:
    """去掉 git 对含空格路径的引号转义（如 "a/foo bar.py"）。"""
    p = p.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1]
        # git 的 C 风格转义：\" \\ \t \n
        p = p.replace('\\"', '"').replace("\\\\", "\\")
        p = p.replace("\\t", "\t").replace("\\n", "\n")
    return p


def project_tree(cwd: Path, max_depth: int = 2, max_entries: int = 60) -> list[str]:
    """项目根目录结构（忽略 .git、常见产物目录）。用于给 LLM 提供上下文。"""
    root = repo_root(cwd)
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
            "build", ".idea", ".vscode", "target", "vendor", ".next", ".cache"}
    entries: list[str] = []
    try:
        for child in sorted(root.iterdir()):
            if child.name in skip or child.name.startswith("."):
                continue
            if child.is_dir():
                entries.append(child.name + "/")
                if max_depth >= 2:
                    try:
                        sub = sorted(
                            c for c in child.iterdir()
                            if c.name not in skip and not c.name.startswith(".")
                        )
                        for s in sub[:max_entries]:
                            entries.append("  " + s.name + ("/" if s.is_dir() else ""))
                    except OSError:
                        pass
            else:
                entries.append(child.name)
    except OSError:
        pass
    return entries


_ISSUE_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])#(\d+)(?![0-9])"),
    re.compile(r"\b(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)"
               r"[esd]*\s+#?(\d+)\b", re.IGNORECASE),
    re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b"),
]


def extract_issues(texts: list[str]) -> list[str]:
    """从分支名与提交信息中提取 Issue 编号（#123 / JIRA-123 / fix #42）。"""
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for pat in _ISSUE_PATTERNS:
            for m in pat.finditer(text):
                group = m.groups()[0] if m.groups() else m.group(0)
                # 纯数字 → GitHub 风格 #123；字母前缀（JIRA-123）→ 原样
                token = f"#{group}" if group.isdigit() else group
                if token not in seen:
                    seen.add(token)
                    found.append(token)
    return found
