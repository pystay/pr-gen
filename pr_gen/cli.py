"""pr-gen 命令行入口。

子命令：
  generate        生成 PR 描述（默认）
  edit            用自然语言指令微调草稿（交互式或一次性）
  install-hooks   安装 Git Hook（prepare-commit-msg：缓存命中时刷新草稿）
  uninstall-hooks 卸载 Git Hook
  cache           查看/清理本地缓存
  diff            调试：输出捕获到的差异摘要（不调 LLM）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, cache as cache_mod
from .generator import GenerateOptions, generate, revise
from .git_ops import GitError, repo_root

HOOK_NAME = "post-commit"

HOOK_SCRIPT = """#!/bin/sh
# pr-gen hook (自动安装，勿手动编辑)
# 每次 commit 后刷新 PR 描述草稿到 .git/pr-description.md。
# 默认仅当缓存命中时刷新（不调 API）；用 install-hooks --api 安装为总是调用 API。
PR_GEN_DIR="%s"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$ROOT" || exit 0
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || exit 0
[ "$BRANCH" = "main" ] && exit 0
SEP=":"
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) SEP=";" ;;
esac
export PYTHONPATH="$PR_GEN_DIR${PYTHONPATH:+$SEP$PYTHONPATH}"
python -m pr_gen generate %s --quiet --file ".git/pr-description.md" 2>/dev/null
exit 0
"""


def _repo_root_or_die() -> Path:
    try:
        return repo_root(Path.cwd())
    except GitError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(2)


def cmd_generate(args: argparse.Namespace) -> int:
    opts = GenerateOptions(
        base=args.base,
        lang=args.lang,
        model=args.model,
        max_hunk_lines=args.max_hunk_lines,
        max_files=args.max_files,
        use_cache=not args.no_cache,
        cache_only=args.cache_only,
        local_only=args.local,
        api_key_env=args.api_key_env,
        timeout=args.timeout,
    )
    try:
        result = generate(opts)
    except GitError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # LLMError 等
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.file:
        # cache-only 且未命中时不写文件：保留旧草稿，避免覆盖
        if not (args.cache_only and not result.text):
            out = Path(args.file)
            out.write_text(result.text, encoding="utf-8")
            note = f"已写入 {out}"
        else:
            note = "缓存未命中，保留原文件（--cache-only）"
    else:
        print(result.text)
        note = ""
    if not args.quiet:
        src = {"llm": "AI 生成", "cache": "缓存命中", "local": "本地模式"}[result.source]
        print(
            f"\n--- [{src} | {result.lang} | {len(result.diff.files)} 个文件 | "
            f"{result.elapsed:.1f}s]{(' | ' + note) if note else ''}",
            file=sys.stderr,
        )
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    opts = GenerateOptions(
        base=args.base, lang=args.lang, model=args.model,
        max_hunk_lines=args.max_hunk_lines, max_files=args.max_files,
        use_cache=not args.no_cache, api_key_env=args.api_key_env,
        timeout=args.timeout,
    )
    try:
        if args.draft:
            draft = Path(args.draft).read_text(encoding="utf-8")
            prev = None
        else:
            prev = generate(opts)
            draft = prev.text
    except GitError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.instruction:
        instructions = [args.instruction]
    else:
        # 交互式 REPL
        print("=== PR 描述草稿 ===")
        print(draft)
        instructions = []
        while True:
            try:
                line = input("\n输入修改指令（回车退出，save 保存）: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            if line.lower() in ("save", "保存"):
                path = input("保存到文件: ").strip() or "pr-description.md"
                Path(path).write_text(draft, encoding="utf-8")
                print(f"已保存到 {path}")
                continue
            instructions.append(line)

    try:
        for i, ins in enumerate(instructions):
            result = revise(draft, ins, opts, prev=prev)
            draft = result.text
            prev = result
            print(f"\n=== 修订 {i + 1}: {ins} ===")
            print(draft)
            if result.cached:
                print("（缓存命中）", file=sys.stderr)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.file and not args.instruction:
        Path(args.file).write_text(draft, encoding="utf-8")
        print(f"已写入 {args.file}")
    return 0


def _hook_path(root: Path) -> Path:
    return root / ".git" / "hooks" / HOOK_NAME


def cmd_install_hooks(args: argparse.Namespace) -> int:
    root = _repo_root_or_die()
    hook = _hook_path(root)
    hook.parent.mkdir(parents=True, exist_ok=True)
    # 包所在目录（pr-gen/），hook 通过 PYTHONPATH + python -m pr_gen 调用
    # Windows 路径转正斜杠，避免 sh 中反斜杠被当转义符
    pkg_dir = str(Path(__file__).resolve().parent.parent).replace("\\", "/")
    mode = "" if args.api else "--cache-only "
    hook.write_text(HOOK_SCRIPT % (pkg_dir, mode), encoding="utf-8")
    print(f"已安装 Hook: {hook}")
    if args.api:
        print("模式：每次 commit 后调用 API 生成最新描述（较慢、消耗额度）")
    else:
        print("模式：缓存命中时才刷新草稿，未命中不调用 API、不覆盖旧草稿")
    return 0


def cmd_uninstall_hooks(args: argparse.Namespace) -> int:
    root = _repo_root_or_die()
    hook = _hook_path(root)
    if hook.exists():
        hook.unlink()
        print(f"已卸载 Hook: {hook}")
    else:
        print("未找到已安装的 Hook。")
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    if args.action == "stats":
        st = cache_mod.stats()
        for k, v in st.items():
            print(f"{k}: {v}")
    elif args.action == "clear":
        n = cache_mod.clear()
        print(f"已清除 {n} 条缓存。")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from .generator import collect_context

    opts = GenerateOptions(base=args.base, lang=args.lang,
                           max_hunk_lines=args.max_hunk_lines,
                           max_files=args.max_files, use_cache=False)
    try:
        diff, analysis, lang = collect_context(opts)
    except GitError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"分支: {diff.branch}  ←  {diff.base}")
    print(f"提交: {len(diff.commits)} 个 | 文件: {len(diff.files)} 个 | "
          f"+{diff.total_additions}/-{diff.total_deletions}")
    print(f"语言: {lang} | 类型: {', '.join(analysis.change_types) or '—'}")
    for fa in analysis.files:
        print(f"  [{fa.status:8s}] {fa.path}  +{fa.additions}/-{fa.deletions}"
              + (f"  新符号: {', '.join(fa.new_symbols[:4])}" if fa.new_symbols else ""))
    if analysis.impacts:
        print("影响面:")
        for imp in analysis.impacts:
            print(f"  - {imp}")
    if analysis.issue_refs:
        print(f"Issue 引用: {', '.join(analysis.issue_refs)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-gen",
        description="自动化 PR 描述生成器：分析 git diff，生成结构化 Markdown 描述。",
    )
    parser.add_argument("--version", action="version", version=f"pr-gen {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_gen = sub.add_parser("generate", help="生成 PR 描述（默认命令）")
    p_gen.add_argument("--base", default="main", help="目标分支（默认 main）")
    p_gen.add_argument("--lang", choices=["auto", "zh", "en"], default="auto",
                       help="输出语言（默认 auto：按提交信息判断）")
    p_gen.add_argument("--model", default="deepseek-v4-flash", help="模型名")
    p_gen.add_argument("--max-hunk-lines", type=int, default=60,
                       help="每个文件送入分析的 diff 行数上限（默认 60）")
    p_gen.add_argument("--max-files", type=int, default=None,
                       help="最多分析的变更文件数（超出仅统计）")
    p_gen.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    p_gen.add_argument("--cache-only", action="store_true",
                       help="仅当缓存命中时输出（供 Git Hook 使用，不调 API）")
    p_gen.add_argument("--local", action="store_true", help="纯本地模板模式（不调 LLM）")
    p_gen.add_argument("--file", default=None, help="写入文件而非 stdout")
    p_gen.add_argument("--api-key-env", default="DEEPSEEK_API_KEY",
                       help="API key 环境变量名（默认 DEEPSEEK_API_KEY）")
    p_gen.add_argument("--timeout", type=int, default=90, help="API 超时秒数")
    p_gen.add_argument("--quiet", action="store_true", help="不输出元信息")
    p_gen.set_defaults(func=cmd_generate)

    p_edit = sub.add_parser("edit", help="用自然语言指令微调草稿")
    p_edit.add_argument("instruction", nargs="?", default=None,
                        help="修改指令；省略则进入交互模式")
    p_edit.add_argument("--draft", default=None, help="从文件读取草稿（否则重新生成）")
    p_edit.add_argument("--file", default=None, help="交互模式退出时写入的文件")
    p_edit.add_argument("--base", default="main")
    p_edit.add_argument("--lang", choices=["auto", "zh", "en"], default="auto")
    p_edit.add_argument("--model", default="deepseek-v4-flash")
    p_edit.add_argument("--max-hunk-lines", type=int, default=60)
    p_edit.add_argument("--max-files", type=int, default=None)
    p_edit.add_argument("--no-cache", action="store_true")
    p_edit.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    p_edit.add_argument("--timeout", type=int, default=90)
    p_edit.set_defaults(func=cmd_edit)

    p_hook = sub.add_parser("install-hooks", help="安装 Git Hook")
    p_hook.add_argument("--api", action="store_true",
                        help="每次 commit 后调用 API 生成最新描述（默认仅缓存命中时刷新）")
    p_hook.set_defaults(func=cmd_install_hooks)

    p_unhook = sub.add_parser("uninstall-hooks", help="卸载 Git Hook")
    p_unhook.set_defaults(func=cmd_uninstall_hooks)

    p_cache = sub.add_parser("cache", help="缓存管理")
    p_cache.add_argument("action", choices=["stats", "clear"])
    p_cache.set_defaults(func=cmd_cache)

    p_diff = sub.add_parser("diff", help="调试：输出差异摘要（不调 LLM）")
    p_diff.add_argument("--base", default="main")
    p_diff.add_argument("--lang", choices=["auto", "zh", "en"], default="auto")
    p_diff.add_argument("--max-hunk-lines", type=int, default=60)
    p_diff.add_argument("--max-files", type=int, default=None)
    p_diff.set_defaults(func=cmd_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，强制 UTF-8 输出保证中文正确
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # 默认行为 = generate
        args = parser.parse_args(["generate", *argv])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
