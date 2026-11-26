#!/bin/sh
# 生成 reviewer 子代理的审查材料（.claude/agents/reviewer.md）：只有命令的原始输出，没有结论。
# 用法：在要审查的分支的工作区里运行 sh scripts/review-bundle.sh > <审查材料文件>
# 只运行只读命令：不 fetch、不改工作区和引用。
set -u

section() { printf '\n===== %s =====\n' "$1"; }

section "工作区"
pwd -W 2>/dev/null || pwd  # Windows 的 Git Bash 下输出 D:/... 形式，Read 工具能直接用
section "当前分支"
git rev-parse --abbrev-ref HEAD
section "远程 main（git ls-remote origin refs/heads/main）"
remote_main=$(git ls-remote origin refs/heads/main | cut -f1)
echo "$remote_main"
section "merge-base（git merge-base HEAD <远程 main>）"
base=$(git merge-base HEAD "$remote_main" 2>&1) && echo "$base" || { echo "报错：$base"; base=""; }
section "分支落后的提交（git log --oneline <merge-base>..<远程 main>）"
[ -n "$base" ] && git log --oneline "$base..$remote_main"
section "本分支的提交（git log --stat <merge-base>..HEAD）"
[ -n "$base" ] && git log --stat "$base..HEAD"
section "改动统计（git diff <merge-base>...HEAD --stat）"
[ -n "$base" ] && git diff "$base...HEAD" --stat
section "被跟踪却应被忽略的文件（git ls-files -i -c --exclude-standard）"
git ls-files -i -c --exclude-standard
section "PR 状态（gh pr view <分支> --json number,state,mergedAt）"
gh pr view "$(git rev-parse --abbrev-ref HEAD)" --json number,state,mergedAt 2>&1
if [ -n "$base" ] && git diff --name-only "$base...HEAD" | grep -q '^\.github/'; then
  section "必需检查（gh api repos/xiaoapple0/duohelm/rules/branches/main）"
  gh api repos/xiaoapple0/duohelm/rules/branches/main 2>&1
fi
section "完整 diff（git diff <merge-base>...HEAD）"
[ -n "$base" ] && git diff "$base...HEAD"
