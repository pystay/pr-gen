"""提示词模板与输出解析：中英文两套，强调具体、可执行、无空洞措辞。"""

from __future__ import annotations

import re

from .diff_analyzer import Analysis

PROMPT_VERSION = 3  # 修改提示词结构时递增，使旧缓存失效

SYSTEM_ZH = """你是一名资深软件工程师兼技术文档作者，负责为 Pull Request 撰写高质量的描述。

要求：
1. 结构必须包含以下五个章节（Markdown 标题，顺序如下）：
   ## Summary（概述，一句话概括核心目的）
   ## Changes（变更明细，按模块/文件列出关键变更点，禁止罗列无意义路径）
   ## Test Plan（测试方法，给出可操作的具体测试建议，如单元测试范围、手工验证步骤）
   ## Impact（影响范围，标注 API、数据库 Schema、前端组件等影响与不兼容变更；无影响则写明"无"）
   ## Related Issues（相关 Issue 链接，如 #123；没有则写"无"）
2. 禁止空洞措辞：如"优化了性能""增强了稳定性""修复了一些问题"这类没有信息量的表述。
   必须说明做了什么、为什么、效果如何，例如："将缓存查询 TTL 从 60s 提升至 300s，命中率提升约 30%"。
3. Changes 中引用具体符号名（类名、函数名、文件名），说明变更意图，而非只抄 diff。
4. Test Plan 必须具体：给出测试文件/用例名或验证步骤，不要写"编写单元测试"这种空话。
5. Impact 中如涉及数据库 Schema、依赖、公开 API 变更，必须明确标注"破坏性变更"。
6. 使用简洁的 Markdown，中文回答，避免废话。"""

SYSTEM_EN = """You are a senior software engineer and technical writer crafting high-quality Pull Request descriptions.

Requirements:
1. The output MUST contain these five sections (Markdown headings, in order):
   ## Summary (one sentence capturing the core purpose)
   ## Changes (key changes by module/file; do NOT list meaningless paths)
   ## Test Plan (actionable, concrete testing suggestions: unit test scope, manual verification steps)
   ## Impact (APIs, DB schema, frontend components affected; flag breaking changes; write "None" if none)
   ## Related Issues (issue links like #123; write "None" if none)
2. No hollow phrasing: avoid "optimized performance", "improved stability", "fixed some issues".
   State what was done, why, and the effect, e.g. "Raised the cache TTL from 60s to 300s, boosting hit rate by ~30%".
3. In Changes, reference concrete symbols (class/function/file names) and intent, not raw diff lines.
4. Test Plan must be concrete: name test files/cases or verification steps, never just "write unit tests".
5. In Impact, explicitly mark "BREAKING" for schema, dependency, or public API changes.
6. Use concise Markdown. Answer in English."""


def system_prompt(lang: str) -> str:
    return SYSTEM_ZH if lang == "zh" else SYSTEM_EN


def _diff_block(files, max_lines_per_file: int = 60) -> str:
    """组装压缩后的 diff 文本块（LLM 的输入，已由本地分析截断）。"""
    parts: list[str] = []
    for fc in files:
        header = f"### {fc.status.upper()}: {fc.path}"
        if fc.old_path:
            header += f"  (原路径: {fc.old_path})"
        header += f"  [+{fc.additions} -{fc.deletions}]"
        parts.append(header)
        if fc.diff_text:
            parts.append(fc.diff_text)
    return "\n\n".join(parts)


