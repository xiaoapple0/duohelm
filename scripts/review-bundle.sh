#!/bin/sh
# 生成 reviewer 子代理的审查材料（.claude/agents/reviewer.md）：只有命令的原始输出，没有结论。
# 用法：在要审查的分支的工作区里（任意子目录都可以）运行
#   sh scripts/review-bundle.sh > <会话临时目录>/review-bundle.txt
# 只运行只读命令：不 fetch、不改工作区和引用。某条命令失败时，对应小节写“报错：”加原始
# 错误输出，依赖它的小节写“未运行：”，不会留空，也不会把错误输出混成正常结果。
set -u

section() { printf '\n===== %s =====\n' "$1"; }
skipped() { echo "未运行：$1"; }
# 运行命令：成功时原样输出；失败时输出“报错：”加退出码和原始输出
run() {
  out=$("$@" 2>&1)
  code=$?
  if [ "$code" -eq 0 ]; then
    [ -n "$out" ] && printf '%s\n' "$out"
    return 0
  fi
  printf '报错（退出码 %s）：%s\n' "$code" "$out"
  return 1
}

cd "$(git rev-parse --show-toplevel)" || exit 1

section "工作区（仓库根目录）"
pwd -W 2>/dev/null || pwd  # Windows 的 Git Bash 下输出 D:/... 形式，Read 工具能直接用
section "当前分支"
branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
if [ -n "$branch" ]; then
  echo "$branch"
else
  echo "报错：分离 HEAD，没有分支名（HEAD 是 $(git rev-parse --short HEAD)）"
fi

section "远程 main（git ls-remote origin refs/heads/main）"
remote_main=""
if out=$(git ls-remote origin refs/heads/main 2>&1) && [ -n "$out" ]; then
  remote_main=$(printf '%s\n' "$out" | cut -f1)
  echo "$remote_main"
else
  echo "报错：${out:-没有输出（远程没有 main 分支？）}"
fi

section "merge-base（git merge-base HEAD <远程 main>）"
base=""
if [ -z "$remote_main" ]; then
  skipped "远程 main 没有取到"
elif out=$(git merge-base HEAD "$remote_main" 2>&1); then
  base=$out
  echo "$base"
else
  echo "报错：$out"
  echo "（本地可能没有远程 main 的这个提交，需要先 git fetch 再重新生成审查材料）"
fi

section "分支落后的提交（git log --oneline <merge-base>..<远程 main>；为空表示不落后）"
if [ -n "$base" ]; then run git log --oneline "$base..$remote_main"; else skipped "merge-base 失败"; fi
section "本分支的提交（git log --stat <merge-base>..HEAD）"
if [ -n "$base" ]; then run git log --stat "$base..HEAD"; else skipped "merge-base 失败"; fi
section "改动统计（git diff <merge-base>...HEAD --stat）"
if [ -n "$base" ]; then run git diff "$base...HEAD" --stat; else skipped "merge-base 失败"; fi

section "改动文件的大小（HEAD 里提交的版本，字节；已删除的文件不列）"
if [ -n "$base" ]; then
  git diff --name-only --diff-filter=d "$base...HEAD" | while IFS= read -r f; do
    if size=$(git cat-file -s "HEAD:$f" 2>&1); then
      printf '%s\t%s\n' "$size" "$f"
    else
      printf '报错：%s\t%s\n' "$size" "$f"
    fi
  done
else
  skipped "merge-base 失败"
fi

section "被跟踪却应被忽略的文件（git ls-files -i -c --exclude-standard；为空表示没有）"
run git ls-files -i -c --exclude-standard

section "PR 状态（gh pr view <分支> --json number,state,mergedAt）"
if [ -n "$branch" ]; then
  run gh pr view "$branch" --json number,state,mergedAt
else
  skipped "分离 HEAD，没有分支名"
fi

if [ -n "$base" ] && git diff --name-only "$base...HEAD" | grep -q '^\.github/'; then
  section "必需检查（gh api repos/xiaoapple0/duohelm/rules/branches/main）"
  run gh api repos/xiaoapple0/duohelm/rules/branches/main
fi

section "完整 diff（git diff <merge-base>...HEAD）"
if [ -n "$base" ]; then run git diff "$base...HEAD"; else skipped "merge-base 失败"; fi
