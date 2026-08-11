"""生成主流程：捕获 diff → 本地分析（缓存）→ LLM 生成（缓存）→ 结构化输出。

缓存分层：
- analysis 层：纯本地，key = diff 指纹（不含模型/语言），命中即秒回
- generation 层：key = diff 指纹 + 模型 + 语言 + 提示词版本 + 指令
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cache as cache_mod
from . import git_ops
from .diff_analyzer import Analysis, FileAnalysis, analyze
from .git_ops import DiffResult
from .llm import LLMClient
from .prompt import PROMPT_VERSION, edit_prompt, ensure_sections, system_prompt, user_prompt

ZH_RE = re.compile(r"[\u4e00-\u9fff]")


def detect_lang(commits: list[str]) -> str:
    """auto 语言检测：提交信息含中文则用中文，否则英文。"""
    zh_chars = sum(len(ZH_RE.findall(c)) for c in commits)
    return "zh" if zh_chars >= 3 else "en"


def _analysis_to_dict(a: Analysis) -> dict:
    return {
        "files": [f.__dict__ for f in a.files],
        "change_types": a.change_types,
        "modules": a.modules,
        "new_symbols": a.new_symbols,
        "removed_symbols": a.removed_symbols,
        "impacts": a.impacts,
        "issue_refs": a.issue_refs,
    }


def _analysis_from_dict(d: dict) -> Analysis:
    return Analysis(
        files=[FileAnalysis(**f) for f in d.get("files", [])],
        change_types=list(d.get("change_types", [])),
        modules=list(d.get("modules", [])),
        new_symbols=list(d.get("new_symbols", [])),
        removed_symbols=list(d.get("removed_symbols", [])),
        impacts=list(d.get("impacts", [])),
        issue_refs=list(d.get("issue_refs", [])),
    )


@dataclass
class GenerationResult:
    text: str
    lang: str
    analysis: Analysis
    diff: DiffResult
    cached: bool = False
    elapsed: float = 0.0
    source: str = "llm"  # llm | local | cache


@dataclass
class GenerateOptions:
    base: str = "main"
    lang: str = "auto"  # auto | zh | en
    model: str = "deepseek-v4-flash"
    max_hunk_lines: int = 60
    max_files: int | None = None
    use_cache: bool = True
    cache_only: bool = False  # 命中缓存才返回（Git Hook 用，绝不调 API）
    local_only: bool = False  # 不调 LLM，纯本地模板（降级模式）
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout: int = 90
    cwd: Path | None = None


def collect_context(opts: GenerateOptions) -> tuple[DiffResult, Analysis, str]:
    """捕获并分析差异；返回 (diff, analysis, lang)。"""
    cwd = opts.cwd or Path.cwd()
    diff = git_ops.get_diff(
        opts.base, cwd,
        max_hunk_lines=opts.max_hunk_lines,
        max_files=opts.max_files,
    )
    lang = opts.lang
    if lang == "auto":
        lang = detect_lang(diff.commits)

    # Issue 提取：分支名 + 提交信息
    issue_refs = git_ops.extract_issues([diff.branch, *diff.commits])

    analysis_key = _diff_fingerprint(diff, opts)
    analysis = None
    if opts.use_cache:
        cached = cache_mod.get(analysis_key)
        if cached is not None:
            analysis = _analysis_from_dict(json.loads(cached))
    if analysis is None:
        analysis = analyze(diff.files, diff.commits, diff.branch, issue_refs)
        if opts.use_cache:
            cache_mod.put(analysis_key, "analysis",
                          json.dumps(_analysis_to_dict(analysis), ensure_ascii=False))
    return diff, analysis, lang


def _diff_fingerprint(diff: DiffResult, opts: GenerateOptions) -> str:
    """diff 指纹：统计 + 截断后的 diff 文本内容 + 截断参数。

    同时作为 analysis 与 generation 的缓存 key——文本内容变化（如 amend 改内容）
    或截断参数变化都会使 key 失效，避免命中语义不符的旧缓存。
    """
    return cache_mod.fingerprint({
        "kind": "diff",
        "files": [
            {"p": f.path, "s": f.status, "a": f.additions, "d": f.deletions,
             "o": f.old_path,
             "t": cache_mod.fingerprint({"t": f.diff_text}) if f.diff_text else None}
            for f in diff.files
        ],
        "commits": diff.commits,
        "branch": diff.branch,
        "max_hunk_lines": opts.max_hunk_lines,
        "max_files": opts.max_files,
    })


def _local_fallback(diff: DiffResult, analysis: Analysis, lang: str) -> str:
    """纯本地降级：无 LLM 时用规则模板生成（质量较低，保证可用）。"""
    is_zh = lang == "zh"
    t = "、".join(analysis.change_types) if analysis.change_types else "unknown"
    lines = [
        f"## Summary\n"
        + (f"本 PR 涉及 {len(diff.files)} 个文件（+{diff.total_additions}/-{diff.total_deletions}），"
           f"主要变更类型：{t}。"
           if is_zh else
           f"This PR touches {len(diff.files)} files (+{diff.total_additions}/-{diff.total_deletions}); "
           f"primary change types: {t}."),
        f"## Changes\n",
    ]
    for fa in analysis.files:
        sym = f"（{', '.join(fa.new_symbols[:4])}）" if fa.new_symbols else ""
        lines.append(
            f"- **{fa.path}** [{fa.status}] +{fa.additions}/-{fa.deletions}{sym}"
        )
    lines.append(
        f"## Test Plan\n"
        + (f"- 针对变更模块补充/运行单元测试；手动验证 "
           f"{', '.join(f.path for f in diff.files[:3])} 相关流程。"
           if is_zh
           else f"- Add/run unit tests for the changed modules; manually verify "
                f"{', '.join(f.path for f in diff.files[:3])}.")
    )
    lines.append(
        f"## Impact\n"
        + ("；".join(analysis.impacts) if analysis.impacts else ("无。" if is_zh else "None."))
    )
    issues = ", ".join(analysis.issue_refs) if analysis.issue_refs else ("无。" if is_zh else "None.")
    lines.append(f"## Related Issues\n{issues}")
    return "\n".join(lines)


def generate(opts: GenerateOptions) -> GenerationResult:
    start = time.monotonic()
    cwd = opts.cwd or Path.cwd()
    diff, analysis, lang = collect_context(opts)

    if opts.local_only:
        text = _local_fallback(diff, analysis, lang)
        return GenerationResult(
            text=ensure_sections(text, lang), lang=lang, analysis=analysis,
            diff=diff, cached=False, elapsed=time.monotonic() - start, source="local",
        )

    gen_key = cache_mod.fingerprint({
        "kind": "generation",
        "diff": _diff_fingerprint(diff, opts),
        "model": opts.model,
        "lang": lang,
        "prompt_version": PROMPT_VERSION,
        "instruction": None,
    })

    if opts.use_cache:
        cached = cache_mod.get(gen_key)
        if cached is not None:
            return GenerationResult(
                text=cached, lang=lang, analysis=analysis, diff=diff,
                cached=True, elapsed=time.monotonic() - start, source="cache",
            )
    if opts.cache_only:
        # 未命中且不允许调 API（Git Hook 场景）：返回空
        return GenerationResult(
            text="", lang=lang, analysis=analysis, diff=diff,
            cached=False, elapsed=time.monotonic() - start, source="local",
        )

    client = LLMClient(model=opts.model, timeout=opts.timeout,
                       api_key_env=opts.api_key_env)
    tree = git_ops.project_tree(cwd)
    user = user_prompt(analysis, diff.commits, diff.branch, tree, lang)
    text = client.messages(system_prompt(lang), user)
    text = ensure_sections(text, lang)
    if opts.use_cache:
        cache_mod.put(gen_key, "generation", text)
    return GenerationResult(
        text=text, lang=lang, analysis=analysis, diff=diff,
        cached=False, elapsed=time.monotonic() - start, source="llm",
    )


def revise(draft: str, instruction: str, opts: GenerateOptions,
           prev: GenerationResult | None = None) -> GenerationResult:
    """交互式微调：把草稿 + 指令发给 LLM 修订。"""
    start = time.monotonic()
    diff, analysis, lang = (
        (prev.diff, prev.analysis, prev.lang)
        if prev is not None else collect_context(opts)
    )
    gen_key = cache_mod.fingerprint({
        "kind": "generation",
        "diff": _diff_fingerprint(diff, opts),
        "model": opts.model,
        "lang": lang,
        "prompt_version": PROMPT_VERSION,
        "instruction": instruction,
    })
    if opts.use_cache:
        cached = cache_mod.get(gen_key)
        if cached is not None:
            return GenerationResult(
                text=cached, lang=lang, analysis=analysis, diff=diff,
                cached=True, elapsed=time.monotonic() - start, source="cache",
            )
    client = LLMClient(model=opts.model, timeout=opts.timeout,
                       api_key_env=opts.api_key_env)
    user = edit_prompt(draft, instruction, lang, analysis, diff.commits)
    text = client.messages(system_prompt(lang), user)
    text = ensure_sections(text, lang)
    if opts.use_cache:
        cache_mod.put(gen_key, "generation", text)
    return GenerationResult(
        text=text, lang=lang, analysis=analysis, diff=diff,
        cached=False, elapsed=time.monotonic() - start, source="llm",
    )