def user_prompt(analysis: Analysis, commits: list[str], branch: str,
                project_tree: list[str], lang: str) -> str:
    """组装发给 LLM 的用户消息。"""
    is_zh = lang == "zh"
    lines: list[str] = []
    lines.append(
        f"### {'分支' if is_zh else 'Branch'}\n{branch}"
        f"\n### {'提交信息' if is_zh else 'Commits'}\n"
        + ("\n".join(f"- {c}" for c in commits) if commits else "—")
    )

    lines.append(f"### {'本地分析结果（结构信号）' if is_zh else 'Local analysis (structural signals)'}")
    lines.append(
        f"- {'变更类型' if is_zh else 'Change types'}: "
        + (", ".join(analysis.change_types) if analysis.change_types else "—")
    )
    lines.append(
        f"- {'涉及模块' if is_zh else 'Modules'}: "
        + (", ".join(analysis.modules) if analysis.modules else "—")
    )
    lines.append(
        f"- {'统计' if is_zh else 'Stats'}: "
        + f"{len(analysis.files)} files, +{analysis.total_additions}/-{analysis.total_deletions}"
    )
    if analysis.new_symbols:
        lines.append(
            f"- {'新增符号' if is_zh else 'New symbols'}: "
            + ", ".join(analysis.new_symbols[:40])
        )
    if analysis.removed_symbols:
        lines.append(
            f"- {'移除符号' if is_zh else 'Removed symbols'}: "
            + ", ".join(analysis.removed_symbols[:20])
        )
    if analysis.impacts:
        lines.append(
            f"- {'潜在影响' if is_zh else 'Potential impacts'}: "
            + "; ".join(analysis.impacts[:10])
        )
    if analysis.issue_refs:
        lines.append(
            f"- {'检测到 Issue 引用' if is_zh else 'Detected issue refs'}: "
            + ", ".join(analysis.issue_refs)
        )

    lines.append(
        f"### {'项目结构（上下文）' if is_zh else 'Project structure (context)'}\n"
        + ("\n".join(project_tree) if project_tree else "—")
    )

    lines.append(
        f"### {'变更内容 (diff)' if is_zh else 'Diff content'}\n"
        + (_diff_block(analysis.files) or "—")
    )

    lines.append(
        f"### {'任务' if is_zh else 'Task'}\n"
        + (
            "根据以上信息撰写 PR 描述，严格遵循系统提示中的五章节结构。"
            "基于本地分析信号正确区分变更类型（bugfix / feature / refactor / performance 等），"
            "并给出可操作的测试建议。"
            if is_zh
            else "Write the PR description per the five-section structure in the system prompt. "
                 "Use the local analysis signals to classify change types correctly "
                 "(bugfix / feature / refactor / performance, etc.) and give actionable test advice."
        )
    )
    return "\n\n".join(lines)


def edit_prompt(draft: str, instruction: str, lang: str, analysis: Analysis,
                commits: list[str]) -> str:
    """交互式微调：初稿 + 指令 + 原分析上下文。"""
    is_zh = lang == "zh"
    return (
        f"### {'指令' if is_zh else 'Instruction'}\n{instruction}\n\n"
        f"### {'当前 PR 描述草稿' if is_zh else 'Current PR description draft'}\n{draft}\n\n"
        f"### {'变更背景' if is_zh else 'Change context'}\n"
        f"- {'类型' if is_zh else 'Types'}: "
        + (", ".join(analysis.change_types) if analysis.change_types else "—")
        + f"\n- {'文件' if is_zh else 'Files'}: "
        + f"{len(analysis.files)} files, +{analysis.total_additions}/-{analysis.total_deletions}"
        + f"\n- {'提交' if is_zh else 'Commits'}: "
        + ("; ".join(commits[:10]) if commits else "—")
        + "\n\n"
        + (
            f"按照指令修改草稿。保持五章节结构（## Summary / ## Changes / ## Test Plan / "
            f"## Impact / ## Related Issues），只输出修改后的完整 Markdown 草稿，不要解释。"
            if is_zh
            else "Revise the draft per the instruction. Keep the five-section structure "
                 "(## Summary / ## Changes / ## Test Plan / ## Impact / ## Related Issues). "
                 "Output only the revised full Markdown draft, no explanation."
        )
    )


SECTION_ORDER = ["Summary", "Changes", "Test Plan", "Impact", "Related Issues"]


def ensure_sections(text: str, lang: str) -> str:
    """后处理：校验五章节齐全；缺失时补上占位标题，保证结构完整（验收要求）。"""
    is_zh = lang == "zh"
    titles = {
        "Summary": "Summary" if not is_zh else "Summary（概述）",
        "Changes": "Changes" if not is_zh else "Changes（变更明细）",
        "Test Plan": "Test Plan" if not is_zh else "Test Plan（测试方法）",
        "Impact": "Impact" if not is_zh else "Impact（影响范围）",
        "Related Issues": "Related Issues" if not is_zh else "Related Issues（相关 Issue）",
    }
    present = {s for s in SECTION_ORDER if re.search(rf"^##\s+{re.escape(s)}\b", text, re.M)}
    missing = [s for s in SECTION_ORDER if s not in present]
    if not missing:
        return text
    # 缺失章节补明确占位，保证结构完整（验收要求）
    parts = [text.rstrip()]
    for s in missing:
        parts.append(f"## {titles[s]}\n" + ("待补充。" if is_zh else "To be filled."))
    return "\n\n".join(parts)
