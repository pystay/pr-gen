"""性能基准：200 个变更文件的极端场景。

测量：
  t1 = 本地分析（git 捕获 + 静态分析）耗时
  t2 = 缓存命中后的完整 generate（cache-only）耗时
验收目标：非 LLM 部分 < 15s（实际应为毫秒级）。

用法: python scripts/bench_200files.py [--files 200]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pr_gen import cache as cache_mod  # noqa: E402
from pr_gen.generator import GenerateOptions, generate  # noqa: E402


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Bench", "-c", "user.email=bench@example.com",
         *args],
        cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def make_repo(files: int) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="prgen-bench-"))
    repo = tmp / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    for i in range(files):
        p = repo / f"mod{i // 20}" / f"file_{i:03d}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"VALUE = {i}\n\ndef get_{i}():\n    return {i}\n",
                     encoding="utf-8")
    git("add", ".", cwd=repo)
    git("commit", "-m", "init bench", cwd=repo)
    git("checkout", "-b", "bench/change", cwd=repo)
    for i in range(files):
        p = repo / f"mod{i // 20}" / f"file_{i:03d}.py"
        p.write_text(
            f"VALUE = {i + 1}\n\ndef get_{i}():\n    return {i + 1}\n\n"
            f"def extra_{i}():\n    return {i} * 2\n",
            encoding="utf-8",
        )
    git("add", ".", cwd=repo)
    git("commit", "-m", "modify all files", cwd=repo)
    return repo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=200)
    args = parser.parse_args()

    cache_mod.clear()
    repo = make_repo(args.files)
    opts = GenerateOptions(cwd=repo, cache_only=True, max_hunk_lines=10)

    # 第一次：本地分析 + 生成（缓存未命中，cache-only 不调 API）
    t0 = time.perf_counter()
    r1 = generate(opts)
    t_analysis = time.perf_counter() - t0
    # 第二次：缓存命中
    t0 = time.perf_counter()
    r2 = generate(opts)
    t_hit = time.perf_counter() - t0

    print(f"文件数: {len(r1.diff.files)}")
    print(f"变更行: +{r1.diff.total_additions}/-{r1.diff.total_deletions}")
    print(f"本地分析+未命中: {t_analysis:.3f}s")
    print(f"缓存命中完整流程: {t_hit:.3f}s")
    print(f"目标: <15s  →  {'PASS' if t_analysis < 15 else 'FAIL'}")

    shutil.rmtree(repo.parent, ignore_errors=True)
    return 0 if t_analysis < 15 else 1


if __name__ == "__main__":
    sys.exit(main())
